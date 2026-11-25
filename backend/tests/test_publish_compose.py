"""发布域：成片合成的入队校验、ffmpeg 合成器、作业执行体与重启收尾。

与 ``test_videos.py`` 同一套夹具约定：SQLite 文件库 + 临时媒体目录，不碰真实
uploads/。ffmpeg 二进制不在测试里真跑——合成器那层把 ``ffmpeg_exe`` 指到假路径、
用假的子进程喂返回码与产物文件，执行体那层直接 monkeypatch ``runner.compose``，
测的是状态机与落盘登记，不是 imageio-ffmpeg 的轮子本身。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.crypto import encrypt
from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import (
    MediaJob,
    ModelConfig,
    PublishJob,
    VideoProject,
    VideoShot,
    WorkflowRun,
)
from app.models.schemas import CurrentUser, ModelPurpose
from app.services.materials import store as material_store
from app.services.media import (
    BIZ_VIDEO_SHOT,
    JOB_SUCCEEDED,
    KIND_VIDEO,
)
from app.services.media import store as media_store
from app.services.publish import (
    PUBLISH_FAILED,
    PUBLISH_QUEUED,
    PUBLISH_RUNNING,
    PUBLISH_SUCCEEDED,
)
from app.services.publish import composer, runner
from app.services.publish.composer import (
    ComposeProgress,
    PublishComposeError,
    VoiceoverLine,
    VoiceoverPlan,
    write_concat_list,
)
from app.services.media.tts import TtsError
from app.services.videos import service as video_service
from app.services.videos.service import VideoService
from app.services.workflows.builtin import seed_builtin_workflows

real_settings = get_settings()
OWNER = 1

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'publish.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    """把各模块各自持有的 settings 都指到临时目录这一份。

    service 要验片段文件在不在盘上，runner 要开临时目录与登记产物，composer 要
    超时时长，两个 store 要大小闸门与 URL 前缀——缺一个，测试就会写进真实 uploads/。
    与 test_videos.py 同一姿势：store 模块持有的是模块级名字，整份替换即可。
    """
    fake = SimpleNamespace(
        upload_media_path=tmp_path,
        UPLOAD_MEDIA_URL="/uploads",
        MEDIA_IMAGE_MAX_BYTES=real_settings.MEDIA_IMAGE_MAX_BYTES,
        MEDIA_VIDEO_MAX_BYTES=real_settings.MEDIA_VIDEO_MAX_BYTES,
        MATERIAL_MAX_IMAGE_BYTES=real_settings.MATERIAL_MAX_IMAGE_BYTES,
        MATERIAL_MAX_VIDEO_BYTES=real_settings.MATERIAL_MAX_VIDEO_BYTES,
        PUBLISH_COMPOSE_TIMEOUT=30.0,
        VIDEO_MAX_SHOTS=real_settings.VIDEO_MAX_SHOTS,
        VIDEO_MAX_PROMPT_CHARS=real_settings.VIDEO_MAX_PROMPT_CHARS,
        # 配音链路新读的几项：夹具里显式给值，缺一个就会把测试写进真实 uploads/
        # 或在滤镜构建时 AttributeError。
        TTS_DEFAULT_VOICE="",
        TTS_MAX_ATEMPO=1.6,
        TTS_SAMPLE_RATE=44100,
        # 拼接层的观感档位按服务端默认给（淡入淡出 + 缓推 + 配音提前），需要验老
        # 的硬切直拼契约时用 hard_cut 夹具归零，而不是在这里写死 0。
        VIDEO_TRANSITION_S=real_settings.VIDEO_TRANSITION_S,
        VIDEO_MOTION_ZOOM=real_settings.VIDEO_MOTION_ZOOM,
        VIDEO_AUDIO_LEAD_S=real_settings.VIDEO_AUDIO_LEAD_S,
        VIDEO_COMPOSE_FPS=real_settings.VIDEO_COMPOSE_FPS,
    )
    monkeypatch.setattr(media_store, "settings", fake)
    monkeypatch.setattr(material_store, "settings", fake)
    monkeypatch.setattr(video_service, "settings", fake)
    monkeypatch.setattr(runner, "settings", fake)
    monkeypatch.setattr(composer, "settings", fake)
    return tmp_path


@pytest.fixture()
def db(engine, monkeypatch):
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    seed_builtin_workflows(session)
    session.add(
        ModelConfig(
            owner_id=OWNER,
            purpose=ModelPurpose.CHAT.value,
            title="测试对话模型",
            model_name="unit-test",
            base_url="http://localhost:1/v1",
            api_key_enc=encrypt("sk-unit-test"),
            is_default=True,
            last_test_ok=True,
        )
    )
    session.commit()
    monkeypatch.setattr("app.database.SessionLocal", maker)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: CurrentUser(user_id=OWNER, username="u1")
    try:
        # 不进上下文管理器：lifespan 不跑（启动自检要连真库），与 test_videos.py 同姿势。
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _service(db) -> VideoService:
    return VideoService(db, OWNER)


def _project(db) -> VideoProject:
    return _service(db).create_project(
        title="季度复盘三步",
        platform="douyin",
        idea="教普通打工人三步做出老板看得懂的季度复盘",
        duration_s=None,
        requirement=None,
        style=None,
        params=None,
    )


def _settle_script_run(db, project_id: int) -> None:
    """建项目即认领的脚本运行置成功：删除闸门只放终态运行过去。"""
    run = db.scalar(
        select(WorkflowRun).where(WorkflowRun.subject_ref == str(project_id))
    )
    run.status = "succeeded"
    db.commit()


def _shot_overrides() -> dict:
    return {
        "duration_s": 5,
        "scene": "办公室工位",
        "camera": "推镜",
        "action": "主角抬头",
        "dialogue": "",
        "on_screen_text": "三步",
        "image_prompt": "竖屏，年轻职员坐在工位上抬头看镜头，暖光",
        "video_prompt": "镜头缓慢推近，人物眨眼",
        "gen_mode": "first_frame",
        "material_ids": [],
    }


def _shots(db, project_id: int, count: int) -> list[VideoShot]:
    service = _service(db)
    service.save_shots(project_id, [_shot_overrides() for _ in range(count)])
    return service.shots_of(project_id)


def _clip_ready(db, project_id: int, shot: VideoShot, *, raw: bytes = MP4) -> MediaJob:
    """给镜头挂一条成功的视频片段作业，产物真的落在临时盘上。"""
    stored = media_store.store_blob(project_id, raw)
    job = MediaJob(
        owner_id=OWNER,
        provider="ark",
        kind=KIND_VIDEO,
        biz_type=BIZ_VIDEO_SHOT,
        biz_id=str(shot.id),
        model_name="unit-test-video",
        status=JOB_SUCCEEDED,
        request={},
        rel_path=stored.rel_path,
        mime=stored.mime,
        file_size=stored.size,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    shot.video_job_id = job.id
    db.commit()
    return job


def _all_clips_ready(db, project_id: int) -> list[VideoShot]:
    rows = _shots(db, project_id, 3)
    for shot in rows:
        _clip_ready(db, project_id, shot)
    return rows


# ---- 入队校验 ----------------------------------------------------------------


def test_compose_rejects_when_no_shots(db):
    project = _project(db)
    with pytest.raises(ValueError, match="没有镜头"):
        _service(db).compose_publish(project.id)


def test_compose_reports_every_shot_missing_a_clip(db):
    project = _project(db)
    rows = _shots(db, project.id, 2)
    # 只给第一镜挂片段：报错要同时点出第 2 镜，而不是拼到一半才发现。
    _clip_ready(db, project.id, rows[0])

    with pytest.raises(ValueError, match="第 2 镜"):
        _service(db).compose_publish(project.id)


def test_compose_rejects_when_clip_file_gone(db):
    project = _project(db)
    rows = _all_clips_ready(db, project.id)
    stored = db.get(MediaJob, rows[0].video_job_id).rel_path
    (media_store.settings.upload_media_path / stored).unlink()

    with pytest.raises(ValueError, match="第 1 镜"):
        _service(db).compose_publish(project.id)


def test_compose_draft_skips_missing_shots_and_records_them(db):
    """草稿片：缺片段的镜头跳过，sources 只含就绪片段，跳过镜号留痕。"""
    project = _project(db)
    rows = _shots(db, project.id, 3)
    _clip_ready(db, project.id, rows[0])
    _clip_ready(db, project.id, rows[2])

    # 严格口径不变：缺第 2 镜仍然整条拒绝。
    with pytest.raises(ValueError, match="第 2 镜"):
        _service(db).compose_publish(project.id)

    job = _service(db).compose_publish(project.id, allow_missing=True)
    assert job.status == PUBLISH_QUEUED
    assert job.request["skipped_shots"] == [2]
    assert len(job.request["sources"]) == 2
    # 没开配音时请求里不带 tts 键。
    assert "tts" not in job.request


def test_compose_draft_needs_at_least_one_ready_clip(db):
    """一条成功片段都没有时，草稿模式也不能拼空气。"""
    project = _project(db)
    _shots(db, project.id, 2)
    with pytest.raises(ValueError, match="成功的视频片段"):
        _service(db).compose_publish(project.id, allow_missing=True)


def test_compose_draft_recomputes_tts_offsets_for_kept_shots(db):
    """跳过中间镜头后，保留镜头的配音起点按新序列重算，不能沿用全片 start_s。"""
    project = _project(db)
    _tts_config(db)
    rows = _all_clips_with_dialogue(db, project.id, ["第一句", "第二句", "第三句"])
    # 让第 2 镜的片段文件消失，它在草稿片里被跳过。
    stored = db.get(MediaJob, rows[1].video_job_id).rel_path
    (media_store.settings.upload_media_path / stored).unlink()
    _enable_tts(db, project, voice="zh_female_1")

    job = _service(db).compose_publish(project.id, allow_missing=True)
    lines = job.request["tts"]["lines"]
    # 第 2 镜整镜不在片里，它的台词也不进快照。
    assert [line["sort_order"] for line in lines] == [1, 3]
    by_order = {line["sort_order"]: line for line in lines}
    # 每镜 5 秒：镜 1 从头（0），镜 3 在新序列里是第二段（5），而不是全片里的 10。
    assert by_order[1]["start_s"] == 0
    assert by_order[3]["start_s"] == 5
    assert job.request["skipped_shots"] == [2]


def test_compose_enqueues_ordered_sources_and_publish_snapshot(db):
    project = _project(db)
    project.publish = {"title_candidates": ["就用这个标题"], "tags": ["复盘", "职场"]}
    db.commit()
    rows = _all_clips_ready(db, project.id)
    expected = [db.get(MediaJob, shot.video_job_id).rel_path for shot in rows]

    job = _service(db).compose_publish(project.id)

    assert job.status == PUBLISH_QUEUED
    assert job.channel == "manual"
    assert job.platform == "douyin"
    # 顺序就是镜头顺序：concat 不会再排序，这份快照决定成片的时间轴。
    assert job.request["sources"] == expected
    assert job.request["title"] == "就用这个标题"
    assert job.request["tags"] == ["复盘", "职场"]


def test_compose_refuses_a_second_active_job(db):
    project = _project(db)
    _all_clips_ready(db, project.id)
    _service(db).compose_publish(project.id)

    with pytest.raises(ValueError, match="已有成片合成在进行"):
        _service(db).compose_publish(project.id)


def test_detail_carries_the_latest_publish_job(db):
    project = _project(db)
    _all_clips_ready(db, project.id)
    job = _service(db).compose_publish(project.id)

    detail = _service(db).to_detail(_service(db).get_project(project.id))

    assert detail.publish_job is not None
    assert detail.publish_job.id == job.id
    assert detail.publish_job.status == PUBLISH_QUEUED
    # 还没合成成功时不给地址，前端按空串显示「未合成」。
    assert detail.publish_job.url == ""


# ---- 执行体 ------------------------------------------------------------------


def test_runner_composes_registers_and_succeeds(db, media_dir, monkeypatch):
    project = _project(db)
    rows = _all_clips_ready(db, project.id)
    sources = [db.get(MediaJob, shot.video_job_id).rel_path for shot in rows]
    job = _service(db).compose_publish(project.id)

    seen: list[dict] = []

    async def fake_compose(actual_sources, out_path: Path, voiceover=None, *, progress=None):
        # 合成器收到的顺序要与入队快照一致；产物写到调用方给的临时路径。
        assert [p.name for p in actual_sources] == [Path(rel).name for rel in sources]
        # 没开配音的既有链路：计划必须是 None，行为与配音功能上线前一致。
        assert voiceover is None
        await progress(ComposeProgress("concat", 55, "正在直接拼接片段"))
        seen.append(dict(db.get(PublishJob, job.id).request["progress"]))
        out_path.write_bytes(MP4 + b"x" * 64)

    monkeypatch.setattr(runner, "compose", fake_compose)

    asyncio.run(runner.run_publish_job(db, job.id))

    db.expire_all()
    done = db.get(PublishJob, job.id)
    assert done.status == PUBLISH_SUCCEEDED
    assert done.error_msg is None
    assert done.finished_at is not None
    assert done.elapsed_ms is not None
    # 产物经 store 登记：随机命名、在项目目录里、URL 拼得出来、文件真在盘上。
    assert media_store.is_safe_rel_path(done.rel_path)
    assert (media_dir / done.rel_path).is_file()
    assert done.file_size == (media_dir / done.rel_path).stat().st_size
    assert seen == [
        {"stage": "concat", "percent": 55, "detail": "正在直接拼接片段", "current": None, "total": None}
    ]
    assert done.request["progress"] == {
        "stage": "succeeded",
        "percent": 100,
        "detail": "成片已保存",
        "current": None,
        "total": None,
    }
    detail = _service(db).to_detail(_service(db).get_project(project.id))
    assert detail.publish_job.url.endswith(done.rel_path)
    assert detail.publish_job.progress is not None
    assert detail.publish_job.progress.percent == 100


def test_runner_marks_failure_instead_of_raising(db, monkeypatch):
    project = _project(db)
    _all_clips_ready(db, project.id)
    job = _service(db).compose_publish(project.id)

    async def boom(sources, out_path, voiceover=None, *, progress=None):
        await progress(ComposeProgress("reencode", 47, "正在统一重编码"))
        raise PublishComposeError("ffmpeg 合成失败：模拟故障")

    monkeypatch.setattr(runner, "compose", boom)

    asyncio.run(runner.run_publish_job(db, job.id))  # 不抛

    db.expire_all()
    done = db.get(PublishJob, job.id)
    assert done.status == PUBLISH_FAILED
    assert "模拟故障" in (done.error_msg or "")
    assert done.request["progress"]["stage"] == "failed"
    assert done.request["progress"]["percent"] == 47
    assert done.finished_at is not None
    # 失败是终态：同一个项目可以立刻重新发起，不被活跃闸门拦住。
    assert _service(db).compose_publish(project.id).status == PUBLISH_QUEUED


def test_runner_fails_when_source_disappeared(db, media_dir):
    project = _project(db)
    rows = _all_clips_ready(db, project.id)
    job = _service(db).compose_publish(project.id)
    for shot in rows:
        rel = db.get(MediaJob, shot.video_job_id).rel_path
        (media_dir / rel).unlink()

    asyncio.run(runner.run_publish_job(db, job.id))

    db.expire_all()
    assert db.get(PublishJob, job.id).status == PUBLISH_FAILED


def test_reset_stale_jobs_marks_interrupted_rows_failed(db):
    project = _project(db)
    _all_clips_ready(db, project.id)
    job = _service(db).compose_publish(project.id)
    job.status = PUBLISH_RUNNING
    db.commit()

    assert runner.reset_stale_publish_jobs(db) == 1

    db.expire_all()
    done = db.get(PublishJob, job.id)
    assert done.status == PUBLISH_FAILED
    assert "重启" in (done.error_msg or "")


# ---- 删除闸门 ----------------------------------------------------------------


def test_delete_project_blocked_while_composing(db):
    project = _project(db)
    _settle_script_run(db, project.id)
    _all_clips_ready(db, project.id)
    _service(db).compose_publish(project.id)

    with pytest.raises(ValueError, match="成片还在合成中"):
        _service(db).delete_project(project.id)


def test_delete_project_removes_terminal_publish_jobs(db):
    project = _project(db)
    _settle_script_run(db, project.id)
    _all_clips_ready(db, project.id)
    job = _service(db).compose_publish(project.id)
    job.status = PUBLISH_SUCCEEDED
    db.commit()

    assert _service(db).delete_project(project.id) == project.id
    assert db.scalar(select(func.count(PublishJob.id))) == 0


# ---- 合成器（ffmpeg 调用形状） ------------------------------------------------


@pytest.fixture()
def fake_ffmpeg(monkeypatch):
    """合成器单测不依赖 imageio_ffmpeg 的轮子：这里验的是参数形状与回退逻辑，
    不是那个二进制本身。真实运行时的可执行路径由 requirements 里钉死的包提供。"""
    monkeypatch.setattr(composer, "ffmpeg_exe", lambda: "ffmpeg")


@pytest.fixture
def hard_cut(monkeypatch):
    """把合成层的观感滤镜调回 0，验「硬切直拼」那一档的原有契约。

    默认档位现在是淡入淡出 + 缓推 + 配音提前，那些行为另有专门的测试；留在这几行的
    是「参数形状与回退顺序」，它们只在关掉滤镜时才走 `-c copy` / concat demuxer。
    """
    monkeypatch.setattr(composer.settings, "VIDEO_TRANSITION_S", 0.0)
    monkeypatch.setattr(composer.settings, "VIDEO_MOTION_ZOOM", 0.0)
    monkeypatch.setattr(composer.settings, "VIDEO_AUDIO_LEAD_S", 0.0)


class _FakeProc:
    def __init__(self, code: int, action=None):
        self._code = code
        self._action = action

    async def communicate(self):
        if self._action:
            self._action()
        return b"", b"fake ffmpeg stderr"

    @property
    def returncode(self):
        return self._code

    async def kill(self):
        pass


class _HangingProc:
    def __init__(self):
        self.killed = False
        self.returncode = None
        self.communicate_calls = 0
        self._release = asyncio.Event()

    async def communicate(self):
        self.communicate_calls += 1
        if not self.killed:
            await self._release.wait()
        return b"", b"encoder stalled"

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._release.set()

    async def wait(self):
        return self.returncode


def test_ffmpeg_timeout_reports_stage_and_drains_stderr(monkeypatch):
    process = _HangingProc()

    async def fake_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(composer.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(composer.settings, "PUBLISH_COMPOSE_TIMEOUT", 0.001)

    with pytest.raises(PublishComposeError, match="ffmpeg 配音混音超过") as exc_info:
        asyncio.run(composer._run_ffmpeg(["ffmpeg"], stage="配音混音"))

    assert process.killed
    assert process.communicate_calls == 1
    assert "encoder stalled" in str(exc_info.value)


class _MemoryStream:
    def __init__(self, data: bytes):
        self._lines = iter(data.splitlines(keepends=True))

    async def readline(self) -> bytes:
        return next(self._lines, b"")


class _ProgressProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", code: int = 0):
        self.returncode = code
        self.stdout = _MemoryStream(stdout)
        self.stderr = _MemoryStream(stderr)

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_ffmpeg_progress_uses_output_time_and_finishes_at_one(monkeypatch):
    process = _ProgressProc(
        b"out_time_us=2500000\nout_time_us=7500000\nprogress=continue\n",
        _duration_stderr(10.0),
    )
    invoked: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        invoked.append(list(args))
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        return process

    monkeypatch.setattr(composer.asyncio, "create_subprocess_exec", fake_exec)
    fractions: list[float] = []

    async def report(fraction: float) -> None:
        fractions.append(fraction)

    async def run():
        return await composer._run_ffmpeg(
            ["ffmpeg", "-loglevel", "error", "-i", "in.mp4", "out.mp4"],
            stage="片段重编码",
            on_progress=report,
        )

    code, stderr = asyncio.run(run())

    assert code == 0
    assert "Duration:" in stderr
    assert fractions == [0.25, 0.75, 1.0]
    assert "-progress" in invoked[0]
    assert "pipe:1" in invoked[0]
    assert invoked[0][invoked[0].index("-loglevel") + 1] == "info"


def test_concat_list_uses_forward_slashes_and_quotes(tmp_path):
    bad = tmp_path / "a'b" / "clip one.mp4"
    list_file = tmp_path / "concat.txt"
    write_concat_list([bad], list_file)

    content = list_file.read_text(encoding="utf-8")
    assert "\\\\" not in content, "Windows 反斜杠在 concat 语法里是转义符，必须转正斜杠"
    assert "'\\''" in content, "单引号要按 concat 规则转义"
    assert content.startswith("file '")


def test_compose_copy_path_writes_output(tmp_path, monkeypatch, fake_ffmpeg, hard_cut):
    out = tmp_path / "final.mp4"

    def fake_exec(*args, **kwargs):
        async def run():
            # 输出路径是最后一个参数：模拟直拼成功落盘。
            Path(args[-1]).write_bytes(MP4)
            return _FakeProc(0)

        return run()

    monkeypatch.setattr(composer.asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(composer.compose([tmp_path / "a.mp4", tmp_path / "b.mp4"], out))

    assert out.is_file()


def test_compose_falls_back_to_reencode_then_succeeds(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    out = tmp_path / "final.mp4"
    calls: list[list[str]] = []

    def fake_exec(*args, **kwargs):
        calls.append(list(args))

        async def run():
            if len(calls) == 1:
                return _FakeProc(1)  # 直拼被参数不一致挡回
            Path(args[-1]).write_bytes(MP4)
            return _FakeProc(0)

        return run()

    monkeypatch.setattr(composer.asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(composer.compose([tmp_path / "a.mp4"], out))

    assert len(calls) == 2
    assert "libx264" in calls[1], "第二次必须是重编码档"
    assert "-an" in calls[1], "本期不做口播，回退档统一不要音轨"


def test_compose_reports_real_concat_progress_range(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    out = tmp_path / "final.mp4"
    updates: list[ComposeProgress] = []

    async def fake_run(args, *, stage="处理", on_progress=None, duration_s=None):
        if on_progress is not None:
            await on_progress(0.5)
            await on_progress(1.0)
        Path(args[-1]).write_bytes(MP4)
        return 0, ""

    monkeypatch.setattr(composer, "_run_ffmpeg", fake_run)

    async def report(update: ComposeProgress) -> None:
        updates.append(update)

    asyncio.run(
        composer.compose(
            [tmp_path / "a.mp4", tmp_path / "b.mp4"],
            out,
            progress=report,
        )
    )

    assert [update.percent for update in updates] == [5, 49, 92]
    assert all(update.stage == "concat" for update in updates)
    assert all(update.total == 2 for update in updates)


def test_compose_raises_when_both_attempts_fail(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    def fake_exec(*args, **kwargs):
        async def run():
            return _FakeProc(1)

        return run()

    monkeypatch.setattr(composer.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(PublishComposeError, match="ffmpeg 合成失败"):
        asyncio.run(composer.compose([tmp_path / "a.mp4"], tmp_path / "final.mp4"))


def test_styled_filter_fades_interior_cuts_and_pushes_every_shot():
    """镜头边界的淡出淡入只加在**中间**切点，推近每一镜都要有。

    滤镜图在这里单独验形状：线上跑的是 imageio-ffmpeg 自带的 FFmpeg 4.2.2，没有
    xfade，所以淡入淡出走 ``fade``、运动兜底走 ``zoompan``，这两样都是老滤镜。
    """
    graph = composer._styled_filter(
        3, [4.0, 5.0, 4.0], 720, 1280, transition_s=0.4, motion_zoom=0.05, fps=24
    )
    chains = graph.split(";")[:3]
    first, second, last = chains
    # 第一镜不淡入：片子不该以黑场开场；它的尾巴照样淡出到切点。
    assert "fade=t=in" not in first
    assert "fade=t=out:st=3.800:d=0.200" in first
    assert "fade=t=in:st=0:d=0.200" in second and "fade=t=out:st=4.800:d=0.200" in second
    # 最后一镜不淡出：结尾的黑留给平台/播放器。
    assert "fade=t=in:st=0:d=0.200" in last and "fade=t=out" not in last
    # 推近按帧数摊开（5 秒 24fps = 120 帧 → 除 119），且先进两倍图再输出回目标画幅。
    assert "zoompan=z='1+0.05*on/119'" in second
    assert "scale=1440:2560" in second and ":s=720x1280:fps=24" in second
    assert graph.endswith("[v0][v1][v2]concat=n=3:v=1:a=0[outv]")


def test_styled_filter_without_motion_zoom_only_letterboxes():
    graph = composer._styled_filter(
        1, [4.0], 720, 1280, transition_s=0.4, motion_zoom=0.0, fps=24
    )
    assert "zoompan" not in graph and "fade" not in graph
    assert "pad=720:1280" in graph and graph.endswith("[outv]")


def test_compose_default_stage_encodes_with_transition(tmp_path, monkeypatch, fake_ffmpeg):
    """默认档位不再走 `-c copy` 流拷贝：转场是逐帧滤镜，只能整段重编码。"""
    sources = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in sources:
        path.write_bytes(MP4)
    out = tmp_path / "final.mp4"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, {"a.mp4": 4.0, "b.mp4": 4.0}),
    )

    asyncio.run(composer.compose(sources, out))

    assert out.is_file()
    assert not any("concat" in argv for argv in calls), "不该再出现 concat demuxer 直拼"
    encode = next(argv for argv in calls if "-filter_complex" in argv)
    graph = encode[encode.index("-filter_complex") + 1]
    assert "zoompan" in graph and "fade=t=in" in graph and "concat=n=2:v=1:a=0" in graph
    assert encode[encode.index("-c:v") + 1] == "libx264"
    assert "-an" in encode, "配音在后面的槽位串拼里装配，这一档不造音轨"


def test_compose_single_shot_still_gets_motion_floor(tmp_path, monkeypatch, fake_ffmpeg):
    """一镜一条片子：没有切点可转场，但静止兜底的推近还是要给。"""
    source = tmp_path / "a.mp4"
    source.write_bytes(MP4)
    out = tmp_path / "final.mp4"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        composer.asyncio, "create_subprocess_exec", _routing_exec(calls, {"a.mp4": 4.0})
    )

    asyncio.run(composer.compose([source], out))

    encode = next(argv for argv in calls if "-filter_complex" in argv)
    assert "zoompan" in encode[encode.index("-filter_complex") + 1]


def test_build_voiceover_leads_lines_after_the_first(tmp_path, monkeypatch, fake_ffmpeg):
    """J-cut：第二句起每句都比画面起点提前 ``VIDEO_AUDIO_LEAD_S``，提前量从第一句尾部扣。"""
    plan = VoiceoverPlan(_voice_lines(tmp_path))  # (start 0, slot 5) + (start 5, slot 5)
    out = tmp_path / "voiceover.m4a"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, {"line_0.mp3": 3.0, "line_1.mp3": 4.0}),
    )

    lead = float(composer.settings.VIDEO_AUDIO_LEAD_S)
    asyncio.run(composer.build_voiceover("ffmpeg", plan, total_s=10.0, out_path=out))

    graph = next(
        argv[argv.index("-filter_complex") + 1]
        for argv in calls
        if "-filter_complex" in argv
    )
    assert f"apad,atrim=0:{5 - lead:.3f}" in graph.split("[s0]")[0], "第一句让出提前量"
    assert f"apad,atrim=0:5.000" in graph.split("[s1]")[0], "后面每句仍是整槽"
    # 两句仍然首尾相接：既不重叠（没有插静音补位），也不能靠 adelay/amix 错位。
    assert "[s0][s1]concat=n=2:v=0:a=1[joined]" in graph
    assert "adelay" not in graph and "amix" not in graph


def test_build_voiceover_first_line_overflow_reports_shrunken_slot(tmp_path, monkeypatch, fake_ffmpeg):
    """第一句把槽位说满时，报的是扣掉提前量之后的预算，不是原槽长。"""
    plan = VoiceoverPlan(_voice_lines(tmp_path))
    out = tmp_path / "voiceover.m4a"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, {"line_0.mp3": 9.0, "line_1.mp3": 3.0}),
    )

    with pytest.raises(PublishComposeError, match="镜头槽位只有 4.8 秒"):
        asyncio.run(composer.build_voiceover("ffmpeg", plan, total_s=10.0, out_path=out))


# ---- HTTP 端到端 --------------------------------------------------------------
def test_compose_endpoint_runs_the_background_job(client, db, monkeypatch):
    project = _project(db)
    _all_clips_ready(db, project.id)

    async def fake_compose(sources, out_path: Path, voiceover=None, *, progress=None):
        out_path.write_bytes(MP4)

    monkeypatch.setattr(runner, "compose", fake_compose)

    resp = client.post(f"/api/office/videos/{project.id}/compose")
    body = resp.json()
    assert body["code"] == 0
    # 响应体在 BackgroundTasks 开跑前就序列化了：回来的这一刻只能是排队中。
    assert body["data"]["publish_job"]["status"] == PUBLISH_QUEUED
    assert body["data"]["publish_job"]["url"] == ""
    assert body["data"]["publish_job"]["progress"] is None

    # 后台任务用另一条 session 提交；本会话先过期身份映射，再走接口看终态。
    db.expire_all()
    detail = client.get(f"/api/office/videos/{project.id}").json()["data"]
    assert detail["publish_job"]["status"] == PUBLISH_SUCCEEDED
    assert detail["publish_job"]["url"]

    assert db.scalar(select(func.count(PublishJob.id))) == 1


def test_compose_endpoint_rejects_missing_clips_with_code_minus_one(client, db):
    project = _project(db)
    _shots(db, project.id, 1)

    body = client.post(f"/api/office/videos/{project.id}/compose").json()

    assert body["code"] == -1
    assert "片段" in body["message"]
    assert db.scalar(select(func.count(PublishJob.id))) == 0


# ---- TTS 入队门槛与快照 ------------------------------------------------------


def _tts_config(db, *, tested: bool = True) -> ModelConfig:
    config = ModelConfig(
        owner_id=OWNER,
        purpose=ModelPurpose.TTS.value,
        title="测试语音模型",
        model_name="tts-unit-test",
        base_url="http://localhost:1/api/v3",
        api_key_enc=encrypt("sk-tts"),
        is_default=True,
        last_test_ok=tested,
    )
    db.add(config)
    db.commit()
    return config


def _shots_with_dialogue(db, project_id: int, texts: list[str]) -> list[VideoShot]:
    overrides = []
    for text in texts:
        item = _shot_overrides()
        item["dialogue"] = text
        overrides.append(item)
    _service(db).save_shots(project_id, overrides)
    return _service(db).shots_of(project_id)


def _enable_tts(db, project: VideoProject, voice: str = "zh_female_1") -> None:
    project.params = {**project.params, "tts_enabled": True, "tts_voice": voice}
    db.commit()


def _all_clips_with_dialogue(db, project_id: int, texts: list[str]) -> list[VideoShot]:
    rows = _shots_with_dialogue(db, project_id, texts)
    for shot in rows:
        _clip_ready(db, project_id, shot)
    return rows


def test_compose_tts_rejected_without_config(db):
    project = _project(db)
    _all_clips_with_dialogue(db, project.id, ["一句台词"])
    _enable_tts(db, project)

    with pytest.raises(ValueError, match="语音"):
        _service(db).compose_publish(project.id)
    assert db.scalar(select(func.count(PublishJob.id))) == 0


def test_compose_tts_rejected_when_config_untested(db):
    project = _project(db)
    _all_clips_with_dialogue(db, project.id, ["一句台词"])
    _tts_config(db, tested=False)
    _enable_tts(db, project)

    with pytest.raises(ValueError, match="连通性测试"):
        _service(db).compose_publish(project.id)


def test_compose_tts_rejected_without_any_voice(db):
    project = _project(db)
    _all_clips_with_dialogue(db, project.id, ["一句台词"])
    _tts_config(db)
    _enable_tts(db, project, voice="")  # 夹具里 TTS_DEFAULT_VOICE 也是空串

    with pytest.raises(ValueError, match="音色"):
        _service(db).compose_publish(project.id)


def test_compose_tts_falls_back_to_server_default_voice(db):
    project = _project(db)
    _all_clips_with_dialogue(db, project.id, ["一句台词"])
    _tts_config(db)
    _enable_tts(db, project, voice="")
    # 夹具给的是 SimpleNamespace，单测内改属性不影响别的用例。
    video_service.settings.TTS_DEFAULT_VOICE = "zh_server_default"

    job = _service(db).compose_publish(project.id)

    assert job.request["tts"]["voice"] == "zh_server_default"


def test_compose_tts_rejected_when_every_dialogue_empty(db):
    project = _project(db)
    _all_clips_ready(db, project.id)  # 默认 overrides 的 dialogue 全是空串
    _tts_config(db)
    _enable_tts(db, project)

    with pytest.raises(ValueError, match="台词都是空的"):
        _service(db).compose_publish(project.id)


def test_compose_tts_snapshot_keeps_only_spoken_shots(db):
    project = _project(db)
    rows = _all_clips_with_dialogue(db, project.id, ["第一句", "", "第三句"])
    assert [row.sort_order for row in rows] == [1, 2, 3]
    _tts_config(db)
    _enable_tts(db, project)

    job = _service(db).compose_publish(project.id)

    snapshot = job.request["tts"]
    assert snapshot["enabled"] is True
    assert snapshot["voice"] == "zh_female_1"
    assert snapshot["model_name"] == "tts-unit-test"
    # 无台词的第二镜不进快照；起点按镜头时间轴（每镜 5 秒）累加。
    assert [line["sort_order"] for line in snapshot["lines"]] == [1, 3]
    assert [line["start_s"] for line in snapshot["lines"]] == [0, 10]
    assert [line["duration_s"] for line in snapshot["lines"]] == [5, 5]
    assert [line["text"] for line in snapshot["lines"]] == ["第一句", "第三句"]


def test_compose_without_tts_has_no_tts_key(db):
    project = _project(db)
    _all_clips_ready(db, project.id)

    job = _service(db).compose_publish(project.id)

    assert "tts" not in job.request


# ---- TTS 执行体 --------------------------------------------------------------


def test_runner_synthesizes_lines_and_passes_voiceover_plan(db, monkeypatch):
    project = _project(db)
    rows = _all_clips_with_dialogue(db, project.id, ["第一句", "", "第三句"])
    _tts_config(db)
    _enable_tts(db, project)
    job = _service(db).compose_publish(project.id)

    async def fake_synthesize(config, *, text, voice, **kwargs):
        assert voice == "zh_female_1"
        # 假音频字节即可：runner 只负责落盘，真正的 ffmpeg 在这一档被 mock 掉。
        return MP4 + text.encode("utf-8")

    captured: dict = {}

    async def fake_compose(sources, out_path: Path, voiceover=None, *, progress=None):
        captured["plan"] = voiceover
        captured["progress"] = dict(db.get(PublishJob, job.id).request["progress"])
        # 临时目录在 with 块退出即删，音频文件存在性只能在这一档验。
        assert all(line.path.is_file() for line in voiceover.lines)
        out_path.write_bytes(MP4)

    monkeypatch.setattr(runner.tts, "synthesize", fake_synthesize)
    monkeypatch.setattr(runner, "compose", fake_compose)

    asyncio.run(runner.run_publish_job(db, job.id))

    db.expire_all()
    done = db.get(PublishJob, job.id)
    assert done.status == PUBLISH_SUCCEEDED
    plan = captured["plan"]
    assert isinstance(plan, VoiceoverPlan)
    assert [line.sort_order for line in plan.lines] == [1, 3]
    assert [line.start_s for line in plan.lines] == [0.0, 10.0]
    assert [line.slot_s for line in plan.lines] == [5.0, 5.0]
    assert captured["progress"] == {
        "stage": "tts",
        "percent": 30,
        "detail": "已完成 2/2 句配音",
        "current": 2,
        "total": 2,
    }


def test_runner_fails_when_tts_config_disappeared(db, monkeypatch):
    project = _project(db)
    _all_clips_with_dialogue(db, project.id, ["第一句"])
    config = _tts_config(db)
    _enable_tts(db, project)
    job = _service(db).compose_publish(project.id)
    # 入队后配置被删：运行时重新解析默认项，作业必须明确失败而不是退回无声片。
    db.delete(config)
    db.commit()

    asyncio.run(runner.run_publish_job(db, job.id))

    db.expire_all()
    done = db.get(PublishJob, job.id)
    assert done.status == PUBLISH_FAILED
    assert "语音" in (done.error_msg or "")


def test_runner_marks_tts_error_on_the_job(db, monkeypatch):
    project = _project(db)
    _all_clips_with_dialogue(db, project.id, ["第一句"])
    _tts_config(db)
    _enable_tts(db, project)
    job = _service(db).compose_publish(project.id)

    async def broken_synthesize(config, *, text, voice, **kwargs):
        raise TtsError("上游服务异常（500），稍后重新发起")

    monkeypatch.setattr(runner.tts, "synthesize", broken_synthesize)

    asyncio.run(runner.run_publish_job(db, job.id))

    db.expire_all()
    done = db.get(PublishJob, job.id)
    assert done.status == PUBLISH_FAILED
    assert "500" in (done.error_msg or "")


# ---- 合成器：时长探针与配音滤镜 ----------------------------------------------


class _Proc:
    """可定制 stderr 的假 ffmpeg 进程；探针靠 stderr 里的 Duration 行读数。"""

    def __init__(self, code: int, stderr: bytes = b"fake ffmpeg stderr", action=None):
        self._code = code
        self._stderr = stderr
        self._action = action

    async def communicate(self):
        if self._action:
            self._action()
        return b"", self._stderr

    @property
    def returncode(self):
        return self._code

    async def kill(self):
        pass


def _duration_stderr(seconds: float, *, size: str = "1280x720") -> bytes:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    # 带一条 Stream 行：探测画面尺寸靠的就是它，形状照 ffmpeg 的真实输出抄。
    return (
        f"  Duration: {hours:02d}:{minutes:02d}:{secs:05.2f}, start: 0.000000, bitrate: 100 kb/s\n"
        f"  Stream #0:0(und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, {size}, "
        f"SAR 1:1 DAR 16:9, 24 fps, 24 tbr, 12288 tbn"
    ).encode()


def _named_duration(durations: dict[str, float], name: str) -> bytes:
    """探针用的假 stderr：没登记过的文件按 4 秒回，别让新滤镜链的探测次数卡住老用例。"""
    return _duration_stderr(durations.get(name, 4.0))


def test_probe_duration_parses_ffmpeg_stderr(tmp_path, monkeypatch, fake_ffmpeg):
    target = tmp_path / "a.mp3"
    target.write_bytes(MP4)

    def fake_exec(*args, **kwargs):
        async def run():
            return _Proc(1, _duration_stderr(62.5))  # 无输出文件时 ffmpeg 退出码就是 1

        return run()

    monkeypatch.setattr(composer.asyncio, "create_subprocess_exec", fake_exec)

    assert asyncio.run(composer.probe_duration("ffmpeg", target)) == 62.5


def test_probe_duration_raises_when_line_missing(tmp_path, monkeypatch, fake_ffmpeg):
    target = tmp_path / "a.mp3"
    target.write_bytes(MP4)

    def fake_exec(*args, **kwargs):
        async def run():
            return _Proc(1, b"some unrelated stderr")

        return run()

    monkeypatch.setattr(composer.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(PublishComposeError, match="读不到文件时长"):
        asyncio.run(composer.probe_duration("ffmpeg", target))


def _voice_lines(tmp_path: Path) -> list[VoiceoverLine]:
    lines = []
    for index, (start, slot) in enumerate(((0, 5), (5, 5))):
        audio = tmp_path / f"line_{index}.mp3"
        audio.write_bytes(MP4)
        lines.append(
            VoiceoverLine(audio, sort_order=index + 1, start_s=start, slot_s=slot)
        )
    return lines


def _routing_exec(calls: list, durations: dict[str, float]):
    """按调用形状路由的假 ffmpeg：探针回 Duration，其余落产物文件。"""

    def fake_exec(*args, **kwargs):
        argv = list(args)
        calls.append(argv)

        async def run():
            if argv[-2] == "-i":  # 探针调用：[exe, -nostdin, -hide_banner, -i, path]
                name = Path(argv[-1]).name
                return _Proc(1, _named_duration(durations, name))
            Path(argv[-1]).write_bytes(MP4)
            return _Proc(0)

        return run()

    return fake_exec


def test_build_voiceover_places_lines_without_atempo_when_they_fit(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    plan = VoiceoverPlan(_voice_lines(tmp_path))
    out = tmp_path / "voiceover.m4a"
    calls: list[list[str]] = []
    durations = {"line_0.mp3": 3.0, "line_1.mp3": 4.0}
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, durations),
    )

    asyncio.run(composer.build_voiceover("ffmpeg", plan, total_s=10.0, out_path=out))

    assert out.is_file()
    mix_call = next(argv for argv in calls if "-filter_complex" in argv)
    graph = mix_call[mix_call.index("-filter_complex") + 1]
    # 都放得进 5 秒槽位：不该出现加速；每句先补/截到槽位，再按镜头顺序串拼。
    assert "atempo" not in graph
    assert "[0:a]apad,atrim=0:5.000,aresample=44100,aformat=channel_layouts=stereo[s0]" in graph
    assert "[1:a]apad,atrim=0:5.000,aresample=44100,aformat=channel_layouts=stereo[s1]" in graph
    assert "[s0][s1]concat=n=2:v=0:a=1[joined]" in graph
    # FFmpeg 4.2.2 的 adelay + amix=duration=longest 会卡到超时，绝不能回退。
    assert "adelay" not in graph
    assert "amix" not in graph
    assert "normalize=" not in graph
    assert "atrim=0:10.000" in graph
    assert "aresample=44100" in graph


def test_build_voiceover_preserves_silent_shot_gaps(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    first, second = _voice_lines(tmp_path)
    second = VoiceoverLine(second.path, second.sort_order, start_s=8.0, slot_s=5.0)
    out = tmp_path / "voiceover.m4a"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, {"line_0.mp3": 3.0, "line_1.mp3": 4.0}),
    )

    asyncio.run(
        composer.build_voiceover(
            "ffmpeg", VoiceoverPlan([first, second]), total_s=13.0, out_path=out
        )
    )

    graph = next(
        argv[argv.index("-filter_complex") + 1]
        for argv in calls
        if "-filter_complex" in argv
    )
    assert "anullsrc=channel_layout=stereo:sample_rate=44100,atrim=0:3.000[g0]" in graph
    assert "[s0][g0][s1]concat=n=3:v=0:a=1[joined]" in graph
    assert "atrim=0:13.000" in graph


def test_build_voiceover_speeds_up_overflowing_line(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    plan = VoiceoverPlan(_voice_lines(tmp_path))
    out = tmp_path / "voiceover.m4a"
    calls: list[list[str]] = []
    # 第一句 8 秒塞进 5 秒槽：倍率 1.6，恰好压在上限，允许；第二句正常。
    durations = {"line_0.mp3": 8.0, "line_1.mp3": 4.0}
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, durations),
    )

    asyncio.run(composer.build_voiceover("ffmpeg", plan, total_s=10.0, out_path=out))

    graph = next(
        argv[argv.index("-filter_complex") + 1]
        for argv in calls
        if "-filter_complex" in argv
    )
    # 先加速再截到槽位；concat 本身按顺序定位，不再叠加绝对起点延迟。
    assert "[0:a]atempo=1.6000,apad,atrim=0:5.000" in graph
    assert "[1:a]apad,atrim=0:5.000" in graph
    assert "adelay" not in graph


def test_build_voiceover_avoids_amix_longest_eof_hang(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    plan = VoiceoverPlan(_voice_lines(tmp_path))
    out = tmp_path / "voiceover.m4a"
    calls: list[list[str]] = []
    durations = {"line_0.mp3": 3.0, "line_1.mp3": 4.0}
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, durations),
    )

    asyncio.run(composer.build_voiceover("ffmpeg", plan, total_s=10.0, out_path=out))

    graph = next(
        argv[argv.index("-filter_complex") + 1]
        for argv in calls
        if "-filter_complex" in argv
    )
    assert "concat=n=2:v=0:a=1" in graph
    assert "amix" not in graph
    assert "duration=longest" not in graph


def test_build_voiceover_fails_when_speedup_exceeds_cap(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    plan = VoiceoverPlan(_voice_lines(tmp_path))
    out = tmp_path / "voiceover.m4a"
    calls: list[list[str]] = []
    # 9 秒 / 5 秒 = 1.8x，超过 1.6 上限：明确失败，让人改台词而不是产出加速怪声。
    durations = {"line_0.mp3": 9.0, "line_1.mp3": 4.0}
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, durations),
    )

    with pytest.raises(PublishComposeError, match="第 1 镜台词太长"):
        asyncio.run(composer.build_voiceover("ffmpeg", plan, total_s=10.0, out_path=out))
    assert not out.exists()


def test_compose_with_voiceover_runs_three_stage_pipeline(
    tmp_path, monkeypatch, fake_ffmpeg, hard_cut
):
    sources = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in sources:
        path.write_bytes(MP4)
    plan = VoiceoverPlan(_voice_lines(tmp_path))
    out = tmp_path / "final.mp4"
    calls: list[list[str]] = []
    durations = {
        "silent.mp4": 10.0,
        "line_0.mp3": 3.0,
        "line_1.mp3": 4.0,
    }
    monkeypatch.setattr(
        composer.asyncio,
        "create_subprocess_exec",
        _routing_exec(calls, durations),
    )

    asyncio.run(composer.compose(sources, out, plan))

    assert out.is_file()
    # 六次调用：concat 直拼、无声片探针、两句探针、混音、封装。
    assert len(calls) == 6
    # 混音调用也带 -map（[aout]），用视频流选择器才能准确定位到封装那一次。
    mux_call = next(argv for argv in calls if "0:v:0" in argv)
    assert any(arg.endswith("silent.mp4") for arg in mux_call)
    assert any(arg.endswith("voiceover.m4a") for arg in mux_call)
    # 封装只做流拷贝，不再重编码视频或音频。
    assert mux_call[mux_call.index("-c:v") + 1] == "copy"
    assert mux_call[mux_call.index("-c:a") + 1] == "copy"
    # 无声中间片与成片是两个文件，失败时能分清是拼接还是混音的锅。
    assert (tmp_path / "silent.mp4").is_file()
