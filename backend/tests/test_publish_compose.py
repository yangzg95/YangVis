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
from app.services.publish.composer import PublishComposeError, write_concat_list
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

    async def fake_compose(actual_sources, out_path: Path):
        # 合成器收到的顺序要与入队快照一致；产物写到调用方给的临时路径。
        assert [p.name for p in actual_sources] == [Path(rel).name for rel in sources]
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
    detail = _service(db).to_detail(_service(db).get_project(project.id))
    assert detail.publish_job.url.endswith(done.rel_path)


def test_runner_marks_failure_instead_of_raising(db, monkeypatch):
    project = _project(db)
    _all_clips_ready(db, project.id)
    job = _service(db).compose_publish(project.id)

    async def boom(sources, out_path):
        raise PublishComposeError("ffmpeg 合成失败：模拟故障")

    monkeypatch.setattr(runner, "compose", boom)

    asyncio.run(runner.run_publish_job(db, job.id))  # 不抛

    db.expire_all()
    done = db.get(PublishJob, job.id)
    assert done.status == PUBLISH_FAILED
    assert "模拟故障" in (done.error_msg or "")
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


def test_concat_list_uses_forward_slashes_and_quotes(tmp_path):
    bad = tmp_path / "a'b" / "clip one.mp4"
    list_file = tmp_path / "concat.txt"
    write_concat_list([bad], list_file)

    content = list_file.read_text(encoding="utf-8")
    assert "\\\\" not in content, "Windows 反斜杠在 concat 语法里是转义符，必须转正斜杠"
    assert "'\\''" in content, "单引号要按 concat 规则转义"
    assert content.startswith("file '")


def test_compose_copy_path_writes_output(tmp_path, monkeypatch, fake_ffmpeg):
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


def test_compose_falls_back_to_reencode_then_succeeds(tmp_path, monkeypatch, fake_ffmpeg):
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


def test_compose_raises_when_both_attempts_fail(tmp_path, monkeypatch, fake_ffmpeg):
    def fake_exec(*args, **kwargs):
        async def run():
            return _FakeProc(1)

        return run()

    monkeypatch.setattr(composer.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(PublishComposeError, match="ffmpeg 合成失败"):
        asyncio.run(composer.compose([tmp_path / "a.mp4"], tmp_path / "final.mp4"))


# ---- HTTP 端到端 --------------------------------------------------------------


def test_compose_endpoint_runs_the_background_job(client, db, monkeypatch):
    project = _project(db)
    _all_clips_ready(db, project.id)

    async def fake_compose(sources, out_path: Path):
        out_path.write_bytes(MP4)

    monkeypatch.setattr(runner, "compose", fake_compose)

    resp = client.post(f"/api/office/videos/{project.id}/compose")
    body = resp.json()
    assert body["code"] == 0
    # 响应体在 BackgroundTasks 开跑前就序列化了：回来的这一刻只能是排队中。
    assert body["data"]["publish_job"]["status"] == PUBLISH_QUEUED
    assert body["data"]["publish_job"]["url"] == ""

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
