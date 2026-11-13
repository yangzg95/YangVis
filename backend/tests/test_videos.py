"""短视频的服务层：分镜清洗、镜头时间轴、首帧入队、删除闸门与序列化。

脚本那条两步流程（模型调用 → 创作卡 + 分镜）在 ``test_workflow_flows.py`` 里测；这里
接在它后面——从「分镜已经落进 ``video_shot``」开始，那一段不碰服务商：``render_frames``
只往 ``media_job`` 写一行 ``queued``，真正的 HTTP 在 ``test_media_ark.py`` 与
``test_media_jobs.py``。产物目录一律指到 pytest 的临时目录，不碰真实部署的 uploads/。

断言的重点始终是**唯一事实源**：状态与产物路径只在作业行上，镜头行只有一个指针，
项目行连指针都不留（封面按 ``biz_type`` + ``biz_id`` 查最新那条成功的）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.crypto import encrypt
from app.database import Base
from app.models.entities import (
    MediaJob,
    ModelConfig,
    VideoProject,
    VideoShot,
    Workflow,
    WorkflowRun,
)
from app.models.schemas import MediaJobOut, ModelPurpose
from app.services.media import (
    BIZ_VIDEO_COVER,
    BIZ_VIDEO_SHOT,
    JOB_CANCELLED,
    JOB_EXPIRED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    KIND_IMAGE,
    KIND_VIDEO,
    MODE_FIRST_FRAME,
    MODE_T2V,
)
from app.services.media import store as media_store
from app.services.materials import store as material_store
from app.services.materials.store import store_material
from app.services.materials.service import MaterialService
from app.services.videos.normalize import MAX_SHOTS_FROM_MODEL, shots_from_model, text_of
from app.services.videos.service import VideoService, _script_inputs
from app.services.workflows import (
    RUN_PENDING,
    SUBJECT_ANALYZING,
    SUBJECT_ERROR,
    SUBJECT_READY,
    SUBJECT_VIDEO_PROJECT,
)
from app.services.workflows.builtin import seed_builtin_workflows

settings = get_settings()
OWNER = 1
OTHER = 2

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32
WEBM = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f" + b"0" * 32
NOT_MEDIA = b"<html><script>alert(1)</script></html>"


@pytest.fixture()
def engine(tmp_path):
    # 文件库：与 flows 测试同一理由——引擎里的步骤自开 session，共享一条连接的内存库
    # 会让它们互相看不见对方写的行。
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'videos.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    """把产物目录与 URL 前缀指到临时目录：断言 cover_url 时不碰真实上传盘。

    素材库落盘用的是它自己模块里那份 settings，所以两个模块都要指过来——镜头引用
    素材时，`render_frames` 会真的按 rel_path 读文件。
    """
    fake = SimpleNamespace(
        upload_media_path=tmp_path,
        UPLOAD_MEDIA_URL="/uploads",
        MATERIAL_MAX_IMAGE_BYTES=settings.MATERIAL_MAX_IMAGE_BYTES,
        MATERIAL_MAX_VIDEO_BYTES=settings.MATERIAL_MAX_VIDEO_BYTES,
    )
    monkeypatch.setattr(media_store, "settings", fake)
    monkeypatch.setattr(material_store, "settings", fake)
    return tmp_path


@pytest.fixture()
def db(engine, monkeypatch):
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    # 内置流程要 seed：create_project 一落行就认领一条运行，那是它状态的唯一来源。
    seed_builtin_workflows(session)
    session.add(
        ModelConfig(
            owner_id=OWNER,
            purpose=ModelPurpose.CHAT.value,
            title="测试对话模型",
            model_name="unit-test",
            base_url="http://localhost:1/v1",
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


def _service(db, owner_id: int = OWNER) -> VideoService:
    return VideoService(db, owner_id)


def _image_model(db, owner_id: int = OWNER, *, tested: bool = True) -> ModelConfig:
    config = ModelConfig(
        owner_id=owner_id,
        purpose=ModelPurpose.IMAGE.value,
        title="测试图片模型",
        model_name="doubao-seedream-5-0-pro-260128",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_enc=encrypt("sk-unit-test"),
        is_default=True,
        last_test_ok=tested,
    )
    db.add(config)
    db.commit()
    return config


def _project(db, **overrides) -> VideoProject:
    fields = {
        "title": "季度复盘三步",
        "platform": "douyin",
        "idea": "教普通打工人三步做出老板看得懂的季度复盘",
        "duration_s": None,
        "requirement": None,
        "style": None,
        "params": None,
    }
    fields.update(overrides)
    return _service(db).create_project(**fields)


def _shot(overrides: dict) -> dict:
    """一条够渲染的分镜：只有 image_prompt 是渲染的硬依赖。"""
    row = {
        "duration_s": 5,
        "scene": "办公室工位",
        "camera": "推镜",
        "action": "主角抬头",
        "dialogue": "季度复盘不是写文档",
        "on_screen_text": "三步",
        "image_prompt": "竖屏，年轻职员坐在工位上抬头看镜头，暖光",
        "video_prompt": "镜头缓慢推近，人物眨眼",
        "gen_mode": MODE_FIRST_FRAME,
        "material_ids": [],
    }
    row.update(overrides)
    return row


def _material(db, name: str = "主角形象.png", raw: bytes = PNG, owner_id: int = OWNER):
    """素材库里的一条真实素材：走落盘与登记，而不是手搓一行数据库记录。

    库是账号级的，所以创建者要与项目 owner 一致才是「本账号的素材」；传 OTHER 就是
    专门造一条越界的引用，看镜头那边认不认。
    """
    return MaterialService(db, owner_id).add(store_material(owner_id, raw), name)


def _shots(db, project_id: int, count: int = 2) -> list[VideoShot]:
    service = _service(db)
    service.save_shots(project_id, [_shot({}) for _ in range(count)])
    return service.shots_of(project_id)


# ---- 创建与字段规范化 --------------------------------------------------------


def test_create_project_lands_a_run_and_default_params(db):
    project = _project(db, title=None, style={"visual_style": "胶片质感"}, duration_s=30)

    # 没起名字就从想法里掐一段：列表页得说得出这是哪个片子。
    assert project.title == "教普通打工人三步做出老板看得懂的季度复盘"[:24]
    assert project.params["generate_audio"] is False, "一期不做口播，方舟的默认开必须显式关掉"
    assert project.params["ratio"] == "9:16", "竖屏是产品前提，不能交给上游的 adaptive"
    assert project.brief["duration_s"] == 30, "用户指定过的时长要活到脚本步里"

    run = db.scalar(select(WorkflowRun).where(WorkflowRun.subject_kind == SUBJECT_VIDEO_PROJECT))
    assert (run.subject_ref, run.status) == (str(project.id), RUN_PENDING)
    assert run.subject_label == project.title


def test_create_project_requires_an_idea(db):
    with pytest.raises(ValueError):
        _service(db).create_project(
            title="只有名字", platform="douyin", idea="   ", duration_s=None,
            requirement=None, style=None, params=None,
        )


def test_update_project_keeps_untouched_fields(db):
    project = _project(db, requirement="别出现真人 logo")
    service = _service(db)

    service.update_project(
        project.id, title="改了标题", params={"ratio": "9:16", "fps": 30, "not_a_param": 1}
    )
    db.refresh(project)

    assert project.title == "改了标题"
    assert project.requirement == "别出现真人 logo", "没给的那一栏不该被清掉"
    # 声明之外的键（模型多写的、前端旧版本带来的）落不进来。
    assert "not_a_param" not in project.params
    assert project.params["ratio"] == "9:16"
    assert project.params["resolution"] == "720p", "没给的档位回到默认，而不是留下脏键"

    with pytest.raises(ValueError):
        service.update_project(project.id, title="   ")


def test_script_inputs_are_exactly_the_declared_variables(db):
    """键名与流程声明一字不差：多一个键在 ``create_run`` 那里就是「未知的输入变量」。"""
    project = _project(db)
    workflow = db.scalar(select(Workflow).where(Workflow.slug == "video-script-generation"))
    declared = {item["name"] for item in workflow.input_schema}

    assert set(_script_inputs(project)) == declared


def test_script_inputs_only_attach_the_sections_that_exist(db):
    project = _project(
        db,
        duration_s=30,
        requirement="结尾要一句口号",
        style={"visual_style": "手绘", "persona": "", "palette": "橙黑"},
    )
    inputs = _script_inputs(_service(db).get_project(project.id))
    assert "快手" not in inputs["platform_note"]
    assert "抖音（竖屏" in inputs["platform_note"]
    assert "成片目标时长：30 秒" in inputs["platform_note"]
    assert "手绘" in inputs["style_note"] and "人物设定" not in inputs["style_note"]
    assert "结尾要一句口号" in inputs["requirement_note"]
    assert inputs["max_shots"] == str(settings.VIDEO_MAX_SHOTS)

    plain = _script_inputs(_project(db))
    assert plain["style_note"] == "" and plain["requirement_note"] == ""
    # 键名要跟内置流程声明一致，多一个少一个都在 create_run 那里当场报错。
    assert set(plain) == {"idea", "platform_note", "style_note", "requirement_note", "max_shots"}


# ---- 模型输出的清洗 ----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [(None, ""), (5, "5"), ("  abc  ", "abc"), ("超长文本一段", "超长文本一")],
)
def test_text_of_stringifies_and_trims(value, expected):
    assert text_of(value, 5) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (5, 5),
        (5.9, 5),
        ("5", 5),
        ("5秒", 5),
        ("约 12 秒", 12),
        ("", 5),
        (True, 5),
        (None, 5),
    ],
)
def test_duration_of_accepts_how_models_write_seconds(raw, expected):
    assert shots_from_model([{"image_prompt": "p", "duration_s": raw}])[0]["duration_s"] == expected


def test_shots_from_model_drops_unusable_rows():
    """非数组 = 一次明确的失败；没有 image_prompt 的那一镜渲染不了，也不替它编。"""
    assert shots_from_model("一、开场\n二、正文") == []
    assert shots_from_model({"shots": [{"image_prompt": "有"}]})[0]["image_prompt"] == "有"
    assert shots_from_model([{"scene": "只有画面描述"}, "不是对象", {"image_prompt": "  "}]) == []


def test_shots_from_model_caps_the_shot_count():
    rows = [{"image_prompt": f"镜头{i}"} for i in range(MAX_SHOTS_FROM_MODEL + 10)]
    assert len(shots_from_model(rows)) == MAX_SHOTS_FROM_MODEL


def test_shots_from_model_never_carries_material_ids():
    """素材引用是人在工作台挑的，不是模型编的：库按账号而非按项目归，模型没有依据。

    所以这一层直接把那个键丢掉——留着一个编出来的 id，报错要等到渲染那一刻才在
    「素材不存在」这种地方炸出来。
    """
    shot = shots_from_model([{"image_prompt": "p", "ref_asset_ids": ["编的1", "编的2"]}])[0]
    assert "ref_asset_ids" not in shot and "material_ids" not in shot


# ---- 整批保存镜头 ------------------------------------------------------------


def test_save_shots_recomputes_order_and_timeline(db):
    project = _project(db)
    service = _service(db)

    service.save_shots(
        project.id,
        [_shot({"duration_s": 5}), _shot({"duration_s": 20}), _shot({"duration_s": 4})],
    )
    db.expire_all()
    rows = service.shots_of(project.id)

    assert [row.sort_order for row in rows] == [1, 2, 3]
    # 超出 2.0 系窗口的时长夹到 15：总时长算错比少给几秒更难被发现。
    assert [row.duration_s for row in rows] == [5, 15, 4]
    assert [row.start_s for row in rows] == [0, 5, 20]


def test_save_shots_clamps_prompts_to_the_ark_limit(db):
    """写入时就截：用户在工作台看到的必须就是实际喂给模型的那段。"""
    project = _project(db)
    service = _service(db)
    service.save_shots(project.id, [_shot({"image_prompt": "字" * (settings.VIDEO_MAX_PROMPT_CHARS + 50)})])

    row = service.shots_of(project.id)[0]
    assert len(row.image_prompt) == settings.VIDEO_MAX_PROMPT_CHARS


def test_save_shots_drops_material_ids_not_in_the_library(db):
    """写入时就对一遍库：别人的 id、已删的 id、手打的 id 一律不留。

    留着它们，报错要等到渲染那一刻才在「素材路径不合法」这种地方炸出来——而那时
    作业行已经入队，用户看到的是「排队中」变成了「失败」。
    """
    project = _project(db)
    service = _service(db)
    mine = _material(db)
    theirs = _material(db, owner_id=OTHER)

    service.save_shots(
        project.id,
        [_shot({"material_ids": [mine.id, theirs.id, "不存在的那条"]})],
    )
    db.expire_all()

    assert service.shots_of(project.id)[0].material_ids == [str(mine.id)]


def test_save_shots_replaces_the_whole_batch(db):
    """数组是全集：没出现的行删掉，出现过的按 id 改——与幻灯片整包保存同一语义。"""
    project = _project(db)
    service = _service(db)
    service.save_shots(project.id, [_shot({}), _shot({})])
    first = service.shots_of(project.id)[0].id

    service.save_shots(project.id, [_shot({"id": first, "scene": "改过的场景"})])
    db.expire_all()
    rows = service.shots_of(project.id)

    assert [row.id for row in rows] == [first]
    assert rows[0].scene == "改过的场景"
    assert rows[0].sort_order == 1


def test_save_shots_rejects_foreign_shot_id_and_oversize_batch(db):
    project = _project(db)
    other = _project(db, idea="另一个项目")
    service = _service(db)
    foreign = _shots(db, other.id, 1)[0].id

    with pytest.raises(ValueError, match="不属于这个项目"):
        service.save_shots(project.id, [_shot({"id": foreign})])

    with pytest.raises(ValueError, match="最多"):
        service.save_shots(
            project.id, [_shot({}) for _ in range(settings.VIDEO_MAX_SHOTS + 1)]
        )


def test_save_shots_defaults_the_gen_mode(db):
    project = _project(db)
    service = _service(db)
    service.save_shots(
        project.id, [_shot({"gen_mode": MODE_T2V}), _shot({"gen_mode": "文生视频"})]
    )
    db.expire_all()

    modes = [row.gen_mode for row in service.shots_of(project.id)]
    assert modes == [MODE_T2V, MODE_FIRST_FRAME], "白名单外的值回落到默认档而不是进库"


# ---- 首帧入队 --------------------------------------------------------------


def test_render_frames_enqueues_one_queued_job_per_shot(db):
    project = _project(db, params={"image_size": "4K", "watermark": True})
    service = _service(db)
    _image_model(db)
    image = _material(db)
    service.save_shots(project.id, [_shot({}), _shot({"material_ids": [str(image.id)]})])
    rows = service.shots_of(project.id)

    jobs = service.render_frames(project.id, [])

    assert len(jobs) == len(rows) == 2
    assert all(job.status == JOB_QUEUED for job in jobs)
    assert [job.biz_type for job in jobs] == [BIZ_VIDEO_SHOT, BIZ_VIDEO_SHOT]
    assert [job.biz_id for job in jobs] == [str(row.id) for row in rows]
    assert [row.image_job_id for row in service.shots_of(project.id)] == [job.id for job in jobs]
    assert jobs[0].model_name == "doubao-seedream-5-0-pro-260128"

    first, second = (job.request for job in jobs)
    assert first["project_id"] == project.id
    assert first["prompt"] == rows[0].image_prompt
    assert first["size"] == "4K" and first["watermark"] is True
    # 素材进库的是 rel_path，base64 到发送那一刻才拼。
    assert first["images"] == [] and second["images"] == [image.rel_path]


def test_render_frames_sends_image_materials_and_records_video_ones(db):
    """视频素材这一期只是被记住，进不了请求体：参考视频要公网可达的地址，而渲染只到首帧。

    所以断言分两半——镜头上两条 id 都在（用户挑的没丢），作业里只有图片那条。
    """
    project = _project(db)
    service = _service(db)
    _image_model(db)
    image = _material(db, "主角形象.png", PNG)
    clip = _material(db, "主角走位.mp4", MP4)

    service.save_shots(project.id, [_shot({"material_ids": [str(image.id), str(clip.id)]})])

    request = service.render_frames(project.id, [])[0].request
    assert request["images"] == [image.rel_path]
    assert service.shots_of(project.id)[0].material_ids == [str(image.id), str(clip.id)]


def test_render_frames_can_target_selected_shots(db):
    project = _project(db)
    service = _service(db)
    _image_model(db)
    rows = _shots(db, project.id, 3)

    jobs = service.render_frames(project.id, [rows[1].id])

    assert [job.biz_id for job in jobs] == [str(rows[1].id)]
    with pytest.raises(ValueError, match="不属于该项目"):
        service.render_frames(project.id, [rows[0].id + 9999])


def test_render_frames_rejects_the_batch_when_a_prompt_is_missing(db):
    """整批拒绝而不是跳过：确认屏上写着 N 张，回来只有 N-1 张最难查。"""
    project = _project(db)
    service = _service(db)
    _image_model(db)
    service.save_shots(project.id, [_shot({}), _shot({"image_prompt": "  "})])

    with pytest.raises(ValueError, match="第 2 镜还没有首帧提示词"):
        service.render_frames(project.id, [])
    assert db.scalar(select(MediaJob.id)) is None


def test_render_frames_needs_a_tested_image_model(db):
    """门槛在取镜头之前：没通过探测就入队，只会留下一批白烧额度的排队作业。"""
    project = _project(db)
    service = _service(db)
    _shots(db, project.id, 1)

    with pytest.raises(ValueError, match="尚未配置图片生成模型"):
        service.render_frames(project.id, [])
    assert db.scalar(select(MediaJob.id)) is None

    _image_model(db, tested=False)
    with pytest.raises(ValueError, match="连通性测试"):
        service.render_frames(project.id, [])
    assert db.scalar(select(MediaJob.id)) is None


def test_render_frames_without_any_shot(db):
    project = _project(db)
    service = _service(db)
    _image_model(db)

    with pytest.raises(ValueError, match="先生成脚本"):
        service.render_frames(project.id, [])


def test_render_cover_addresses_the_project_without_a_pointer(db):
    project = _project(db)
    service = _service(db)
    _image_model(db)
    service.update_project(project.id, publish={"cover_prompt": "封面：三步清单压在画面右侧"})

    job = service.render_cover(project.id)

    assert (job.biz_type, job.biz_id) == (BIZ_VIDEO_COVER, str(project.id))
    assert job.request["prompt"] == "封面：三步清单压在画面右侧"
    assert job.request["images"] == [] and job.request["watermark"] is False


def test_render_cover_falls_back_to_the_hook(db):
    """没有封面描述时退回钩子；两者都空就拒掉，不发一条必然失败的作业。"""
    project = _project(db)
    service = _service(db)
    _image_model(db)
    service.update_project(project.id, brief={"hook": "三步，十分钟，讲完一个季度"})

    assert service.render_cover(project.id).request["prompt"] == "三步，十分钟，讲完一个季度"

    empty = _project(db, idea="另一个想法")
    with pytest.raises(ValueError, match="还没有封面描述"):
        _service(db).render_cover(empty.id)


def test_cancel_a_job_that_belongs_to_the_project(db):
    project = _project(db)
    service = _service(db)
    _image_model(db)
    _shots(db, project.id, 1)
    job = service.render_frames(project.id, [])[0]

    cancelled = asyncio.run(service.cancel_job(job.id))
    db.expire_all()

    assert cancelled.status == JOB_CANCELLED
    assert service.shots_of(project.id)[0].image_job_id == job.id, "取消不改指针"


# ---- 删除闸门 --------------------------------------------------------------


def test_delete_is_blocked_while_the_script_runs(db):
    project = _project(db)
    with pytest.raises(ValueError, match="脚本正在生成"):
        _service(db).delete_project(project.id)


def test_delete_blocks_every_job_that_is_not_terminal(db):
    """queued 也挡：那种作业一次上游都没发，删完项目再让它被循环捞起来就是白烧。"""
    project = _project(db)
    service = _service(db)
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.subject_ref == str(project.id)))
    run.status = "succeeded"
    db.commit()

    _image_model(db)
    _shots(db, project.id, 1)
    job = service.render_frames(project.id, [])[0]
    assert job.status == JOB_QUEUED
    with pytest.raises(ValueError, match="还有 1 个渲染没有结束"):
        service.delete_project(project.id)

    job.status = JOB_CANCELLED
    db.commit()
    assert service.delete_project(project.id) == project.id
    assert service.shots_of(project.id) == []
    assert db.scalar(select(VideoProject.id)) is None


def test_delete_counts_the_jobs_hanging_off_shots(db):
    """挡删除的不只是项目自己那条封面作业：镜头指针的首帧同样算「没结束」。"""
    project = _project(db)
    service = _service(db)
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.subject_ref == str(project.id)))
    run.status = "succeeded"
    db.commit()
    shot = _shots(db, project.id, 1)[0]
    db.add(
        MediaJob(
            owner_id=OWNER,
            kind=KIND_IMAGE,
            biz_type=BIZ_VIDEO_SHOT,
            biz_id=str(shot.id),
            model_name="m",
            status=JOB_RUNNING,
            request={"project_id": project.id},
        )
    )
    db.commit()

    with pytest.raises(ValueError, match="渲染没有结束"):
        service.delete_project(project.id)

    # 判死（超龄）是终态：循环不会再碰它，删除不该被一条已经死了的作业永久卡住。
    db.query(MediaJob).update({"status": JOB_EXPIRED})
    db.commit()
    assert service.delete_project(project.id) == project.id


def test_other_users_cannot_reach_a_project(db):
    project = _project(db)
    stranger = VideoService(db, OTHER)

    with pytest.raises(LookupError):
        stranger.get_project(project.id)
    with pytest.raises(LookupError):
        stranger.save_shots(project.id, [_shot({})])
    assert stranger.list_projects() == []


# ---- 并发现状与序列化 ------------------------------------------------------


def test_capacity_counts_active_jobs_per_kind(db):
    project = _project(db)
    for kind, status in (
        (KIND_IMAGE, JOB_QUEUED),
        (KIND_IMAGE, JOB_RUNNING),
        (KIND_IMAGE, JOB_SUCCEEDED),
        (KIND_VIDEO, JOB_RUNNING),
        (KIND_VIDEO, JOB_CANCELLED),
    ):
        db.add(
            MediaJob(
                owner_id=OWNER, kind=kind, biz_type=BIZ_VIDEO_SHOT, biz_id="1",
                model_name="m", status=status, request={},
            )
        )
    db.commit()

    items = {item.kind: item for item in _service(db).capacity()}

    assert [item.kind for item in _service(db).capacity()] == [KIND_IMAGE, KIND_VIDEO]
    assert (items[KIND_IMAGE].inflight, items[KIND_IMAGE].limit) == (1, settings.IMAGE_MAX_INFLIGHT)
    assert (items[KIND_VIDEO].inflight, items[KIND_VIDEO].limit) == (1, settings.VIDEO_MAX_INFLIGHT)


def test_item_status_comes_from_the_run_and_the_shots(db):
    project = _project(db)
    service = _service(db)
    assert service.to_item(project).status == SUBJECT_ANALYZING

    run = db.scalar(select(WorkflowRun).where(WorkflowRun.subject_ref == str(project.id)))
    run.status = "succeeded"
    run.error_msg = None
    db.commit()
    db.expire_all()

    empty = service.to_item(service.get_project(project.id))
    assert empty.status == SUBJECT_ERROR, "运行成功却一镜都没有，不能报「已完成」"
    assert empty.shot_count == 0 and empty.duration_s == 0

    _shots(db, project.id, 2)
    db.expire_all()
    done = service.to_item(service.get_project(project.id))
    assert (done.status, done.shot_count, done.duration_s) == (SUBJECT_READY, 2, 10)


def test_items_carry_shot_count_and_local_cover(db):
    project = _project(db)
    service = _service(db)
    _shots(db, project.id, 1)
    db.add(
        MediaJob(
            owner_id=OWNER, kind=KIND_IMAGE, biz_type=BIZ_VIDEO_COVER, biz_id=str(project.id),
            model_name="m", status=JOB_SUCCEEDED, request={},
            rel_path="videos/1/abc123def4567890.png",
        )
    )
    db.add(
        MediaJob(
            owner_id=OWNER, kind=KIND_IMAGE, biz_type=BIZ_VIDEO_COVER, biz_id=str(project.id),
            model_name="m", status=JOB_FAILED, request={}, rel_path=None,
        )
    )
    db.commit()

    items = service.to_items(service.list_projects())

    assert len(items) == 1
    assert items[0].cover_url == "/uploads/videos/1/abc123def4567890.png"


def test_detail_serializes_jobs_without_the_upstream_url(db):
    """界面上的地址只能是本站的 rel_path 拼出来的：上游那条 24 小时链接不出门。"""
    project = _project(db)
    service = _service(db)
    _image_model(db)
    _shots(db, project.id, 1)
    job = service.render_frames(project.id, [])[0]
    job.status = JOB_SUCCEEDED
    job.rel_path = f"videos/{project.id}/abc123def4567890.png"
    job.mime = "image/png"
    job.file_size = len(PNG)
    job.response = {"url": "https://ark.example.com/gone-in-24h.png"}
    db.commit()

    detail = service.to_detail(service.get_project(project.id))

    assert detail.shots[0].image_job.status == JOB_SUCCEEDED
    assert detail.shots[0].image_job.url == f"/uploads/videos/{project.id}/abc123def4567890.png"
    assert "ark.example.com" not in detail.model_dump_json()
    # 作业的原始响应与 rel_path 都不对外：界面上要的只有拼好的站内地址。
    assert not {"response", "rel_path", "request"} & set(MediaJobOut.model_fields)
    assert detail.params.generate_audio is False
    assert detail.shots[0].gen_mode == MODE_FIRST_FRAME


def test_business_rows_still_carry_no_status_column(db):
    """状态只在运行行与作业行上：这两张表一长出 status 就有了第三份事实源。"""
    for model in (VideoProject, VideoShot):
        columns = {column.name for column in model.__table__.columns}
        assert not {"status", "error_msg"} & columns, model.__tablename__
    assert not {"rel_path", "image_url"} & {column.name for column in VideoShot.__table__.columns}
