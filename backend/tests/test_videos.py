"""短视频的服务层：分镜清洗、镜头时间轴、首帧与片段入队、删除闸门与序列化。

脚本那条两步流程（模型调用 → 创作卡 + 分镜）在 ``test_workflow_flows.py`` 里测；这里
接在它后面——从「分镜已经落进 ``video_shot``」开始，那一段不碰服务商：``render_frames``
只往 ``media_job`` 写一行 ``queued``，真正的 HTTP 在 ``test_media_ark.py`` 与
``test_media_jobs.py``。产物目录一律指到 pytest 的临时目录，不碰真实部署的 uploads/。

断言的重点始终是**唯一事实源**：状态与产物路径只在作业行上，镜头行只有一个指针，
项目行连指针都不留（封面按 ``biz_type`` + ``biz_id`` 查最新那条成功的）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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
    MaterialAsset,
    MediaJob,
    ModelConfig,
    PublishJob,
    VideoProject,
    VideoShot,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
)
from app.models.schemas import CurrentUser, MediaJobOut, ModelPurpose, VideoScriptStepOut
from app.services.media import (
    BIZ_VIDEO_COVER,
    BIZ_VIDEO_SHOT,
    JOB_CANCELLED,
    JOB_DOWNLOADING,
    JOB_EXPIRED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    KIND_IMAGE,
    KIND_VIDEO,
    MODE_FIRST_FRAME,
    MODE_FIRST_LAST_FRAME,
    MODE_REFERENCE,
    MODE_T2V,
    ROLE_FIRST_FRAME,
    ROLE_REFERENCE_IMAGE,
    ROLE_REFERENCE_VIDEO,
)
from app.services.characters import service as character_service
from app.services.characters import store as character_store
from app.services.characters.service import CharacterService
from app.services.characters.store import store_character_image
from app.services.media import store as media_store
from app.services.materials import KIND_IMAGE as MATERIAL_IMAGE
from app.services.materials import service as material_service
from app.services.materials import store as material_store
from app.services.materials.store import store_material
from app.services.materials.service import MaterialService
from app.services.videos.normalize import MAX_SHOTS_FROM_MODEL, shots_from_model, text_of
from app.services.videos import service as video_service
from app.services.videos.service import VideoService, _script_inputs
from app.services.workflows import (
    RUN_PENDING,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_RUNNING,
    STEP_SUCCEEDED,
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
        CHARACTER_MAX_PER_OWNER=settings.CHARACTER_MAX_PER_OWNER,
    )
    monkeypatch.setattr(media_store, "settings", fake)
    monkeypatch.setattr(material_store, "settings", fake)
    # 人物库的形象图与配额闸门各读自己模块的 settings，要指到同一份临时盘配置。
    monkeypatch.setattr(character_store, "settings", fake)
    monkeypatch.setattr(character_service, "settings", fake)
    # 视频服务模块直接读 settings.upload_media_path 做产物存在性检查（链式衔接、
    # 成片合成）。它是 Settings 上的 property、实例没有 setter，所以在类上换一个
    # 临时 property：只重定向落盘目录，其余配置仍走真实 settings，monkeypatch 自动还原。
    monkeypatch.setattr(
        type(settings), "upload_media_path", property(lambda self: tmp_path)
    )
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


@pytest.fixture()
def client(db):
    """按 OWNER 身份发请求的 TestClient。

    不进上下文管理器，所以 lifespan 不跑：启动自检那几步要连真库，这里的表已经由
    ``Base.metadata.create_all`` 建好了（与 ``test_materials.py`` 同一姿势）。
    """
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: CurrentUser(user_id=OWNER, username="u1")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


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


def _video_model(
    db,
    owner_id: int = OWNER,
    *,
    tested: bool = True,
    model_name: str = "doubao-seedance-2-0-260128",
) -> ModelConfig:
    config = ModelConfig(
        owner_id=owner_id,
        purpose=ModelPurpose.VIDEO.value,
        title="测试视频模型",
        model_name=model_name,
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_enc=encrypt("sk-unit-test"),
        is_default=True,
        last_test_ok=tested,
    )
    db.add(config)
    db.commit()
    return config


# Seedance 2.5 接入点 ID 的典型形状（型号段的点在 ID 里是连字符）。
SEEDANCE_25 = "doubao-seedance-2-5-251015"


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


def _character(db, name: str = "林小夏", owner_id: int = OWNER):
    """人物库里的一个真实角色：走形象图落盘与建档，而不是手搓数据库行。"""
    stored = store_character_image(owner_id, PNG)
    return CharacterService(db, owner_id).create(
        name=name,
        persona="28 岁互联网运营，语速快",
        outfit="米白色西装外套、牛仔裤",
        features="齐肩短发、圆框眼镜",
        stored=stored,
    )


def _shots(db, project_id: int, count: int = 2) -> list[VideoShot]:
    service = _service(db)
    service.save_shots(project_id, [_shot({}) for _ in range(count)])
    return service.shots_of(project_id)


def _rendered(db, project_id: int, count: int = 2) -> tuple[list[VideoShot], list[MediaJob]]:
    """渲好的镜头：作业行标成功、产物真的写进临时盘，``rel_path`` 与那个文件一致。

    ``render_frames`` 只管入队，落盘是常驻循环（作业执行器）干的事，所以「这一镜出片了」
    在测试里要自己把两半都造出来。
    """
    service = _service(db)
    _image_model(db)
    rows = _shots(db, project_id, count)
    jobs = service.render_frames(project_id, [])
    for job in jobs:
        stored = media_store.store_blob(project_id, PNG)
        job.status = JOB_SUCCEEDED
        job.rel_path = stored.rel_path
        job.mime = stored.mime
        job.file_size = stored.size
    db.commit()
    return rows, jobs


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
    assert set(plain) == {
        "idea",
        "platform_note",
        "style_note",
        "characters_note",
        "requirement_note",
        "brief_note",
        "max_shots",
    }
    assert plain["brief_note"] == "", "没改过的创作卡不该被当成用户前提带进去"
    assert plain["characters_note"] == "", "没选角色的项目不该带出角色段落"


def test_confirmed_brief_note_carries_the_card_the_user_edited(db):
    """创作卡被人改过时，重新生成要把这张卡带进策划那一步。"""
    project = _project(db)
    service = _service(db)
    # 直接改项目上的卡，等价于工作台里编辑并保存。
    service.update_project(
        project.id,
        brief={
            "hook_type": "反差",
            "hook": "月薪 3000 和 30000 的区别",
            "structure": ["开场", "对比", "收束"],
        },
    )

    note = service._confirmed_brief_note(service.get_project(project.id))

    assert "钩子类型：反差" in note
    assert "开头钩子：月薪 3000 和 30000 的区别" in note
    assert "段落顺序：开场 → 对比 → 收束" in note
    assert _script_inputs(service.get_project(project.id), note)["brief_note"] == note


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


# ---- 片段入队 --------------------------------------------------------------


def test_render_clips_feeds_the_ready_first_frame(db):
    """首帧档吃的是已经落盘的那张图；参数按**本镜**时长发，一镜一条任务。"""
    project = _project(db, params={"resolution": "1080p", "watermark": True})
    service = _service(db)
    rows, frames = _rendered(db, project.id, 2)
    # 两镜不同时长：片段时长取的是镜头行，不是项目级的成片时长。
    service.save_shots(
        project.id,
        [_shot({"id": rows[0].id, "duration_s": 5}), _shot({"id": rows[1].id, "duration_s": 9})],
    )
    _video_model(db)

    jobs = service.render_clips(project.id, [])

    assert len(jobs) == 2
    assert all(job.kind == KIND_VIDEO and job.status == JOB_QUEUED for job in jobs)
    assert [job.biz_type for job in jobs] == [BIZ_VIDEO_SHOT, BIZ_VIDEO_SHOT]
    assert [job.biz_id for job in jobs] == [str(row.id) for row in rows]
    assert jobs[0].model_name == "doubao-seedance-2-0-260128"
    fresh = service.shots_of(project.id)
    assert [row.video_job_id for row in fresh] == [job.id for job in jobs]
    assert [row.image_job_id for row in fresh] == [job.id for job in frames], "首帧指针不该被动"

    first, second = (job.request for job in jobs)
    assert first["project_id"] == project.id
    assert first["prompt"] == rows[0].video_prompt
    assert first["images"] == [{"role": ROLE_FIRST_FRAME, "rel_path": frames[0].rel_path}]
    assert first["params"] == {
        "ratio": "9:16",
        "resolution": "1080p",
        "duration": 5,
        "watermark": True,
        "generate_audio": False,
    }
    assert second["params"]["duration"] == 9
    assert "fps" not in first["params"], "方舟没有 fps 这个键，发过去就是白烧一次 400"


def test_render_clips_t2v_sends_no_image(db):
    """纯文生一张图都不给，也不要求先渲首帧——给了图它就变成图生视频了。"""
    project = _project(db)
    service = _service(db)
    _video_model(db)
    service.save_shots(project.id, [_shot({"gen_mode": MODE_T2V})])

    request = service.render_clips(project.id, [])[0].request

    assert request["images"] == []
    assert request["prompt"] == "镜头缓慢推近，人物眨眼"


def test_seedance_25_detection_covers_id_and_series_spellings():
    """接入点 ID 用连字符、系列名用点，两种写法都要认得；2.0 与其他型号不能误判。"""
    assert video_service._is_seedance_25("doubao-seedance-2-5-251015")
    assert video_service._is_seedance_25("doubao-seedance-2.5")
    assert video_service._is_seedance_25("SEEDANCE-2-5-PRO")
    assert not video_service._is_seedance_25("doubao-seedance-2-0-fast-250115")
    assert not video_service._is_seedance_25("doubao-seedance-1-0-pro-250528")
    assert not video_service._is_seedance_25("")


def test_render_clips_seedance_25_first_frame_forces_adaptive_ratio(db):
    """2.5 首帧档显式 ratio 会被上游 400（TaskTypeConstraint）：画幅跟首帧，只能发 adaptive。"""
    project = _project(db, params={"ratio": "9:16", "resolution": "1080p"})
    service = _service(db)
    rows, frames = _rendered(db, project.id, 1)
    _video_model(db, model_name=SEEDANCE_25)

    request = service.render_clips(project.id, [])[0].request

    assert request["images"] == [{"role": ROLE_FIRST_FRAME, "rel_path": frames[0].rel_path}]
    assert request["params"]["ratio"] == "adaptive"
    # 只改画幅口径，其余参数原样：分辨率、本镜时长、音频开关都不受影响。
    assert request["params"]["resolution"] == "1080p"
    assert request["params"]["duration"] == rows[0].duration_s
    assert request["params"]["generate_audio"] is False


def test_render_clips_seedance_25_t2v_keeps_the_explicit_ratio(db):
    """纯文生没有首帧可跟，2.5 仍按项目的显式画幅发，不能一刀切 adaptive。"""
    project = _project(db, params={"ratio": "9:16"})
    service = _service(db)
    _video_model(db, model_name=SEEDANCE_25)
    service.save_shots(project.id, [_shot({"gen_mode": MODE_T2V})])

    request = service.render_clips(project.id, [])[0].request

    assert request["params"]["ratio"] == "9:16"
    assert request["images"] == []


def test_render_clips_seedance_20_first_frame_keeps_the_explicit_ratio(db):
    """老型号的既有行为不能被 2.5 的适配带偏：2.0 首帧档仍吃显式 9:16。"""
    project = _project(db)
    service = _service(db)
    _rendered(db, project.id, 1)
    _video_model(db)  # 默认就是 doubao-seedance-2-0

    request = service.render_clips(project.id, [])[0].request

    assert request["params"]["ratio"] == "9:16"


def test_render_clips_seedance_25_reference_previous_uses_adaptive(db, monkeypatch):
    """2.5 上「参考上一版」可能被判成编辑/延长任务，那两类同样只收 adaptive。"""
    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    project = _project(db)
    service = _service(db)
    _rendered(db, project.id, 1)
    _video_model(db, model_name=SEEDANCE_25)
    previous = _stored_clip(db, service, project.id)

    request = service.render_clips(project.id, [], reference_previous=True)[0].request

    assert request["images"] == [
        {"role": ROLE_REFERENCE_VIDEO, "rel_path": previous.rel_path},
    ]
    assert request["params"]["ratio"] == "adaptive"


# ---- 尾帧链式渲染 ------------------------------------------------------------


def _clip_with_last_frame(db, project_id: int, shot: VideoShot) -> MediaJob:
    """给镜头挂一条带尾帧产物的成功视频作业（两个文件都真的落在临时盘上）。"""
    video_stored = media_store.store_blob(project_id, MP4)
    frame_stored = media_store.store_blob(project_id, PNG)
    job = MediaJob(
        owner_id=OWNER,
        provider="ark",
        kind=KIND_VIDEO,
        biz_type=BIZ_VIDEO_SHOT,
        biz_id=str(shot.id),
        model_name="doubao-seedance-2-0-260128",
        status=JOB_SUCCEEDED,
        request={},
        rel_path=video_stored.rel_path,
        mime=video_stored.mime,
        file_size=video_stored.size,
        last_frame_rel_path=frame_stored.rel_path,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    shot.video_job_id = job.id
    db.commit()
    return job


def _chain_project(db, **project_overrides):
    params = {"chain_clips": True}
    params.update(project_overrides.pop("params", {}))
    return _project(db, params=params, **project_overrides)


def test_chain_enqueue_snapshots_prev_job_and_keeps_head_unchained(db):
    project = _chain_project(db)
    service = _service(db)
    rows, _ = _rendered(db, project.id, 3)  # 首帧全部就绪，片段作业另行挂
    prev_first = _clip_with_last_frame(db, project.id, rows[0])
    prev_second = _clip_with_last_frame(db, project.id, rows[1])
    _video_model(db)

    jobs = service.render_clips(project.id, [])

    assert "chain" not in jobs[0].request, "项目第一镜是链头，用自己的首帧定调"
    chain_2 = jobs[1].request["chain"]
    chain_3 = jobs[2].request["chain"]
    assert chain_2["enabled"] and chain_2["injected"] is False
    assert chain_2["prev_job_id"] == prev_first.id and chain_2["prev_shot_order"] == 1
    # 快照的是上镜**当时那条作业**：叙事顺序上的紧邻前镜，不是选中集合里的前一个。
    assert chain_3["prev_job_id"] == prev_second.id and chain_3["prev_shot_order"] == 2
    # 入队阶段不替换 images：尾帧注入发生在作业执行的提交前。
    assert jobs[1].request["images"][0]["role"] == ROLE_FIRST_FRAME


def test_chain_rejects_when_the_previous_shot_has_no_last_frame(db):
    project = _chain_project(db)
    service = _service(db)
    rows = _shots(db, project.id, 2)
    # 只有普通成功片段、没有尾帧：链式衔接缺素材，入队就要拒绝，不能等到执行期。
    stored = media_store.store_blob(project.id, MP4)
    job = MediaJob(
        owner_id=OWNER, provider="ark", kind=KIND_VIDEO, biz_type=BIZ_VIDEO_SHOT,
        biz_id=str(rows[0].id), model_name="m", status=JOB_SUCCEEDED, request={},
        rel_path=stored.rel_path, mime=stored.mime, file_size=stored.size,
    )
    db.add(job)
    db.commit()
    rows[0].video_job_id = job.id
    db.commit()
    _video_model(db)

    with pytest.raises(ValueError, match="第 2 镜"):
        service.render_clips(project.id, [])


def test_chain_strict_mode_rejects_t2v_and_reference_shots(db):
    project = _chain_project(db)
    service = _service(db)
    rows = _shots(db, project.id, 2)
    _clip_with_last_frame(db, project.id, rows[0])
    service.save_shots(
        project.id,
        [_shot({"id": rows[0].id}), _shot({"id": rows[1].id, "gen_mode": MODE_T2V})],
    )
    _video_model(db)

    with pytest.raises(ValueError, match="首帧模式"):
        service.render_clips(project.id, [])


def test_chain_off_writes_no_chain_block(db):
    """开关默认关：入队形状与链式上线前一字不差。"""
    project = _project(db)
    service = _service(db)
    rows, _ = _rendered(db, project.id, 2)
    _video_model(db)

    jobs = service.render_clips(project.id, [])

    assert all("chain" not in job.request for job in jobs)


def test_chain_yields_to_reference_previous(db, monkeypatch):
    """两个开关同时存在时，显式重做动作优先：走参考视频，不写 chain 块。"""
    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    project = _chain_project(db)
    service = _service(db)
    _rendered(db, project.id, 1)
    _video_model(db)
    previous = _stored_clip(db, service, project.id)

    request = service.render_clips(project.id, [], reference_previous=True)[0].request

    assert "chain" not in request
    assert request["images"] == [
        {"role": ROLE_REFERENCE_VIDEO, "rel_path": previous.rel_path},
    ]


def test_chain_partial_render_follows_the_project_order(db):
    """只渲镜 3：它接的是项目里的镜 2，而不是选中集合里的链头镜 3 自己。"""
    project = _chain_project(db)
    service = _service(db)
    rows, _ = _rendered(db, project.id, 3)
    _clip_with_last_frame(db, project.id, rows[1])  # 镜 2 就绪
    _video_model(db)

    jobs = service.render_clips(project.id, [rows[2].id])

    assert len(jobs) == 1
    chain = jobs[0].request["chain"]
    assert chain["prev_shot_order"] == 2


def test_render_clips_reference_sends_image_materials_only(db):
    """参考图档与首帧同一取舍：视频素材要公网地址，进不了 base64 那一档。"""
    project = _project(db)
    service = _service(db)
    _video_model(db)
    image = _material(db, "主角形象.png", PNG)
    clip = _material(db, "主角走位.mp4", MP4)
    service.save_shots(
        project.id,
        [_shot({"gen_mode": MODE_REFERENCE, "material_ids": [str(image.id), str(clip.id)]})],
    )

    request = service.render_clips(project.id, [])[0].request

    assert request["images"] == [{"role": ROLE_REFERENCE_IMAGE, "rel_path": image.rel_path}]
    assert service.shots_of(project.id)[0].material_ids == [str(image.id), str(clip.id)]


def _stored_clip(db, service, project_id: int) -> MediaJob:
    """先渲一版并让它真的落盘：「参考上一版」要的就是这份产物（与 ``_rendered`` 同一姿势）。"""
    job = service.render_clips(project_id, [])[0]
    stored = media_store.store_blob(project_id, MP4)
    job.status = JOB_SUCCEEDED
    job.rel_path = stored.rel_path
    job.mime = stored.mime
    job.file_size = stored.size
    db.commit()
    return job


def test_render_clips_can_reference_the_previous_clip(db, monkeypatch):
    """重做时把上一版成片挂成参考视频：沿用形象与运镜，而不是重掷一次骰子。"""
    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    project = _project(db)
    service = _service(db)
    rows, _ = _rendered(db, project.id, 1)
    _video_model(db)
    previous = _stored_clip(db, service, project.id)

    job = service.render_clips(project.id, [], reference_previous=True)[0]

    # 首帧**不能**跟着一起发：方舟对首帧/尾帧与参考素材混用直接回 400
    # （InvalidParameter: first/last frame content cannot be mixed with reference
    # media content）。上一版成片已经承载了画面，这一档只发它。
    assert job.request["images"] == [
        {"role": ROLE_REFERENCE_VIDEO, "rel_path": previous.rel_path},
    ]
    assert "@视频1" in job.request["prompt"], "只挂素材不引用，模型可能直接忽略它"
    assert job.request["prompt"].endswith(rows[0].video_prompt)
    # 纯「参考生视频」任务对 ratio/duration 无特殊限制，仍按本镜发；能显式锁定子任务
    # 的 omni_reference_task_type 只有 2.5 支持，2.0 系传不得。
    assert job.request["params"]["ratio"] == "9:16"
    assert "omni_reference_task_type" not in job.request["params"]
    assert service.shots_of(project.id)[0].video_job_id == job.id


def test_render_clips_reference_previous_keeps_reference_images(db, monkeypatch):
    """参考图档的图片素材留着：图片 + 视频的组合参考是允许的，只有首帧/尾帧不行。"""
    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    project = _project(db)
    service = _service(db)
    _video_model(db)
    image = _material(db, "主角形象.png", PNG)
    service.save_shots(
        project.id,
        [_shot({"gen_mode": MODE_REFERENCE, "material_ids": [str(image.id)]})],
    )
    previous = _stored_clip(db, service, project.id)

    request = service.render_clips(project.id, [], reference_previous=True)[0].request

    assert request["images"] == [
        {"role": ROLE_REFERENCE_IMAGE, "rel_path": image.rel_path},
        {"role": ROLE_REFERENCE_VIDEO, "rel_path": previous.rel_path},
    ]


def test_render_clips_reference_previous_does_not_need_a_frame(db, monkeypatch):
    """首帧既然不发，「首帧还没出图」那一档就不该再拦着重做。"""
    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    project = _project(db)
    service = _service(db)
    _video_model(db)
    rows = _shots(db, project.id, 1)
    assert rows[0].gen_mode == MODE_FIRST_FRAME and rows[0].image_job_id is None
    stored = media_store.store_blob(project.id, MP4)
    previous = MediaJob(
        owner_id=OWNER,
        kind=KIND_VIDEO,
        biz_type=BIZ_VIDEO_SHOT,
        biz_id=str(rows[0].id),
        model_name="doubao-seedance-2-0-260128",
        status=JOB_SUCCEEDED,
        rel_path=stored.rel_path,
        request={"project_id": project.id},
    )
    db.add(previous)
    db.flush()
    rows[0].video_job_id = previous.id
    db.commit()

    job = service.render_clips(project.id, [], reference_previous=True)[0]

    assert job.request["images"] == [
        {"role": ROLE_REFERENCE_VIDEO, "rel_path": stored.rel_path},
    ]
    # 同一个镜头关掉开关就仍然被首帧那一档拦住：开关只改这一档，不是把门槛拆了。
    with pytest.raises(ValueError, match="首帧还没渲出来"):
        service.render_clips(project.id, [])


def test_render_clips_reference_previous_needs_a_stored_clip(db, monkeypatch):
    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    project = _project(db)
    service = _service(db)
    _rendered(db, project.id, 1)
    _video_model(db)

    with pytest.raises(ValueError, match="还没有可参考的上一版片段"):
        service.render_clips(project.id, [], reference_previous=True)


def test_render_clips_reference_previous_needs_a_public_base(db, monkeypatch):
    """方舟的视频参考只收公网 URL：没配基址就入队前拒掉，别等异步报错才发现。"""
    monkeypatch.setattr(video_service.settings, "PUBLIC_MEDIA_BASE_URL", "")
    project = _project(db)
    service = _service(db)
    _rendered(db, project.id, 1)
    _video_model(db)

    with pytest.raises(ValueError, match="PUBLIC_MEDIA_BASE_URL"):
        service.render_clips(project.id, [], reference_previous=True)


def test_reference_previous_hint_survives_the_prompt_clamp(db, monkeypatch):
    """引用句拼在最前面：480 字上限截掉的是运动提示词的尾巴，不是那句引用。"""
    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    project = _project(db)
    service = _service(db)
    rows, _ = _rendered(db, project.id, 1)
    _video_model(db)
    service.save_shots(project.id, [_shot({"id": rows[0].id, "video_prompt": "推" * 600})])
    _stored_clip(db, service, project.id)

    prompt = service.render_clips(project.id, [], reference_previous=True)[0].request["prompt"]

    assert len(prompt) == settings.VIDEO_MAX_PROMPT_CHARS
    assert "@视频1" in prompt


def test_render_clips_reports_every_problem_in_one_rejection(db):
    """四档不合格一次报全：补一条、提交、再撞下一条是最难用的拒绝方式。"""
    project = _project(db)
    service = _service(db)
    _image_model(db)
    _video_model(db)
    service.save_shots(
        project.id,
        [
            _shot({"video_prompt": "  "}),
            _shot({}),
            _shot({"gen_mode": MODE_FIRST_LAST_FRAME}),
            _shot({"gen_mode": MODE_REFERENCE}),
        ],
    )
    rows = service.shots_of(project.id)
    # 第 2 镜故意不渲首帧，其余三镜的首帧都落盘，好让四档问题各归各的镜号。
    frames = service.render_frames(project.id, [rows[0].id, rows[2].id, rows[3].id])
    for job in frames:
        job.status = JOB_SUCCEEDED
        job.rel_path = media_store.store_blob(project.id, PNG).rel_path
    db.commit()

    with pytest.raises(ValueError) as exc:
        service.render_clips(project.id, [])

    message = str(exc.value)
    assert "第 1 镜还没有运动提示词" in message
    assert "第 2 镜的首帧还没渲出来" in message
    assert "第 3 镜选了首尾帧生视频" in message
    assert "第 4 镜选了参考图生视频" in message
    assert db.scalar(select(MediaJob.id).where(MediaJob.kind == KIND_VIDEO)) is None


def test_render_clips_needs_a_stored_frame_not_just_a_succeeded_row(db):
    """报了成功却没落盘的那种行（转存死在中途）喂不上去，入队前就要拦住。"""
    project = _project(db)
    service = _service(db)
    _image_model(db)
    _video_model(db)
    _shots(db, project.id, 1)
    frame = service.render_frames(project.id, [])[0]
    frame.status = JOB_SUCCEEDED
    db.commit()

    with pytest.raises(ValueError, match="首帧还没渲出来"):
        service.render_clips(project.id, [])


def test_render_clips_needs_a_tested_video_model(db):
    """图片模型通过探测顶不了视频这一档：purpose 是两条配置。"""
    project = _project(db)
    service = _service(db)
    _rendered(db, project.id, 1)

    with pytest.raises(ValueError, match="尚未配置视频生成模型"):
        service.render_clips(project.id, [])
    assert db.scalar(select(MediaJob.id).where(MediaJob.kind == KIND_VIDEO)) is None

    _video_model(db, tested=False)
    with pytest.raises(ValueError, match="连通性测试"):
        service.render_clips(project.id, [])
    assert db.scalar(select(MediaJob.id).where(MediaJob.kind == KIND_VIDEO)) is None


def test_render_clips_can_target_selected_shots(db):
    project = _project(db)
    service = _service(db)
    rows, _frames = _rendered(db, project.id, 3)
    _video_model(db)

    jobs = service.render_clips(project.id, [rows[1].id])

    assert [job.biz_id for job in jobs] == [str(rows[1].id)]
    assert [row.video_job_id for row in service.shots_of(project.id)] == [None, jobs[0].id, None]
    with pytest.raises(ValueError, match="不属于该项目"):
        service.render_clips(project.id, [rows[0].id + 9999])

    empty = _project(db, idea="另一个想法")
    with pytest.raises(ValueError, match="先生成脚本"):
        service.render_clips(empty.id, [])


def test_clips_http_enqueues_and_returns_both_jobs(client, db):
    project = _project(db)
    _rendered(db, project.id, 1)
    _video_model(db)

    body = client.post(f"/api/office/videos/{project.id}/clips", json={"shot_ids": []}).json()

    assert body["code"] == 0 and "已开始渲染片段" in body["message"]
    shot = body["data"]["shots"][0]
    assert shot["image_job"]["status"] == JOB_SUCCEEDED
    assert shot["video_job"]["status"] == JOB_QUEUED
    assert shot["video_job"]["kind"] == KIND_VIDEO
    assert shot["video_job"]["url"] == "", "还没出片就没有地址"
    assert client.post("/api/office/videos/999999/clips", json={"shot_ids": []}).status_code == 404


def test_clips_http_rejects_a_shot_without_a_frame(client, db):
    project = _project(db)
    _image_model(db)
    _video_model(db)
    _shots(db, project.id, 1)

    body = client.post(f"/api/office/videos/{project.id}/clips", json={"shot_ids": []}).json()

    assert body["code"] == -1 and "首帧还没渲出来" in body["message"]


def test_clips_http_passes_reference_previous(client, db, monkeypatch):
    """开关要真的从正文走到服务层：路由漏传一个参数，服务层的用例是看不出来的。"""
    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    project = _project(db)
    service = _service(db)
    _rendered(db, project.id, 1)
    _video_model(db)
    _stored_clip(db, service, project.id)

    body = client.post(
        f"/api/office/videos/{project.id}/clips",
        json={"shot_ids": [], "reference_previous": True},
    ).json()

    assert body["code"] == 0
    latest = db.scalar(
        select(MediaJob).where(MediaJob.kind == KIND_VIDEO).order_by(MediaJob.id.desc())
    )
    assert [item["role"] for item in latest.request["images"]] == [ROLE_REFERENCE_VIDEO]


def test_capacity_reports_whether_reference_video_is_usable(client, db, monkeypatch):
    """界面上那个勾选框据此禁用：没配公网基址时勾了也发不出去。"""
    url = "/api/office/videos/capacity"
    monkeypatch.setattr(video_service.settings, "PUBLIC_MEDIA_BASE_URL", "")
    assert client.get(url).json()["data"]["video_reference_ready"] is False

    monkeypatch.setattr(
        video_service.settings, "PUBLIC_MEDIA_BASE_URL", "https://yangvis.example.com"
    )
    assert client.get(url).json()["data"]["video_reference_ready"] is True


# ---- 人物库：角色组、档案段落与形象图注入 ------------------------------------


def test_project_character_ids_keep_only_known_owned_ids(db):
    """写入侧归一：别人的、已删除的、编出来的 id 一律丢，保序并截到项目上限。"""
    first = _character(db, "林小夏")
    second = _character(db, "老周")
    foreign = _character(db, "别人的角色", owner_id=OTHER)

    project = _project(
        db,
        character_ids=[str(first.id), "999999", str(foreign.id), str(second.id)],
    )
    assert project.character_ids == [str(first.id), str(second.id)]

    # 角色被硬删除后，下一次保存把残留 id 自然清掉，读取侧不报错。
    CharacterService(db, OWNER).remove(second.id)
    service = _service(db)
    saved = service.update_project(project.id, character_ids=project.character_ids)
    assert saved.character_ids == [str(first.id)]

    # 空列表是显式清空；None（没带字段）保持原样。
    assert service.update_project(project.id, character_ids=[]).character_ids == []
    assert service.update_project(project.id, title="换个标题").character_ids == []


def test_project_character_ids_are_capped_per_project(db):
    characters = [_character(db, f"角色 {index}") for index in range(4)]
    project = _project(db, character_ids=[str(row.id) for row in characters])
    # 项目角色组上限在 config（默认 3）：入参可以更宽，落库只留前三个。
    assert project.character_ids == [str(row.id) for row in characters[:3]]


def test_script_inputs_carry_character_profiles(db):
    character = _character(db, "林小夏")
    project = _project(db, character_ids=[str(character.id)])
    service = _service(db)

    note = service._characters_note(service.get_project(project.id))
    assert "林小夏" in note
    assert "人设：28 岁互联网运营，语速快" in note
    assert "衣着：米白色西装外套、牛仔裤" in note
    assert "外形特色：齐肩短发、圆框眼镜" in note

    inputs = _script_inputs(service.get_project(project.id), "", note)
    assert inputs["characters_note"] == note
    # 没选角色的项目：键还在（流程声明要求闭合），值是空串，模板整段不渲染。
    plain = _project(db)
    assert _service(db)._characters_note(plain) == ""


def test_render_frames_injects_character_portraits_before_materials(db):
    """每镜的参考图 = 项目角色组形象图（在前）+ 本镜图片素材，角色缺失时自然跳过。"""
    character = _character(db, "林小夏")
    project = _project(db, character_ids=[str(character.id)])
    service = _service(db)
    _image_model(db)
    image = _material(db)
    service.save_shots(project.id, [_shot({"material_ids": [str(image.id)]})])

    request = service.render_frames(project.id, [])[0].request
    assert request["images"] == [character.rel_path, image.rel_path]

    # 删掉角色后同一项目再渲：残留 id 被跳过，只剩素材，渲染不炸。
    CharacterService(db, OWNER).remove(character.id)
    request = service.render_frames(project.id, [])[0].request
    assert request["images"] == [image.rel_path]


def test_render_frames_caps_merged_references_at_the_budget(db):
    """角色图 + 素材合计超预算时保角色：截断到 MAX_REFERENCE_IMAGES，不去碰用户的素材行。"""
    cast = [_character(db, f"角色 {index}") for index in range(2)]
    materials = [_material(db, f"素材 {index}.png") for index in range(3)]
    project = _project(db, character_ids=[str(row.id) for row in cast])
    service = _service(db)
    _image_model(db)
    service.save_shots(
        project.id,
        [_shot({"material_ids": [str(row.id) for row in materials]})],
    )

    request = service.render_frames(project.id, [])[0].request
    assert len(request["images"]) == video_service.MAX_REFERENCE_IMAGES == 4
    # 角色图两张排在最前，素材按顺序补到预算用完。
    assert request["images"][:2] == [row.rel_path for row in cast]
    assert request["images"][2:] == [row.rel_path for row in materials[:2]]
    # 镜头上的引用原样保留：截断只发生在入队快照里。
    assert service.shots_of(project.id)[0].material_ids == [str(row.id) for row in materials]


def test_render_clips_reference_mode_uses_character_portraits(db):
    """参考图生视频：角色形象图与图片素材一起挂 reference_image，角色在前。"""
    character = _character(db, "林小夏")
    project = _project(db, character_ids=[str(character.id)])
    service = _service(db)
    _video_model(db)
    image = _material(db)
    service.save_shots(
        project.id,
        [_shot({"gen_mode": MODE_REFERENCE, "material_ids": [str(image.id)]})],
    )

    request = service.render_clips(project.id, [])[0].request
    assert request["images"] == [
        {"role": ROLE_REFERENCE_IMAGE, "rel_path": character.rel_path},
        {"role": ROLE_REFERENCE_IMAGE, "rel_path": image.rel_path},
    ]


def test_render_clips_first_frame_mode_never_mixes_portraits(db):
    """首帧档只喂本镜首帧产物：角色形象已烤在首帧里，混挂参考图会撞方舟 400。"""
    character = _character(db, "林小夏")
    project = _project(db, character_ids=[str(character.id)])
    service = _service(db)
    _image_model(db)
    service.save_shots(project.id, [_shot({})])
    frames = service.render_frames(project.id, [])
    for job in frames:
        job.status = JOB_SUCCEEDED
        job.rel_path = media_store.store_blob(project.id, PNG).rel_path
    db.commit()
    _video_model(db)

    request = service.render_clips(project.id, [])[0].request
    assert [item["role"] for item in request["images"]] == [ROLE_FIRST_FRAME]
    assert character.rel_path not in [item["rel_path"] for item in request["images"]]


# ---- 产物存为素材 ------------------------------------------------------------


def test_save_frame_copies_the_artifact_into_the_library(db, tmp_path):
    """复制，不是移动，也不是引用：产物与素材是两份独立文件。"""
    project = _project(db)
    service = _service(db)
    rows, jobs = _rendered(db, project.id, 2)
    artifact = tmp_path / jobs[0].rel_path

    asset = service.save_frame_to_material(project.id, rows[0].id)

    assert (asset.owner_id, asset.kind, asset.size) == (OWNER, MATERIAL_IMAGE, len(PNG))
    # 名字由服务端按「项目 + 镜头号」给：界面上没有一个填名框，库里也就说得出这是哪一张。
    assert asset.name == "季度复盘三步｜第 1 镜首帧"
    # 打标分组就是项目名：同一批存进来的首帧在素材库里按它聚成一组，不用用户再认。
    assert asset.tag == "季度复盘三步"
    assert asset.rel_path.startswith(f"materials/{OWNER}/")
    assert (tmp_path / asset.rel_path).read_bytes() == PNG
    # 作业行一个字段都没改：这一镜的预览照旧指向产物，重做也不受影响。
    assert artifact.exists() and artifact.read_bytes() == PNG
    db.expire_all()
    assert db.get(MediaJob, jobs[0].id).rel_path == jobs[0].rel_path
    assert rows[0].material_ids in ([], None), "存进库不等于这一镜开始引用它"


def test_the_copy_outlives_the_project(db, tmp_path):
    """素材按账号分目录正是为了这一条：选题删了， reusable 的那张图还在。"""
    project = _project(db)
    service = _service(db)
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.subject_ref == str(project.id)))
    run.status = "succeeded"
    db.commit()
    rows, jobs = _rendered(db, project.id)
    asset = service.save_frame_to_material(project.id, rows[0].id)
    artifact, material_file = tmp_path / jobs[0].rel_path, tmp_path / asset.rel_path

    service.delete_project(project.id)
    media_store.delete_project_dir(project.id)  # 删整棵产物目录是路由那一步

    assert not artifact.exists()
    assert material_file.exists()
    assert MaterialService(db, OWNER).get_asset(asset.id).rel_path == asset.rel_path


def test_save_frame_refuses_a_shot_that_has_no_artifact(db, tmp_path):
    project = _project(db)
    service = _service(db)
    _image_model(db)
    row = _shots(db, project.id, 1)[0]
    job = service.render_frames(project.id, [])[0]

    with pytest.raises(ValueError, match="还没有产物"):
        service.save_frame_to_material(project.id, row.id)

    # 成功却读不出文件：转存那一步挂掉时状态先落定、文件没落，界面只能给一条明确失败。
    job.status = JOB_SUCCEEDED
    job.rel_path = f"videos/{project.id}/0000000000000000.png"
    db.commit()
    with pytest.raises(ValueError, match="读不出来"):
        service.save_frame_to_material(project.id, row.id)
    assert db.scalar(select(func.count(MaterialAsset.id))) == 0
    assert not (tmp_path / "materials").exists(), "拒绝要发生在建目录之前"


def test_save_frame_only_accepts_a_shot_of_that_project(db):
    first = _project(db)
    second = _project(db, idea="另一个想法")
    rows, _ = _rendered(db, first.id)

    with pytest.raises(ValueError, match="不属于该项目"):
        _service(db).save_frame_to_material(second.id, rows[0].id)
    # 越界的项目读作不存在，与打错 id 同一种答复。
    with pytest.raises(LookupError, match="短视频项目不存在"):
        _service(db, OTHER).save_frame_to_material(first.id, rows[0].id)


def test_save_frame_leaves_no_orphan_when_the_library_is_full(db, tmp_path, monkeypatch):
    """库满这条拒绝来自 ``add``，而文件是这里刚写下去的，所以得由这里清掉。"""
    monkeypatch.setattr(
        material_service,
        "settings",
        SimpleNamespace(
            upload_media_path=tmp_path,
            UPLOAD_MEDIA_URL="/uploads",
            MATERIAL_MAX_IMAGE_BYTES=settings.MATERIAL_MAX_IMAGE_BYTES,
            MATERIAL_MAX_VIDEO_BYTES=settings.MATERIAL_MAX_VIDEO_BYTES,
            MATERIAL_MAX_PER_OWNER=1,
        ),
    )
    project = _project(db)
    service = _service(db)
    rows, _ = _rendered(db, project.id, 2)
    service.save_frame_to_material(project.id, rows[0].id)

    with pytest.raises(ValueError, match="最多存 1 条"):
        service.save_frame_to_material(project.id, rows[1].id)

    assert db.scalar(select(func.count(MaterialAsset.id))) == 1
    kept = [path.name for path in (tmp_path / "materials" / str(OWNER)).iterdir()]
    assert len(kept) == 1


# ---- HTTP 层（产物存为素材）---------------------------------------------------


def test_save_frame_http_shows_up_in_the_library(client, db):
    """存完在素材库接口上就看得见：工作台的挑选框读的是那一条接口。"""
    project = _project(db)
    rows, jobs = _rendered(db, project.id)

    body = client.post(
        f"/api/office/videos/{project.id}/shots/{rows[0].id}/to-material"
    ).json()

    assert body["code"] == 0 and body["message"] == "已存进素材库"
    assert body["data"]["shots"][0]["image_job"]["status"] == JOB_SUCCEEDED
    listed = client.get("/api/office/materials").json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["name"] == "季度复盘三步｜第 1 镜首帧"
    asset = db.scalar(select(MaterialAsset))
    assert listed["items"][0]["url"] == f"/uploads/{asset.rel_path}"
    assert asset.rel_path != jobs[0].rel_path, "库里那份是另一份文件，不是产物本身"


def test_save_frame_http_rejects_a_shot_still_in_the_queue(client, db):
    project = _project(db)
    _image_model(db)
    row = _shots(db, project.id, 1)[0]

    body = client.post(
        f"/api/office/videos/{project.id}/shots/{row.id}/to-material"
    ).json()

    assert body["code"] == -1 and "还没有产物" in body["message"]
    assert client.post("/api/office/videos/999999/shots/1/to-material").status_code == 404


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


def test_items_summarize_current_render_jobs_and_latest_publish(db):
    project = _project(db)
    rows = _shots(db, project.id, 8)

    def put_job(shot, kind, status, *, current=True, owner_id=OWNER):
        job = MediaJob(
            owner_id=owner_id,
            kind=kind,
            biz_type=BIZ_VIDEO_SHOT,
            biz_id=str(shot.id),
            model_name="m",
            status=status,
            request={},
        )
        db.add(job)
        db.flush()
        if current:
            if kind == KIND_IMAGE:
                shot.image_job_id = job.id
            else:
                shot.video_job_id = job.id
        return job

    # 重做前的失败历史不算当前进度；当前指针指向的成功作业才算一镜完成。
    put_job(rows[0], KIND_IMAGE, JOB_FAILED, current=False)
    put_job(rows[0], KIND_IMAGE, JOB_SUCCEEDED)
    for row, status in zip(
        rows[1:7],
        [JOB_QUEUED, JOB_RUNNING, JOB_DOWNLOADING, JOB_FAILED, JOB_CANCELLED, JOB_EXPIRED],
    ):
        put_job(row, KIND_IMAGE, status)
    # 第 8 镜没有首帧作业，留在待渲染数里。

    put_job(rows[0], KIND_VIDEO, JOB_SUCCEEDED)
    put_job(rows[1], KIND_VIDEO, JOB_QUEUED)
    put_job(rows[2], KIND_VIDEO, JOB_CANCELLED)
    db.add_all(
        [
            PublishJob(
                owner_id=OWNER,
                project_id=project.id,
                platform="douyin",
                channel="manual",
                status="failed",
                error_msg="旧合成失败",
                request={},
            ),
            PublishJob(
                owner_id=OWNER,
                project_id=project.id,
                platform="douyin",
                channel="manual",
                status="succeeded",
                request={},
                rel_path=f"videos/{project.id}/abc123def4567890.mp4",
                mime="video/mp4",
                file_size=123,
            ),
            # id 最新但不属于当前账号，不能盖住本项目自己的最近作业。
            PublishJob(
                owner_id=OTHER,
                project_id=project.id,
                platform="douyin",
                channel="manual",
                status="failed",
                error_msg="其他账号的作业",
                request={},
            ),
        ]
    )
    db.commit()

    item = _service(db).to_items(_service(db).list_projects())[0]

    assert item.frames.model_dump() == {
        "total": 8,
        "succeeded": 1,
        "active": 3,
        "failed": 1,
        "cancelled": 1,
        "expired": 1,
    }
    assert item.clips.model_dump() == {
        "total": 8,
        "succeeded": 1,
        "active": 1,
        "failed": 0,
        "cancelled": 1,
        "expired": 0,
    }
    assert item.publish_job.status == "succeeded"
    assert item.publish_job.url == f"/uploads/videos/{project.id}/abc123def4567890.mp4"
    assert item.publish_job.file_size == 123
    detail = _service(db).to_detail(_service(db).get_project(project.id))
    assert detail.frames == item.frames
    assert detail.clips == item.clips
    assert detail.publish_job.id == item.publish_job.id


def test_detail_serializes_jobs_without_the_upstream_url(db):
    """界面上的地址只能是本站的 rel_path 拼出来的：上游那条 24 小时链接不出门。

    首帧与片段两条作业一起带回来，各认各的产物。
    """
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

    _video_model(db)
    clip = service.render_clips(project.id, [])[0]
    clip.status = JOB_SUCCEEDED
    clip.rel_path = f"videos/{project.id}/abc123def4567891.mp4"
    clip.mime = "video/mp4"
    clip.response = {"video_url": "https://ark.example.com/gone-in-24h.mp4"}
    db.commit()

    detail = service.to_detail(service.get_project(project.id))

    assert detail.shots[0].video_job.kind == KIND_VIDEO
    assert detail.shots[0].video_job.url == f"/uploads/videos/{project.id}/abc123def4567891.mp4"
    assert detail.shots[0].image_job.url.endswith(".png"), "两类作业各带各的产物"
    assert "ark.example.com" not in detail.model_dump_json()


# ---- 脚本生成现场 ------------------------------------------------------------


def _script_run(db, project: VideoProject) -> tuple[WorkflowRun, list[WorkflowRunStep]]:
    """认领本项目的那条运行与它的步骤行（创建项目时就建齐了，都是 pending）。"""
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.subject_ref == str(project.id)))
    steps = list(
        db.scalars(
            select(WorkflowRunStep)
            .where(WorkflowRunStep.run_id == run.id)
            .order_by(WorkflowRunStep.sort_order)
        ).all()
    )
    return run, steps


def test_detail_carries_the_script_run_breakdown(db):
    """两步各自的耗时都要在详情里。

    这条流程是「创作卡 → 分镜」两次模型调用，AI 网关的日志按调用分行：只报其中
    一行，用户会以为整个生成就是那几十秒，而前面那一步可能已经跑了几分钟。
    """
    project = _project(db)
    service = _service(db)
    run, steps = _script_run(db, project)
    assert [row.key for row in steps] == ["brief", "script"]

    started = datetime(2026, 11, 14, 23, 14, 14)
    run.status = RUN_RUNNING
    run.started_at = started
    steps[0].status = STEP_SUCCEEDED
    steps[0].started_at = started
    steps[0].finished_at = started + timedelta(seconds=247)
    steps[0].elapsed_ms = 247300
    steps[1].status = STEP_RUNNING
    steps[1].started_at = started + timedelta(seconds=247)
    db.commit()
    db.expire_all()

    detail = service.to_detail(service.get_project(project.id))

    assert detail.script_run.id == run.id
    assert detail.script_run.status == RUN_RUNNING
    assert [(row.key, row.status, row.elapsed_ms) for row in detail.script_run.steps] == [
        ("brief", STEP_SUCCEEDED, 247300),
        ("script", STEP_RUNNING, None),
    ]
    assert detail.script_run.steps[1].started_at is not None, "在跑的那步靠它算「已跑多久」"
    # 现场只报状态与耗时：prompt、产物、轨迹留在运行详情页，不跟着轮询来回搬。
    assert not {"prompt", "output", "parsed", "trace"} & set(VideoScriptStepOut.model_fields)

    run.status = RUN_SUCCEEDED
    run.elapsed_ms = 312500
    steps[1].status = STEP_SUCCEEDED
    steps[1].elapsed_ms = 65100
    db.commit()
    db.expire_all()

    done = service.to_detail(service.get_project(project.id)).script_run

    assert done.elapsed_ms == 312500, "合计取运行行的墙上时间，不是把各步相加"
    assert [row.elapsed_ms for row in done.steps] == [247300, 65100]


def test_detail_script_run_is_none_after_the_runs_are_pruned(db):
    """运行记录会被留存清理删掉，项目还在：详情照常给，只是没有现场可报。"""
    project = _project(db)
    run, steps = _script_run(db, project)
    for row in steps:
        db.delete(row)
    db.delete(run)
    db.commit()
    db.expire_all()

    detail = _service(db).to_detail(_service(db).get_project(project.id))

    assert detail.script_run is None


def test_detail_http_carries_the_script_run(client, db):
    project = _project(db)

    body = client.get(f"/api/office/videos/{project.id}").json()

    assert body["code"] == 0
    assert body["data"]["script_run"]["status"] == RUN_PENDING
    assert [row["key"] for row in body["data"]["script_run"]["steps"]] == ["brief", "script"]


def test_business_rows_still_carry_no_status_column(db):
    """状态只在运行行与作业行上：这两张表一长出 status 就有了第三份事实源。"""
    for model in (VideoProject, VideoShot):
        columns = {column.name for column in model.__table__.columns}
        assert not {"status", "error_msg"} & columns, model.__tablename__
    assert not {"rel_path", "image_url"} & {column.name for column in VideoShot.__table__.columns}



