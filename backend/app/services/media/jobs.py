"""媒体作业的入队、执行、轮询、取消与清理。

这一层只认 ``media_job``，不认识「镜头」：业务侧（``services/videos``）负责决定提示词
与参数、把作业入队、并把 ``video_shot.image_job_id`` 指向最新那条。状态与产物路径都
只在作业行上，业务行一份都不抄。

作业的 ``request`` 存的是「这次要发什么」的归一形状，里面用 ``rel_path`` 指本地文件：

* ``image``：``{"project_id", "prompt", "size", "images": [rel_path], "watermark"}``
* ``video``：``{"project_id", "prompt", "params": {...}, "images": [{"role", "rel_path"}]}``

base64 内联在发送那一刻才拼出来，不进数据库——否则一条 2K 首帧图要在库里躺 5MB。
视频参考是例外：方舟只收公网 URL，所以它拼的是 ``PUBLIC_MEDIA_BASE_URL`` + ``rel_path``。

**为什么是一个常驻循环而不是 ``BackgroundTasks``**：一条视频要在上游排队几十秒到
几分钟，而且成功之后还必须转存（上游链接 24 小时失效）。请求级后台任务的寿命跟着一次
调用走，进程一重启就没了；这种活必须能在重启后接着干。所以执行体是 lifespan 里的一
个 asyncio 任务，事实源是数据库：

* ``queued`` + ``submitted_at IS NULL`` —— 本地排队，等着占一个并发位子提交；
* 已提交（``submitted_at`` 有值）—— 视频按 ``remote_task_id`` 轮询，图片按「上次进程
  死在同步调用里」重来；
* ``downloading`` —— 已经拿到上游 URL（存在 ``response`` 里），接着转存。

并发位子按 ``(owner, kind)`` 在库里数出来，不在内存里数：密钥是按用户配的、上游额度
也是按账号算的，而进程重启或多开一个 worker 都会让内存计数器跟事实脱节——并发上限恰恰
是那种「错了就多花钱」的东西。位子只约束**提交**，不约束轮询：查询不占上游并发，而且
只有持续查才能发现跑完了。

本进程正在执行的作业 id 记在 :data:`_IN_FLIGHT` 里。它存在的唯一理由是把「这条还在
我手里跑」和「上个进程死时留下的」分开——两者在数据库里长得一模一样，把在跑的当成遗留
重来一次，就会出现两个协程写同一行。重启后它是空的，于是所有遗留行都正确地变成待接管。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models.entities import MediaJob, ModelConfig, VideoShot
from app.models.schemas import ModelPurpose
from app.services.media import (
    JOB_ACTIVE_STATES,
    JOB_CANCELLED,
    JOB_DOWNLOADING,
    JOB_EXPIRED,
    JOB_FAILED,
    JOB_FINAL_STATES,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    KIND_IMAGE,
    KIND_VIDEO,
    ROLE_FIRST_FRAME,
    ROLE_LAST_FRAME,
    ROLE_REFERENCE_IMAGE,
    ROLE_REFERENCE_VIDEO,
)
from app.services.media import ark, store
from app.services.model_config import default_model_config
from app.timeutil import db_now

logger = logging.getLogger("yangvis.media.jobs")

settings: Settings = get_settings()

# 一条作业最多提交几次（``attempts`` 只在 :func:`_submit` 里加，所以口径是「真发出去
# 过几次」）。只数「可重试」的那类失败（限流、5xx、网络、下载超时）：
# 内容审核与 Model ID 写错重试多少次都是同一个结果，重试只会白烧额度。
MEDIA_MAX_ATTEMPTS = 3
# 一轮 tick 最多推进多少条，防止某个账号堆了几百条时一轮把上游打满。
MEDIA_TICK_BATCH = 24

_ROLE_BY_NAME = {
    ROLE_FIRST_FRAME: ROLE_FIRST_FRAME,
    ROLE_LAST_FRAME: ROLE_LAST_FRAME,
    ROLE_REFERENCE_IMAGE: ROLE_REFERENCE_IMAGE,
    ROLE_REFERENCE_VIDEO: ROLE_REFERENCE_VIDEO,
}

# 本进程正在执行的作业 id（见模块 docstring）。
_IN_FLIGHT: Set[int] = set()


class MediaJobError(ValueError):
    """业务侧能预见的拒绝（没配模型、作业不在可取消的状态）。路由按 ``code=-1`` 呈现。"""


# ---- 配置解析 ---------------------------------------------------------------


def purpose_of(kind: str) -> ModelPurpose:
    return ModelPurpose.IMAGE if kind == KIND_IMAGE else ModelPurpose.VIDEO


def resolve_media_config(db: Session, owner_id: int, kind: str) -> ModelConfig:
    """按用途取该用户的默认媒体模型，不可用就抛 :class:`MediaJobError`。

    要求 ``last_test_ok``：媒体调用是**真花钱**的，凭证错了要靠上游报错才知道，等于每
    条作业都白烧一次抵扣。设置页的探测是免费的只读请求，先过它再入队。
    """
    purpose = purpose_of(kind)
    label = "图片生成" if kind == KIND_IMAGE else "视频生成"
    config = default_model_config(db, owner_id, purpose)
    if config is None:
        raise MediaJobError(
            f"尚未配置{label}模型：请到「设置 · 模型配置」新增一条用途为 {purpose.value} 的配置"
        )
    if not config.last_test_ok:
        raise MediaJobError(
            f"{label}模型「{config.title}」还没通过连通性测试：请先在设置页测试成功后再渲染"
        )
    return config


# ---- 入队 -------------------------------------------------------------------


def enqueue(
    db: Session,
    owner_id: int,
    *,
    kind: str,
    biz_type: str,
    biz_id: object,
    request: Dict[str, Any],
    config: ModelConfig,
) -> MediaJob:
    """写一条 ``queued`` 作业，**不发请求**。

    入队与提交分开，是为了让并发限制、幂等重试、重启续跑三件事只有「作业表」这一个
    地方要管。接口返回时作业已经在库里，界面立刻是「排队中」。
    """
    job = MediaJob(
        owner_id=owner_id,
        provider="ark",
        kind=kind,
        biz_type=biz_type,
        biz_id=str(biz_id),
        model_config_id=config.id,
        model_name=config.model_name,
        status=JOB_QUEUED,
        request=request,
    )
    db.add(job)
    db.flush()
    db.commit()
    logger.info(
        "media job queued: id=%s kind=%s biz=%s/%s model=%s",
        job.id,
        kind,
        biz_type,
        biz_id,
        config.model_name,
    )
    return job


# ---- 请求拼装 ---------------------------------------------------------------


def _image_transport() -> str:
    return (settings.MEDIA_IMAGE_TRANSPORT or "data_url").strip().lower()


def to_ark_image_url(rel_path: str) -> str:
    """把本地产物变成方舟能吃的 ``image_url``。

    默认读本地文件转 base64：``/uploads`` 在本地磁盘上，未必公网可达（本地开发时根本
    不可达）。``public_url`` 那一档留给把媒体目录挂在自己 CDN/反代后面的部署，省下每图
    几 MB 的请求体。
    """
    if _image_transport() == "public_url":
        base = (settings.PUBLIC_MEDIA_BASE_URL or "").rstrip("/")
        if not base:
            raise MediaJobError("MEDIA_IMAGE_TRANSPORT=public_url 必须同时配 PUBLIC_MEDIA_BASE_URL")
        url = store.resolve_url(rel_path)
        if not url:
            raise MediaJobError(f"参考图路径不合法：{rel_path!r}")
        return f"{base}/{url.lstrip('/')}"
    raw = store.read_blob(rel_path)
    mime, _ext = store.sniff_media(raw)
    return ark.image_data_url(raw, mime)


def to_ark_video_url(rel_path: str) -> str:
    """参考视频只能给公网地址：方舟的 ``video_url`` 不收 base64（只有图片收）。

    所以这一档没有 ``data_url`` 可选，``PUBLIC_MEDIA_BASE_URL`` 是硬前置。没配就当场
    判失败，而不是发一个方舟拉不到的地址过去——那要等异步报错才知道，还白占一个位子。
    """
    base = (settings.PUBLIC_MEDIA_BASE_URL or "").rstrip("/")
    if not base:
        raise MediaJobError(
            "参考视频需要公网地址：请在服务端配置 PUBLIC_MEDIA_BASE_URL（本站对外 origin）"
        )
    url = store.resolve_url(rel_path)
    if not url:
        raise MediaJobError(f"参考视频路径不合法：{rel_path!r}")
    return f"{base}/{url.lstrip('/')}"


def build_video_content(request: Dict[str, Any]) -> List[dict]:
    """把镜头提示词 + 参考素材拼成方舟的 ``content`` 数组。

    ``images`` 这个键同时装图片与视频条目，靠 ``role`` 分流：键名是历史形状，库里
    可能还躺着按它入队的作业，改不得。
    """
    images: List[Tuple[str, str]] = []
    videos: List[Tuple[str, str]] = []
    for item in request.get("images") or []:
        if not isinstance(item, dict):
            continue
        role = _ROLE_BY_NAME.get(str(item.get("role") or ""))
        rel_path = str(item.get("rel_path") or "")
        if role is None or not rel_path:
            logger.warning("media job image entry skipped, bad role or rel_path: %r", item)
            continue
        if role == ROLE_REFERENCE_VIDEO:
            videos.append((role, to_ark_video_url(rel_path)))
        else:
            images.append((role, to_ark_image_url(rel_path)))
    return ark.build_video_content(str(request.get("prompt") or ""), images, videos)


def _project_id_of(job: MediaJob) -> int:
    """产物按项目分目录，所以落盘前要知道项目 id。

    取 ``request`` 里业务侧显式写下的那个，而不是解析 ``biz_id``：``biz_id`` 是镜头 id，
    不是项目 id，猜不得。
    """
    value = (job.request or {}).get("project_id")
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise MediaJobError(f"作业 {job.id} 的 request 里没有 project_id，无法落盘") from None


def _video_params(job: MediaJob) -> Dict[str, Any]:
    """视频任务的参数：业务侧给什么发什么，Model 由配置决定。

    这里不兜默认值是有意的：1.0 系纯文生默认 16:9、2.x 默认 adaptive，分辨率默认档也不
    同。写死一种就是给另一种型号埋雷，所以必须是项目 ``params`` 里显式存下来的那一档。
    """
    params = (job.request or {}).get("params")
    return dict(params) if isinstance(params, dict) else {}


# ---- 状态推进 ---------------------------------------------------------------


def _mark_terminal(job: MediaJob, status: str, *, code: str = "", message: str = "") -> None:
    job.status = status
    job.error_code = code or None
    job.error_msg = message[:512] if message else None
    job.finished_at = db_now()


async def _store_artifact(db: Session, job: MediaJob, url: str) -> None:
    """把上游产物转存到本地磁盘——渲染链上唯一不能省的一步。"""
    max_bytes = (
        settings.MEDIA_VIDEO_MAX_BYTES
        if job.kind == KIND_VIDEO
        else settings.MEDIA_IMAGE_MAX_BYTES
    )
    job.status = JOB_DOWNLOADING
    db.commit()

    raw = await ark.download(url, max_bytes=max_bytes)
    blob = store.store_blob(_project_id_of(job), raw)
    job.rel_path = blob.rel_path
    job.mime = blob.mime
    job.file_size = blob.size
    job.status = JOB_SUCCEEDED
    job.error_code = None
    job.error_msg = None
    # 从提交算到产物落盘：这一列在工作台上就是「这条渲染花了多久」，含上游排队时间。
    # 图片那一次同步调用与视频的轮询等待都归在里面，口径只有一处。
    now = db_now()
    if job.submitted_at is not None:
        job.elapsed_ms = int((now - job.submitted_at).total_seconds() * 1000)
    job.finished_at = now
    db.commit()
    logger.info(
        "media job stored: id=%s kind=%s mime=%s bytes=%s -> %s",
        job.id,
        job.kind,
        blob.mime,
        blob.size,
        blob.rel_path,
    )


def _on_failure(db: Session, job: MediaJob, exc: Exception) -> None:
    """按 ``retriable`` 分流：可重试的回队列排队子，不可重试的直接判死。"""
    ark_error = exc if isinstance(exc, ark.ArkError) else None
    retriable = bool(ark_error and ark_error.retriable)
    code = ark_error.code if ark_error else type(exc).__name__
    message = str(exc)

    if retriable and job.attempts < MEDIA_MAX_ATTEMPTS:
        job.status = JOB_QUEUED
        # 「已提交」这个事实要跟着擦掉：留着它，下一轮会去轮询一个可能根本没建起来的
        # 任务，或者把一条待重发的图片作业当成在跑。
        job.submitted_at = None
        job.error_code = code
        job.error_msg = message[:512]
        job.last_polled_at = None
        db.commit()
        logger.warning(
            "media job %s requeued after a retriable error (attempt %s): %s",
            job.id,
            job.attempts,
            message[:200],
        )
        return

    if retriable:
        # 上游那句「稍后自动重试」此刻已经不再成立：预算用完了就得把「不再重试」说清楚，
        # 否则界面上留一条判死的作业配一句「稍后自动重试」，用户只能干等。
        message = f"{message}（已试 {job.attempts} 次仍失败，不再自动重试）"
    _mark_terminal(job, JOB_FAILED, code=code, message=message)
    db.commit()
    logger.warning(
        "media job failed: id=%s kind=%s code=%s error=%s", job.id, job.kind, code, message[:200]
    )


async def _submit(db: Session, job: MediaJob, config: ModelConfig) -> None:
    """把一条未提交的作业交给上游。"""
    job.submitted_at = db_now()
    job.attempts += 1
    job.last_polled_at = db_now()
    db.commit()

    if job.kind == KIND_IMAGE:
        request = job.request or {}
        images = [
            to_ark_image_url(str(path))
            for path in (request.get("images") or [])
            if str(path or "").strip()
        ]
        result = await ark.generate_image(
            config,
            prompt=str(request.get("prompt") or ""),
            size=str(request.get("size") or "2K"),
            images=images,
            watermark=bool(request.get("watermark")),
        )
        job.status = JOB_RUNNING
        job.response = {"url": result.url, "size": result.size}
        job.usage = result.usage
        db.commit()
        await _store_artifact(db, job, result.url)
        return

    submission = await ark.submit_video(
        config, content=build_video_content(job.request or {}), params=_video_params(job)
    )
    job.remote_task_id = submission.task_id
    job.status = JOB_RUNNING
    db.commit()


async def _poll(db: Session, job: MediaJob, config: ModelConfig) -> None:
    """查一次视频任务状态，并按上游给的结果往下推。"""
    state = await ark.get_video_task(config, str(job.remote_task_id))
    job.last_polled_at = db_now()
    job.status = state.status or job.status
    job.usage = state.usage or job.usage
    job.response = _trim(state.raw)

    if state.status in (JOB_FAILED, JOB_EXPIRED):
        _mark_terminal(
            job, state.status, code=state.error_code, message=state.error_message or "上游未给出原因"
        )
        db.commit()
        logger.warning(
            "media job failed upstream: id=%s code=%s error=%s",
            job.id,
            state.error_code,
            state.error_message,
        )
        return
    db.commit()

    if state.status == JOB_SUCCEEDED:
        if not state.video_url:
            _mark_terminal(job, JOB_FAILED, code="NoVideoUrl", message="上游报成功但没给视频地址")
            db.commit()
            return
        await _store_artifact(db, job, state.video_url)


async def _resume_download(db: Session, job: MediaJob) -> None:
    """从 ``response`` 里把上游 URL 捞出来接着存。"""
    body = job.response or {}
    url = str(body.get("url") or body.get("video_url") or "")
    if not url:
        _mark_terminal(
            job,
            JOB_FAILED,
            code="UrlLost",
            message="产物地址在上游响应里找不到了，请重新渲染",
        )
        db.commit()
        return
    await _store_artifact(db, job, url)


def _trim(body: Dict[str, Any]) -> Dict[str, Any]:
    """原始响应按字段截断后入库：留结构、砍长度。

    方舟任务体里是 URL、状态、用量，值都很短；真撑爆 JSON 列的是以后可能加的长字段，
    而那类值对排障没有意义。
    """
    trimmed: Dict[str, Any] = {}
    for key, value in (body or {}).items():
        if isinstance(value, str):
            trimmed[key] = value[:1024]
        elif isinstance(value, (dict, list, int, float, bool)) or value is None:
            trimmed[key] = value
        else:
            trimmed[key] = str(value)[:1024]
    return trimmed


# ---- 一轮 tick --------------------------------------------------------------


def _due_poll_ids(db: Session) -> List[int]:
    """到点该重查的上游视频任务。"""
    due_before = db_now() - timedelta(seconds=settings.MEDIA_POLL_INTERVAL)
    return [
        int(job_id)
        for job_id in db.execute(
            select(MediaJob.id)
            .where(MediaJob.kind == KIND_VIDEO)
            .where(MediaJob.status.in_((JOB_RUNNING, JOB_DOWNLOADING)))
            .where(MediaJob.submitted_at.is_not(None))
            .where(
                or_(MediaJob.last_polled_at.is_(None), MediaJob.last_polled_at < due_before)
            )
            .order_by(MediaJob.last_polled_at.is_(None).desc(), MediaJob.id)
            .limit(MEDIA_TICK_BATCH)
        ).scalars().all()
    ]


def _due_download_ids(db: Session) -> List[int]:
    """拿到 URL 却没存下来的（进程死在转存中途，或轮询刚置成这个状态）。"""
    return [
        int(job_id)
        for job_id in db.execute(
            select(MediaJob.id)
            .where(MediaJob.status == JOB_DOWNLOADING)
            .where(MediaJob.submitted_at.is_not(None))
            .order_by(MediaJob.id)
            .limit(MEDIA_TICK_BATCH)
        ).scalars().all()
    ]


def _due_image_ids(db: Session) -> List[int]:
    """图片作业里「上个进程死在同步调用里」的那一种遗留。

    图片没有外部任务 id，``running`` 且已提交只可能是崩在中途——本进程里那次调用要么
    已经返回、要么此刻正在 :data:`_IN_FLIGHT` 里，而后者在下面会被滤掉。待提交的队头
    不在这里取，它和视频走同一条并发口子（见 :func:`_due_submit_ids`）。
    """
    return [
        int(job_id)
        for job_id in db.execute(
            select(MediaJob.id)
            .where(MediaJob.kind == KIND_IMAGE)
            .where(MediaJob.status == JOB_RUNNING)
            .where(MediaJob.submitted_at.is_not(None))
            .order_by(MediaJob.id)
            .limit(MEDIA_TICK_BATCH)
        ).scalars().all()
    ]


def _slot_counts(db: Session) -> Dict[Tuple[int, str], int]:
    """每个 ``(owner, kind)`` 现在占着几个上游位子。"""
    rows = db.execute(
        select(MediaJob.owner_id, MediaJob.kind, func.count(MediaJob.id))
        .where(MediaJob.status.in_(tuple(JOB_ACTIVE_STATES)))
        .group_by(MediaJob.owner_id, MediaJob.kind)
    ).all()
    return {(int(owner), str(kind)): int(count) for owner, kind, count in rows}


def _limits() -> Dict[str, int]:
    return {KIND_VIDEO: settings.VIDEO_MAX_INFLIGHT, KIND_IMAGE: settings.IMAGE_MAX_INFLIGHT}


def _due_submit_ids(db: Session) -> List[int]:
    """两类作业共用的提交口子：每个 ``(owner, kind)`` 只取够位子的队头。

    图片也必须走这里——它没有外部任务、看起来「一条同步调用而已」，但账号级的并发
    额度是**图片和视频一起数**的。以前只有视频受约束，一次「全片重渲」十几个镜头会把
    图片同步调用一次性打出去，撞回一堆 429，而 429 又让作业回队列重排，等于自己
    把自己堵死。
    """
    occupied = _slot_counts(db)
    limits = _limits()
    picked: List[int] = []
    groups = db.execute(
        select(MediaJob.owner_id, MediaJob.kind)
        .where(MediaJob.status == JOB_QUEUED)
        .where(MediaJob.submitted_at.is_(None))
        .group_by(MediaJob.owner_id, MediaJob.kind)
        .order_by(MediaJob.owner_id, MediaJob.kind)
    ).all()
    for owner, kind in groups:
        available = limits.get(kind, 1) - occupied.get((int(owner), kind), 0)
        if available <= 0:
            continue
        ids = db.execute(
            select(MediaJob.id)
            .where(MediaJob.owner_id == owner)
            .where(MediaJob.kind == kind)
            .where(MediaJob.status == JOB_QUEUED)
            .where(MediaJob.submitted_at.is_(None))
            .order_by(MediaJob.id)
            .limit(min(available, MEDIA_TICK_BATCH))
        ).scalars().all()
        picked.extend(int(job_id) for job_id in ids)
    return picked


def _expire_stale(db: Session, now: datetime) -> int:
    """老到上游大概已经不收查询的作业，本地判死并让出并发位子。"""
    cutoff = now - timedelta(seconds=settings.MEDIA_JOB_STALE_SECONDS)
    ids = [
        int(job_id)
        for job_id in db.execute(
            select(MediaJob.id)
            .where(MediaJob.created_at < cutoff)
            .where(MediaJob.status.notin_(tuple(JOB_FINAL_STATES)))
            .order_by(MediaJob.id)
            .limit(MEDIA_TICK_BATCH)
        ).scalars().all()
    ]
    if not ids:
        return 0
    hours = settings.MEDIA_JOB_STALE_SECONDS // 3600
    db.execute(
        update(MediaJob)
        .where(MediaJob.id.in_(ids))
        .values(
            status=JOB_EXPIRED,
            error_code="StaleJob",
            error_msg=f"超过 {hours} 小时仍未拿到结果，本地判死；如仍需要请重新渲染",
            finished_at=now,
        )
    )
    db.commit()
    logger.warning("expired %s media job(s) that outlived the upstream window", len(ids))
    return len(ids)


async def _advance(job_id: int) -> None:
    """推进一条作业，一条一个 session。

    ``Session`` 不是并发安全的，而一轮里会 ``gather`` 多条——与
    ``services/workflows/exec.py`` 里并行组「一步一 session」同一处理。
    """
    db = SessionLocal()
    try:
        job = db.get(MediaJob, job_id)
        if job is None:
            return
        config = _config_for(db, job)
        if config is None:
            _mark_terminal(
                job,
                JOB_FAILED,
                code="NoModelConfig",
                message=f"模型配置 {job.model_config_id} 已被删除，请重新渲染",
            )
            db.commit()
            return

        if job.status == JOB_DOWNLOADING:
            await _resume_download(db, job)
        elif job.kind == KIND_IMAGE and job.status == JOB_QUEUED:
            await _submit(db, job, config)
        elif job.kind == KIND_IMAGE and job.status == JOB_RUNNING:
            # 上个进程死在同步调用中途：没有外部任务可接，重来一次。
            job.submitted_at = None
            db.commit()
            await _submit(db, job, config)
        elif job.kind == KIND_VIDEO and job.status == JOB_QUEUED:
            await _submit(db, job, config)
        elif job.kind == KIND_VIDEO and job.status == JOB_RUNNING:
            await _poll(db, job, config)
    except Exception as exc:  # noqa: BLE001 - 单条作业的失败不能拖垮整个循环
        db.rollback()
        fresh = db.get(MediaJob, job_id)
        if fresh is None:
            logger.error("media job vanished mid-flight: id=%s error=%s", job_id, exc, exc_info=True)
            return
        try:
            _on_failure(db, fresh, exc)
        except Exception:  # noqa: BLE001 - 连失败都写不进去就是数据库本身的问题
            logger.exception("media job failure could not be persisted: id=%s", job_id)
    finally:
        db.close()
        _IN_FLIGHT.discard(job_id)


def _config_for(db: Session, job: MediaJob) -> Optional[ModelConfig]:
    """按作业记下的配置 id 取凭证。

    取的是 id 而不是「当前默认」：默认位随时会换，一条已经花了钱的作业要用当时那一份
    跑完，否则用户改配置会把进行中的渲染指到另一个模型上去。
    """
    if job.model_config_id is None:
        return None
    return db.get(ModelConfig, job.model_config_id)


async def run_tick() -> int:
    """扫一遍作业表并推进它够得着的每一条，返回本轮派发的条数。

    先判死超龄的（让出位子），再取遗留图片、待提交（图片与视频共用同一套位子算法）、
    待轮询任务、待转存。几批的状态条件互不重叠，所以可以并发跑；唯一要滤的是本进程已
    经在跑的那几条。
    """
    db = SessionLocal()
    try:
        _expire_stale(db, db_now())
        images = _due_image_ids(db)
        submits = _due_submit_ids(db)
        polls = _due_poll_ids(db)
        downloads = _due_download_ids(db)
    finally:
        db.close()

    ids = [
        job_id
        for job_id in dict.fromkeys([*images, *submits, *polls, *downloads])
        if job_id not in _IN_FLIGHT
    ][:MEDIA_TICK_BATCH]
    if not ids:
        return 0

    _IN_FLIGHT.update(ids)
    results = await asyncio.gather(*[_advance(job_id) for job_id in ids], return_exceptions=True)
    for job_id, result in zip(ids, results):
        if isinstance(result, BaseException):
            # _advance 自己兜异常；走到这里说明兜异常的过程中又炸了（比如数据库断了）。
            _IN_FLIGHT.discard(job_id)
            logger.error("media advance escaped its own handler: id=%s error=%s", job_id, result)
    return len(ids)


# ---- 常驻循环 ---------------------------------------------------------------

_LOOP_TASK: Optional[asyncio.Task] = None
_LOOP_STOP: Optional[asyncio.Event] = None


async def _loop() -> None:
    logger.info(
        "media job loop started: video_inflight=%s image_inflight=%s poll=%.1fs",
        settings.VIDEO_MAX_INFLIGHT,
        settings.IMAGE_MAX_INFLIGHT,
        settings.MEDIA_POLL_INTERVAL,
    )
    assert _LOOP_STOP is not None
    while not _LOOP_STOP.is_set():
        handled = 0
        try:
            handled = await run_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 循环不能因为一轮失败就退出
            logger.exception("media job loop tick failed")
        # 空转时歇得久一点：没有作业时这个循环的唯一成本就是每次 tick 的几条查询。
        idle = settings.MEDIA_LOOP_IDLE if handled else settings.MEDIA_LOOP_IDLE * 4
        try:
            await asyncio.wait_for(_LOOP_STOP.wait(), timeout=idle)
        except asyncio.TimeoutError:
            pass
    logger.info("media job loop stopped")


def start_loop() -> None:
    """在 lifespan 里调用：起常驻循环。重复调用是幂等的。"""
    global _LOOP_TASK, _LOOP_STOP
    if _LOOP_TASK is not None and not _LOOP_TASK.done():
        return
    _LOOP_STOP = asyncio.Event()
    _LOOP_TASK = asyncio.create_task(_loop())
    # asyncio 只弱引用任务，不留强引用它可能在跑完前被回收（同 app/background.py 的理由）。
    _LOOP_REFS.add(_LOOP_TASK)
    _LOOP_TASK.add_done_callback(_LOOP_REFS.discard)


_LOOP_REFS: Set[asyncio.Task] = set()


async def stop_loop() -> None:
    """关停：先置停止位再等收尾。等不到就取消——作业状态都在库里，下个进程接着跑。"""
    global _LOOP_TASK, _LOOP_STOP
    if _LOOP_STOP is not None:
        _LOOP_STOP.set()
    task = _LOOP_TASK
    _LOOP_TASK = None
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


# ---- 取消与清理 -------------------------------------------------------------


async def cancel(db: Session, owner_id: int, job_id: int) -> MediaJob:
    """取消一条作业。

    没提交的只改本地状态；已提交且还在上游排队的要跟着发 DELETE，否则上游会照常把
    这条跑完并计费。已经完成的不给取消——那个动作要删产物，语义是「重做」，界面上
    应该走重做而不是取消。
    """
    job = db.get(MediaJob, job_id)
    if job is None or job.owner_id != owner_id:
        # 别人的作业与不存在同样处理：按 id 试探不出这张表里有哪些行。
        raise LookupError("作业不存在")
    if job.status == JOB_SUCCEEDED:
        raise MediaJobError("该渲染已经完成，请直接重做而不是取消")
    if job.status in (JOB_CANCELLED, JOB_EXPIRED, JOB_FAILED):
        return job

    if job.kind == KIND_VIDEO and job.remote_task_id and job.status in (JOB_QUEUED, JOB_RUNNING):
        config = _config_for(db, job)
        if config is not None:
            try:
                await ark.cancel_video_task(config, str(job.remote_task_id))
            except Exception as exc:  # noqa: BLE001 - 上游取消失败不改变本地已经放弃这个事实
                logger.warning("upstream cancel failed, cancelling locally anyway: job=%s error=%s", job.id, exc)

    _mark_terminal(job, JOB_CANCELLED, message="用户取消")
    db.commit()
    logger.info("media job cancelled: id=%s kind=%s", job.id, job.kind)
    return job


def prune_jobs(keep_days: Optional[int] = None) -> int:
    """删掉「既太老、又不是任何镜头当前指针」的失败/取消/判死记录（它们没有产物）。

    带产物的成功作业**不在清理范围内**：那一行可能就是镜头现在指着的那一份，删它等于
    删用户的成片。产物目录的清理跟着项目删除走（``store.delete_project_dir``）；按龄
    清文件要等界面上有了「历史渲染挑一版」才做得对——在那之前删文件就是删作品。
    """
    days = keep_days if keep_days is not None else settings.VIDEO_RETENTION_DAYS
    db = SessionLocal()
    try:
        cutoff = db_now() - timedelta(days=days)
        ids = [
            int(job_id)
            for job_id in db.execute(
                select(MediaJob.id)
                .where(MediaJob.created_at < cutoff)
                .where(MediaJob.status.in_((JOB_FAILED, JOB_CANCELLED, JOB_EXPIRED)))
                .where(MediaJob.rel_path.is_(None))
            ).scalars().all()
        ]
        if not ids:
            return 0
        wanted = set(ids)
        # 镜头指着的失败也要留下：那是这个镜头「为什么是红的」的唯一解释。
        referenced = set(
            db.execute(
                select(VideoShot.image_job_id).where(VideoShot.image_job_id.in_(wanted))
            ).scalars().all()
        ) | set(
            db.execute(
                select(VideoShot.video_job_id).where(VideoShot.video_job_id.in_(wanted))
            ).scalars().all()
        )
        doomed = [job_id for job_id in ids if job_id not in referenced]
        if not doomed:
            return 0
        db.execute(delete(MediaJob).where(MediaJob.id.in_(doomed)))
        db.commit()
        logger.info("pruned %s media job row(s) older than %s day(s)", len(doomed), days)
        return len(doomed)
    finally:
        db.close()


# ---- 读侧 -------------------------------------------------------------------


def jobs_by_ids(db: Session, owner_id: int, job_ids: List[int]) -> Dict[int, MediaJob]:
    """工作台一次取回多条作业（详情接口按镜头批量读，避免 N+1）。"""
    ids = [job_id for job_id in job_ids if job_id]
    if not ids:
        return {}
    rows = db.execute(
        select(MediaJob).where(MediaJob.owner_id == owner_id, MediaJob.id.in_(ids))
    ).scalars().all()
    return {job.id: job for job in rows}
