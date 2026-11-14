"""发布作业的执行体：跑 ffmpeg、登记成片、把状态写回 ``publish_job``。

形态与路由里的 ``_script_task`` 同一约定：请求级 ``BackgroundTasks`` 里跑，函数体内
延迟导入 ``SessionLocal``（测试 monkeypatch 的是 ``app.database`` 上的名字，模块顶层
import 进来的副本替换不掉）。本地 ffmpeg 是秒级到几分钟的活，不像方舟任务要排队
几十分钟，所以不需要 media_job 那种常驻循环；但进程重启同样会把中途的任务丢下，
:func:`reset_stale_publish_jobs` 在启动时把它们收成明确的失败，而不是留一条永远
``running``、把「这个项目正在合成」的位子永久占住的行。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import PublishJob
from app.services.media import store
from app.services.publish import (
    PUBLISH_ACTIVE_STATES,
    PUBLISH_FAILED,
    PUBLISH_RUNNING,
    PUBLISH_SUCCEEDED,
)
from app.services.publish.composer import PublishComposeError, compose
from app.timeutil import db_now

logger = logging.getLogger("yangvis.publish.runner")

settings = get_settings()


async def run_publish_job(db: Session, job_id: int) -> None:
    """执行一条发布作业：校验片段文件 → ffmpeg 合成 → 登记进媒体目录。

    所有可预见的失败都落在作业行的 ``status/error_msg`` 上，不向外抛——调用方是
    后台任务，抛出去只会变成一条没人看见的日志和一条停在 running 的行。
    """
    job = db.get(PublishJob, job_id)
    if job is None or job.status not in PUBLISH_ACTIVE_STATES:
        return

    started = time.monotonic()
    job.status = PUBLISH_RUNNING
    db.commit()
    logger.info("publish job started: id=%s project=%s", job.id, job.project_id)
    try:
        sources = [
            settings.upload_media_path / str(rel)
            for rel in (job.request or {}).get("sources", [])
        ]
        if not sources:
            raise PublishComposeError("作业里没有记录任何片段，无法合成")
        missing = [str(path) for path in sources if not path.is_file()]
        if missing:
            # 片段可能被手工清过：在发 ffmpeg 之前说清楚是哪条，而不是等它报
            # 一句看不懂的 'No such file'。
            raise PublishComposeError(f"片段文件已不在服务器上：{'、'.join(missing[:3])}")

        # 临时目录开在媒体目录里面：同文件系统，登记时 shutil.move 是原子改名。
        with TemporaryDirectory(dir=settings.upload_media_path) as tmp:
            work_dir = Path(tmp)
            out_path = work_dir / "final.mp4"
            await compose(sources, out_path)
            # 登记这一步还要过文件头魔数与大小闸门：ffmpeg 真出了意外容器，
            # 这里会抛 MediaFileError，被下面同一档接住。
            stored = store.store_file(job.project_id, out_path)

        job.rel_path = stored.rel_path
        job.mime = stored.mime
        job.file_size = stored.size
        job.status = PUBLISH_SUCCEEDED
        job.error_msg = None
        logger.info(
            "publish job succeeded: id=%s project=%s bytes=%s elapsed=%.1fs",
            job.id,
            job.project_id,
            stored.size,
            time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - 见函数 docstring，失败只落作业行
        job.status = PUBLISH_FAILED
        job.error_msg = str(exc)[:512]
        logger.warning("publish job failed: id=%s error=%s", job.id, exc, exc_info=True)
    finally:
        job.elapsed_ms = int((time.monotonic() - started) * 1000)
        job.finished_at = db_now()
        db.commit()


def reset_stale_publish_jobs(db: Session) -> int:
    """把上个进程遗留在 queued/running 的发布作业收成失败，返回处理条数。"""
    rows = list(
        db.scalars(
            select(PublishJob).where(PublishJob.status.in_(tuple(PUBLISH_ACTIVE_STATES)))
        ).all()
    )
    for row in rows:
        row.status = PUBLISH_FAILED
        row.error_msg = "服务重启打断了这次合成，请重新发起"
        row.finished_at = db_now()
    if rows:
        db.commit()
        logger.warning("reset stale publish jobs on startup: count=%s", len(rows))
    return len(rows)


def reset_stale_on_startup() -> None:
    """lifespan 启动步骤的无参包装，与 bootstrap 那组收尾同一形状。"""
    from app.database import SessionLocal

    with SessionLocal() as db:
        reset_stale_publish_jobs(db)
