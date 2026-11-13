"""媒体作业的状态机：入队、提交、轮询、转存、重试、取消与清理。

上游那一层在 ``test_media_ark.py`` 已经钉死了，这里把 :mod:`app.services.media.ark`
整体换成假实现，只验**作业表**——它是渲染进度的唯一事实源，所以值得逐条状态迁移都
按上：哪些行该被本轮捞起来、失败之后回不回队列、重启之后谁接管、什么时候才算完。

执行体是 lifespan 里那个常驻循环，测试直接调 :func:`run_tick`（它自己开 session，
所以用的是 monkeypatch 过的 ``SessionLocal`` + 文件库）。``_advance`` 写的是另一条
连接，每次 tick 前后都要把测试这条 session 的读事务交出去，否则看见的一直是旧快照。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.crypto import encrypt
from app.database import Base
from app.models.entities import MediaJob, ModelConfig, VideoShot
from app.models.schemas import ModelPurpose
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
    ROLE_FIRST_FRAME,
)
from app.services.media import ark, jobs
from app.services.media import store as media_store
from app.timeutil import db_now

OWNER = 1
NEIGHBOUR = 2
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32

# 测试里的并发档压到 1：真实档（各 2）要在断言里数好几条作业才看得出漏了一个位子。
# 轮询间隔给负数，意思是「每一轮都到点」——不然用例得依赖两次 tick 之间真的隔了 5 秒。
LIMITS = SimpleNamespace(
    IMAGE_MAX_INFLIGHT=1,
    VIDEO_MAX_INFLIGHT=1,
    MEDIA_IMAGE_MAX_BYTES=1024,
    MEDIA_VIDEO_MAX_BYTES=2048,
    MEDIA_POLL_INTERVAL=-1.0,
    MEDIA_LOOP_IDLE=0.01,
    MEDIA_JOB_STALE_SECONDS=48 * 3600,
    MEDIA_IMAGE_TRANSPORT="data_url",
    PUBLIC_MEDIA_BASE_URL="",
    VIDEO_RETENTION_DAYS=30,
)


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'media.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture(autouse=True)
def no_leftover_in_flight():
    """``_IN_FLIGHT`` 是进程级集合：上一个用例漏一条，这一个就少派发一条。"""
    original = set(jobs._IN_FLIGHT)
    jobs._IN_FLIGHT.clear()
    yield
    jobs._IN_FLIGHT.clear()
    jobs._IN_FLIGHT.update(original)


@pytest.fixture()
def db(engine, monkeypatch, tmp_path):
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    # 两处 SessionLocal 都要换：``get_db`` 走 app.database 那个，而 run_tick / _advance
    # 是 jobs 顶层 import 进来的名字，只改前者会悄悄连到真库上（于是每条用例都是 0 条作业）。
    monkeypatch.setattr("app.database.SessionLocal", maker)
    monkeypatch.setattr(jobs, "SessionLocal", maker)
    # 三处 settings 快照都要换：jobs 数的是并发位子与产物上限，store 落的是盘。
    monkeypatch.setattr(jobs, "settings", LIMITS)
    monkeypatch.setattr(
        ark, "settings", SimpleNamespace(MEDIA_HTTP_TIMEOUT=1.0, MEDIA_DOWNLOAD_TIMEOUT=1.0)
    )
    monkeypatch.setattr(
        media_store,
        "settings",
        SimpleNamespace(upload_media_path=tmp_path, UPLOAD_MEDIA_URL="/uploads"),
    )
    monkeypatch.setattr(media_store, "_IMAGE_MAX_BYTES", 1024)
    monkeypatch.setattr(media_store, "_VIDEO_MAX_BYTES", 2048)
    for purpose in (ModelPurpose.IMAGE, ModelPurpose.VIDEO):
        session.add(
            ModelConfig(
                owner_id=OWNER,
                purpose=purpose.value,
                title=f"测试{purpose.value}模型",
                model_name=f"model-{purpose.value}",
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                api_key_enc=encrypt("sk-unit-test"),
                is_default=True,
                last_test_ok=True,
            )
        )
    session.commit()
    try:
        yield session
    finally:
        session.close()


class FakeArk:
    """按脚本演出的方舟：每类调用记一份，结果与异常都由用例摆好。"""

    def __init__(self, monkeypatch) -> None:
        self.images: list[dict] = []
        self.submits: list[dict] = []
        self.polled: list[str] = []
        self.cancelled: list[str] = []
        self.downloaded: list[str] = []
        self.image_error: Exception | None = None
        self.submit_error: Exception | None = None
        self.download_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.artifact_bytes = PNG
        # 最后一条留着不弹出：轮询用例要 tick 好几次，不该逼每个用例预先数准查询次数。
        self.poll_states: list[ark.TaskState] = []
        monkeypatch.setattr(ark, "generate_image", self._generate_image)
        monkeypatch.setattr(ark, "submit_video", self._submit_video)
        monkeypatch.setattr(ark, "get_video_task", self._get_video_task)
        monkeypatch.setattr(ark, "cancel_video_task", self._cancel_video_task)
        monkeypatch.setattr(ark, "download", self._download)

    async def _generate_image(self, config, **kwargs):
        self.images.append({"config": config.id, **kwargs})
        if self.image_error is not None:
            raise self.image_error
        return ark.ImageResult(
            url="https://ark.example.com/out.png", size="2048x2048", usage={"n": 1}
        )

    async def _submit_video(self, config, *, content, params=None):
        self.submits.append({"config": config.id, "content": content, "params": params or {}})
        if self.submit_error is not None:
            raise self.submit_error
        return ark.VideoSubmission(task_id=f"task-{len(self.submits)}", request={})

    async def _get_video_task(self, config, task_id):
        self.polled.append(task_id)
        if not self.poll_states:
            raise AssertionError("用例没有为这一次轮询预备状态")
        return self.poll_states.pop(0) if len(self.poll_states) > 1 else self.poll_states[0]

    async def _cancel_video_task(self, config, task_id):
        self.cancelled.append(task_id)
        if self.cancel_error is not None:
            raise self.cancel_error

    async def _download(self, url, *, max_bytes, timeout=None):
        self.downloaded.append(url)
        if self.download_error is not None:
            raise self.download_error
        return self.artifact_bytes


@pytest.fixture()
def fake_ark(monkeypatch) -> FakeArk:
    return FakeArk(monkeypatch)


def _tick(db) -> int:
    db.rollback()
    handled = asyncio.run(jobs.run_tick())
    db.rollback()
    db.expire_all()
    return handled


def _config(db, purpose: ModelPurpose) -> ModelConfig:
    return db.scalar(select(ModelConfig).where(ModelConfig.purpose == purpose.value))


def _queued(
    db,
    kind: str = KIND_IMAGE,
    *,
    biz_id: object = 11,
    biz_type: str = BIZ_VIDEO_SHOT,
    owner: int = OWNER,
    **over,
) -> MediaJob:
    """一条已入库的作业；状态与现场列都用关键字覆盖，不在用例里手工改实体属性。"""
    job = MediaJob(
        owner_id=owner,
        kind=kind,
        biz_type=biz_type,
        biz_id=str(biz_id),
        model_config_id=_config(
            db, ModelPurpose.IMAGE if kind == KIND_IMAGE else ModelPurpose.VIDEO
        ).id,
        model_name=f"model-{kind}",
        status=over.pop("status", JOB_QUEUED),
        request=over.pop("request", {"project_id": 7, "prompt": "一段提示词"}),
        **over,
    )
    db.add(job)
    db.commit()
    return job


# ---- 配置门槛 --------------------------------------------------------------


def test_media_config_must_exist_and_have_been_tested(db):
    assert jobs.resolve_media_config(db, OWNER, KIND_IMAGE).purpose == ModelPurpose.IMAGE.value
    assert jobs.resolve_media_config(db, OWNER, KIND_VIDEO).purpose == ModelPurpose.VIDEO.value

    with pytest.raises(jobs.MediaJobError, match="尚未配置视频生成模型"):
        jobs.resolve_media_config(db, NEIGHBOUR, KIND_VIDEO)

    video = _config(db, ModelPurpose.VIDEO)
    video.last_test_ok = False
    db.commit()
    with pytest.raises(jobs.MediaJobError, match="连通性测试"):
        jobs.resolve_media_config(db, OWNER, KIND_VIDEO)


def test_enqueue_writes_a_row_and_sends_nothing(db, fake_ark):
    job = jobs.enqueue(
        db,
        OWNER,
        kind=KIND_IMAGE,
        biz_type=BIZ_VIDEO_SHOT,
        biz_id=42,
        request={"project_id": 7, "prompt": "p"},
        config=_config(db, ModelPurpose.IMAGE),
    )

    assert job.status == JOB_QUEUED and job.submitted_at is None
    assert job.biz_id == "42", "业务 id 一律按字符串存，与 workflow_run.subject_ref 同形"
    assert (job.model_name, job.model_config_id) == (
        "model-image",
        _config(db, ModelPurpose.IMAGE).id,
    )
    assert fake_ark.images == [] and fake_ark.submits == []


# ---- 图片：一次同步调用 + 转存 --------------------------------------------


def test_image_job_ends_up_with_a_local_file(db, fake_ark):
    job = _queued(db)

    assert _tick(db) == 1
    db.refresh(job)

    assert job.status == JOB_SUCCEEDED
    assert job.submitted_at is not None and job.finished_at is not None
    assert job.rel_path.startswith("videos/7/") and job.rel_path.endswith(".png")
    assert job.mime == "image/png" and job.file_size == len(PNG)
    # 产物必须落在自己盘上：上游那条链接 24 小时就作废。
    assert (media_store.settings.upload_media_path / job.rel_path).read_bytes() == PNG
    assert job.response == {"url": "https://ark.example.com/out.png", "size": "2048x2048"}
    assert job.usage == {"n": 1}
    assert fake_ark.downloaded == ["https://ark.example.com/out.png"]
    # 工作台要报「这次渲染花了多久」：这一列在 MediaJobOut 里，作业层不写就永远空着。
    assert job.elapsed_ms is not None and job.elapsed_ms >= 0
    # 图片没有外部任务 id：能不能续跑只能看 submitted_at，这一列对图片永远是空。
    assert job.remote_task_id is None


def test_image_request_carries_the_reference_images_as_data_urls(db, fake_ark):
    blob = media_store.store_blob(7, PNG)
    job = _queued(
        db, request={"project_id": 7, "prompt": "p", "images": [blob.rel_path], "size": "1K"}
    )

    _tick(db)
    db.refresh(job)

    assert fake_ark.images[0]["images"][0].startswith("data:image/png;base64,")
    assert fake_ark.images[0]["size"] == "1K", "request 里给的档位原样发出去，不在这里兜默认"
    assert job.status == JOB_SUCCEEDED


def test_public_url_transport_is_used_when_configured(db, monkeypatch):
    monkeypatch.setattr(
        jobs,
        "settings",
        SimpleNamespace(
            **{
                **LIMITS.__dict__,
                "MEDIA_IMAGE_TRANSPORT": "public_url",
                "PUBLIC_MEDIA_BASE_URL": "https://cdn.example.com/",
            }
        ),
    )
    blob = media_store.store_blob(7, PNG)

    assert (
        jobs.to_ark_image_url(blob.rel_path)
        == f"https://cdn.example.com/uploads/{blob.rel_path}"
    )


def test_public_url_transport_without_a_base_is_refused(monkeypatch):
    monkeypatch.setattr(
        jobs,
        "settings",
        SimpleNamespace(**{**LIMITS.__dict__, "MEDIA_IMAGE_TRANSPORT": "public_url"}),
    )
    with pytest.raises(jobs.MediaJobError, match="PUBLIC_MEDIA_BASE_URL"):
        jobs.to_ark_image_url("videos/7/9b873576ec46468f.png")


def test_a_missing_reference_file_fails_that_job_only(db, fake_ark, monkeypatch):
    monkeypatch.setattr(
        jobs, "settings", SimpleNamespace(**{**LIMITS.__dict__, "IMAGE_MAX_INFLIGHT": 2})
    )
    _queued(
        db,
        biz_id=11,
        request={"project_id": 7, "prompt": "坏参考图", "images": ["videos/7/9b873576ec46468f.png"]},
    )
    healthy = _queued(db, biz_id=12)

    assert _tick(db) == 2

    db.expire_all()
    failed = db.scalar(select(MediaJob).where(MediaJob.biz_id == "11"))
    assert failed.status == JOB_FAILED
    assert failed.error_code == "MediaFileError" and "读不出来" in failed.error_msg
    # 参考图读不出来就不该发那条必然错的请求：这一批里只有健康那条真到了上游。
    assert [call["prompt"] for call in fake_ark.images] == ["一段提示词"]
    assert healthy.status == JOB_SUCCEEDED, "一条作业的文件问题不该连坐整个批次"


# ---- 失败分类与重试预算 ----------------------------------------------------


def test_a_retriable_image_error_goes_back_to_the_queue(db, fake_ark):
    job = _queued(db)
    fake_ark.image_error = ark.ArkError(
        "触发上游限流", code="QuotaExceeded", status_code=429, retriable=True
    )

    _tick(db)
    db.refresh(job)

    assert job.status == JOB_QUEUED
    # 「已提交」这个事实要跟着擦掉：留着它，下一轮会去轮询一个根本没建起来的活。
    assert job.submitted_at is None and job.attempts == 1
    assert job.error_code == "QuotaExceeded" and "限流" in job.error_msg
    assert job.finished_at is None, "回队列不是结束"

    fake_ark.image_error = None
    _tick(db)
    db.refresh(job)
    assert job.status == JOB_SUCCEEDED and job.attempts == 2


def test_the_retry_budget_eventually_ends_the_job(db, fake_ark):
    # 预算数的是「真发出去过几次」：上一次提交算第 3 次，失败就不该再排第 4 次。
    job = _queued(db, attempts=jobs.MEDIA_MAX_ATTEMPTS - 1)
    fake_ark.image_error = ark.ArkError("上游服务异常", code="Internal", status_code=500, retriable=True)

    _tick(db)
    db.refresh(job)

    assert job.status == JOB_FAILED and job.error_code == "Internal"
    assert job.attempts == jobs.MEDIA_MAX_ATTEMPTS
    assert "重试" in job.error_msg, "判死时要把「重试过」留在话里"


def test_a_content_review_failure_is_final_at_once(db, fake_ark):
    job = _queued(db)
    fake_ark.image_error = ark.ArkError("提示词未通过内容安全审核", code="ContentRisk", status_code=400)

    _tick(db)
    db.refresh(job)

    assert job.status == JOB_FAILED and job.error_code == "ContentRisk"
    assert job.finished_at is not None and len(fake_ark.images) == 1


def test_an_unexpected_error_keeps_its_class_name_as_the_code(db, fake_ark):
    """不是 ArkError 的炸法（磁盘满了、代码有 bug）也要留一个搜得到的 code。"""
    job = _queued(db)
    fake_ark.image_error = OSError("disk full")

    _tick(db)
    db.refresh(job)

    assert job.status == JOB_FAILED and job.error_code == "OSError" and "disk full" in job.error_msg


def test_a_job_whose_model_config_vanished_fails_clearly(db, fake_ark):
    job = _queued(db)
    db.query(ModelConfig).delete()
    db.commit()

    _tick(db)
    db.refresh(job)

    assert job.status == JOB_FAILED and job.error_code == "NoModelConfig"
    assert fake_ark.images == []


def test_a_dead_process_image_running_is_taken_over(db, fake_ark):
    """running + 已提交 = 上个进程死在同步调用里：没有外部任务可接，只能重来一次。"""
    job = _queued(db, status=JOB_RUNNING, submitted_at=db_now())

    assert _tick(db) == 1
    db.refresh(job)

    assert job.status == JOB_SUCCEEDED and len(fake_ark.images) == 1


# ---- 视频：提交 + 轮询 ----------------------------------------------------


def test_video_job_is_submitted_then_polled_then_stored(db, fake_ark):
    fake_ark.artifact_bytes = MP4
    fake_ark.poll_states = [
        ark.TaskState(task_id="task-1", status=JOB_RUNNING),
        ark.TaskState(
            task_id="task-1",
            status=JOB_SUCCEEDED,
            video_url="https://ark.example.com/1.mp4",
            usage={"seconds": 5},
            raw={"id": "task-1", "status": "succeeded", "content": {"video_url": "x"}},
        ),
    ]
    job = _queued(
        db,
        KIND_VIDEO,
        biz_id=7,
        biz_type=BIZ_VIDEO_COVER,
        request={"project_id": 7, "prompt": "动起来", "params": {"ratio": "9:16", "duration": 5}},
    )

    _tick(db)  # 提交
    db.refresh(job)
    assert (job.status, job.remote_task_id) == (JOB_RUNNING, "task-1")
    assert fake_ark.submits[0]["params"] == {"ratio": "9:16", "duration": 5}
    assert fake_ark.submits[0]["content"][0] == {"type": "text", "text": "动起来"}

    _tick(db)  # 第一次查：还在跑
    db.refresh(job)
    assert job.status == JOB_RUNNING and job.last_polled_at is not None
    assert job.finished_at is None

    _tick(db)  # 第二次查：出片，转存
    db.refresh(job)

    assert job.status == JOB_SUCCEEDED and job.rel_path.endswith(".mp4")
    assert job.mime == "video/mp4" and job.file_size == len(MP4)
    assert job.usage == {"seconds": 5}
    # 原始响应留在行上：以后要说「上游当时到底返回了什么」只能问这一列。
    assert job.response["id"] == "task-1"
    assert job.elapsed_ms is not None
    assert fake_ark.polled == ["task-1", "task-1"]
    assert fake_ark.downloaded == ["https://ark.example.com/1.mp4"]


def test_video_content_keeps_the_roles(db):
    blob = media_store.store_blob(7, PNG)
    request = {
        "project_id": 7,
        "prompt": "动起来",
        "images": [
            {"role": ROLE_FIRST_FRAME, "rel_path": blob.rel_path},
            {"role": "reference_video", "rel_path": blob.rel_path},
            {"role": "", "rel_path": blob.rel_path},
            {"role": ROLE_FIRST_FRAME, "rel_path": ""},
        ],
    }
    content = jobs.build_video_content(request)

    assert content[0] == {"type": "text", "text": "动起来"}
    assert [item["role"] for item in content[1:]] == ["first_frame"], "认不出的 role 直接丢掉"


def test_upstream_failure_and_expiry_are_recorded_as_they_come(db, fake_ark):
    failed = _queued(db, KIND_VIDEO, biz_id=7)
    fake_ark.poll_states = [
        ark.TaskState(
            task_id="task-1", status=JOB_FAILED, error_code="ContentRisk", error_message="涉政"
        )
    ]

    _tick(db)  # 提交
    _tick(db)  # 查询
    db.refresh(failed)

    assert failed.status == JOB_FAILED and failed.error_code == "ContentRisk"
    assert "涉政" in failed.error_msg

    fake_ark.poll_states = [ark.TaskState(task_id="task-2", status=JOB_EXPIRED)]
    aged = _queued(db, KIND_VIDEO, biz_id=8)

    _tick(db)
    _tick(db)
    db.refresh(aged)

    assert aged.status == JOB_EXPIRED, "上游的 expired 原样写回，不再翻译一层"
    assert "未给出原因" in aged.error_msg


def test_a_success_without_a_video_url_is_a_failure(db, fake_ark):
    job = _queued(db, KIND_VIDEO, biz_id=7)
    fake_ark.poll_states = [ark.TaskState(task_id="task-1", status=JOB_SUCCEEDED)]

    _tick(db)
    _tick(db)
    db.refresh(job)

    assert job.status == JOB_FAILED and job.error_code == "NoVideoUrl"


def test_downloading_survives_a_restart(db, fake_ark):
    """已经拿到 URL 没存下来：本轮 tick 直接接着转存，不重发一次生成。"""
    fake_ark.artifact_bytes = MP4
    job = _queued(
        db,
        KIND_VIDEO,
        biz_id=7,
        status=JOB_DOWNLOADING,
        submitted_at=db_now(),
        remote_task_id="task-9",
        response={"url": "https://ark.example.com/1.mp4"},
    )

    assert _tick(db) == 1
    db.refresh(job)

    assert job.status == JOB_SUCCEEDED and job.rel_path.endswith(".mp4")
    assert fake_ark.submits == [] and fake_ark.polled == []


def test_a_lost_artifact_url_is_reported_as_such(db, fake_ark):
    job = _queued(db, status=JOB_DOWNLOADING, submitted_at=db_now(), response={})

    _tick(db)
    db.refresh(job)

    assert job.status == JOB_FAILED and job.error_code == "UrlLost"
    assert fake_ark.downloaded == [], "连地址都没有，不该去下一个必然空的东西"


# ---- 取哪几条：状态条件与并发位子 ----------------------------------------


def test_only_the_slots_available_per_kind_are_submitted(db, fake_ark, monkeypatch):
    """一次「全片重渲」不能把图片同步调用一次性打出去：那是自己把自己堵死。"""
    monkeypatch.setattr(
        jobs, "settings", SimpleNamespace(**{**LIMITS.__dict__, "IMAGE_MAX_INFLIGHT": 2})
    )
    ids = [_queued(db, biz_id=biz_id).id for biz_id in (11, 12, 13)]

    assert _tick(db) == 2

    db.expire_all()
    states = {job.id: job.status for job in db.scalars(select(MediaJob)).all()}
    assert [states[job_id] for job_id in ids] == [JOB_SUCCEEDED, JOB_SUCCEEDED, JOB_QUEUED]


def test_the_video_slots_do_not_borrow_from_the_image_slots(db, fake_ark, monkeypatch):
    monkeypatch.setattr(
        jobs, "settings", SimpleNamespace(**{**LIMITS.__dict__, "IMAGE_MAX_INFLIGHT": 2})
    )
    for biz_id in (11, 12):
        _queued(db, biz_id=biz_id)
    video = _queued(db, KIND_VIDEO, biz_id=7)

    _tick(db)
    db.expire_all()

    assert video.status == JOB_RUNNING, "图片占满它自己那 2 个位子，视频的位子还空着"


def test_a_full_image_quota_blocks_the_next_submit(db, fake_ark):
    """位子是**在库里数**出来的：内存计数器重启就骗人，而这条约束恰恰在花真钱。"""
    for biz_id in (11, 12):
        _queued(db, biz_id=biz_id, status=JOB_RUNNING, submitted_at=db_now())
    waiting = _queued(db, biz_id=13)

    _tick(db)
    db.expire_all()

    assert waiting.status == JOB_QUEUED, "位子都被占着，队列里那条这轮不该发出去"


def test_finished_jobs_are_never_picked_up_again(db, fake_ark):
    for status in (JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED, JOB_EXPIRED):
        _queued(db, status=status)

    assert _tick(db) == 0
    assert fake_ark.images == []


def test_a_job_already_running_in_this_process_is_not_taken_twice(db, fake_ark):
    job = _queued(db, status=JOB_RUNNING, submitted_at=db_now())
    jobs._IN_FLIGHT.add(job.id)

    assert _tick(db) == 0
    assert fake_ark.images == [], "两个协程写同一行就是这个集合存在的唯一理由"


def test_stale_jobs_are_killed_locally_to_free_a_slot(db, fake_ark):
    job = _queued(
        db,
        KIND_VIDEO,
        biz_id=7,
        status=JOB_RUNNING,
        submitted_at=db_now(),
        remote_task_id="task-1",
        created_at=db_now() - timedelta(seconds=LIMITS.MEDIA_JOB_STALE_SECONDS + 60),
    )

    _tick(db)
    db.refresh(job)

    assert job.status == JOB_EXPIRED and job.error_code == "StaleJob"
    assert fake_ark.polled == [], "超龄的直接判死，不再花一次查询"


# ---- 取消与清理 --------------------------------------------------------------


def test_cancelling_a_queued_image_is_local_only(db, fake_ark):
    job = _queued(db)

    cancelled = asyncio.run(jobs.cancel(db, OWNER, job.id))

    assert cancelled.status == JOB_CANCELLED and cancelled.error_msg == "用户取消"
    assert fake_ark.cancelled == [] and fake_ark.submits == []


def test_cancelling_a_submitted_video_asks_upstream_too(db, fake_ark):
    job = _queued(
        db,
        KIND_VIDEO,
        biz_id=7,
        status=JOB_RUNNING,
        submitted_at=db_now(),
        remote_task_id="task-1",
    )

    asyncio.run(jobs.cancel(db, OWNER, job.id))

    assert fake_ark.cancelled == ["task-1"], "不发的话上游会照常跑完并计费"
    db.refresh(job)
    assert job.status == JOB_CANCELLED


def test_an_upstream_cancel_failure_still_frees_the_user(db, fake_ark):
    fake_ark.cancel_error = ark.ArkError("上游不理", code="Bad")
    job = _queued(
        db,
        KIND_VIDEO,
        biz_id=7,
        status=JOB_RUNNING,
        submitted_at=db_now(),
        remote_task_id="task-1",
    )

    cancelled = asyncio.run(jobs.cancel(db, OWNER, job.id))

    assert cancelled.status == JOB_CANCELLED, "上游不答应改变不了「用户已经放弃」这个事实"


def test_cancelling_something_done_or_borrowed(db, fake_ark):
    done = _queued(db, status=JOB_SUCCEEDED)
    with pytest.raises(jobs.MediaJobError, match="已经完成"):
        asyncio.run(jobs.cancel(db, OWNER, done.id))

    other = _queued(db)
    # 别人的作业与不存在同样处理：按 id 试探不出这张表里有哪些行。
    with pytest.raises(LookupError):
        asyncio.run(jobs.cancel(db, NEIGHBOUR, other.id))

    with pytest.raises(LookupError):
        asyncio.run(jobs.cancel(db, OWNER, 999999))


def test_cancelling_is_idempotent(db, fake_ark):
    job = _queued(db)

    assert asyncio.run(jobs.cancel(db, OWNER, job.id)).status == JOB_CANCELLED
    assert asyncio.run(jobs.cancel(db, OWNER, job.id)).status == JOB_CANCELLED
    assert fake_ark.cancelled == []


def test_prune_keeps_products_and_the_rows_shots_point_at(db):
    def aged(**over) -> MediaJob:
        cutoff = db_now() - timedelta(days=LIMITS.VIDEO_RETENTION_DAYS + 1)
        return _queued(db, created_at=cutoff, **over)

    doomed = aged(biz_id=101, status=JOB_FAILED)
    kept_product = aged(
        biz_id=102, status=JOB_SUCCEEDED, rel_path="videos/7/9b873576ec46468f.png"
    )
    referenced = aged(biz_id=103, status=JOB_CANCELLED)
    db.add(
        VideoShot(project_id=7, owner_id=OWNER, image_job_id=referenced.id, sort_order=1, start_s=0)
    )
    db.commit()
    # 先记下 id：prune 用的是另一条连接，删完再碰这个对象只会拿到「行已经不见了」。
    doomed_id, product_id, referenced_id = doomed.id, kept_product.id, referenced.id

    assert jobs.prune_jobs() == 1

    db.expire_all()
    left = {job.id for job in db.scalars(select(MediaJob)).all()}
    assert doomed_id not in left
    assert {product_id, referenced_id} <= left, "产物行与镜头指针指向的行都不能删"


def test_prune_of_a_fresh_table_does_nothing(db):
    _queued(db, status=JOB_FAILED)
    assert jobs.prune_jobs() == 0


# ---- 常驻循环 --------------------------------------------------------------


def test_the_loop_starts_once_and_stops_cleanly(db, fake_ark):
    job = _queued(db)

    async def main():
        jobs.start_loop()
        jobs.start_loop()  # 幂等：lifespan 之外再调一次不该起第二个循环
        task = jobs._LOOP_TASK
        await asyncio.sleep(0.2)
        await jobs.stop_loop()
        return task

    task = asyncio.run(main())

    assert task is not None and task.done() and not task.cancelled()
    assert len(fake_ark.images) == 1
    db.expire_all()
    assert db.scalar(select(MediaJob).where(MediaJob.id == job.id)).status == JOB_SUCCEEDED
    assert jobs._LOOP_TASK is None
