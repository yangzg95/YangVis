"""发布作业的执行体：跑 TTS 与 ffmpeg、登记成片、把状态写回 ``publish_job``。

形态与路由里的 ``_script_task`` 同一约定：请求级 ``BackgroundTasks`` 里跑，函数体内
延迟导入 ``SessionLocal``（测试 monkeypatch 的是 ``app.database`` 上的名字，模块顶层
import 进来的副本替换不掉）。本地 ffmpeg 是秒级到几分钟的活，不像方舟任务要排队
几十分钟，所以不需要 media_job 那种常驻循环；但进程重启同样会把中途的任务丢下，
:func:`reset_stale_publish_jobs` 在启动时把它们收成明确的失败，而不是留一条永远
``running``、把「这个项目正在合成」的位子永久占住的行。

TTS 与 ffmpeg 同在这一个后台任务里顺序执行：配音句是入队时快照在 ``request.tts``
里的（之后改台词不影响这次合成），运行时重新解析用途 ``tts`` 的默认模型配置——
配置被删掉或测试状态失效时，让作业明确失败而不是偷偷退回无声片。
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import PublishJob
from app.models.schemas import ModelPurpose
from app.services.media import store, tts
from app.services.model_config import default_model_config
from app.services.publish import (
    PUBLISH_ACTIVE_STATES,
    PUBLISH_FAILED,
    PUBLISH_RUNNING,
    PUBLISH_SUCCEEDED,
)
from app.services.publish.composer import (
    ComposeProgress,
    ProgressCallback,
    PublishComposeError,
    VoiceoverLine,
    VoiceoverPlan,
    compose,
)
from app.timeutil import db_now

logger = logging.getLogger("yangvis.publish.runner")

settings = get_settings()


def _progress_reporter(db: Session, job: PublishJob) -> ProgressCallback:
    """把进度写进入队快照的独立键；相同内容不重复提交。"""
    last: ComposeProgress | None = None

    async def report(update: ComposeProgress) -> None:
        nonlocal last
        if update == last:
            return
        request = dict(job.request or {})
        request["progress"] = asdict(update)
        job.request = request
        db.commit()
        last = update

    return report


async def _synthesize_voiceover(
    db: Session,
    job: PublishJob,
    work_dir: Path,
    *,
    progress: ProgressCallback | None = None,
) -> VoiceoverPlan | None:
    """按作业快照逐句合成配音，返回 ffmpeg 用的时间轴计划；没开配音返回 None。

    台词快照在入队时写定（``request.tts.lines``），运行时不回查镜头——合成进行中
    用户改台词不该影响这次作业，与 ``sources`` 快照同一个理由。模型配置则相反，
    必须运行时重新解析默认项并要求探测通过：配置被删/失效时让作业明确失败，而不是
    拿一行历史 id 调一条已经不可用的密钥。
    """
    snapshot = (job.request or {}).get("tts")
    if not snapshot or not snapshot.get("enabled"):
        return None

    raw_lines = snapshot.get("lines") or []
    if not raw_lines:
        raise PublishComposeError("配音开关已打开，但这次作业没有快照到任何台词")

    config = default_model_config(db, job.owner_id, ModelPurpose.TTS)
    if config is None:
        raise PublishComposeError(
            "还没有配置用途为「语音」的默认模型：先在「模型与集成」里添加 TTS 配置"
        )
    if not config.last_test_ok:
        raise PublishComposeError(
            "语音模型还没通过连通性测试：先在「模型与集成」里测通再发起合成"
        )

    voice = (snapshot.get("voice") or "").strip() or settings.TTS_DEFAULT_VOICE.strip()
    if not voice:
        raise PublishComposeError(
            "没有可用的音色 ID：项目渲染参数与服务端 TTS_DEFAULT_VOICE 都为空"
        )

    total = sum(1 for item in raw_lines if (item.get("text") or "").strip())
    done = 0
    if progress is not None:
        await progress(ComposeProgress("tts", 5, "正在准备配音", 0, total))
    plan_lines: list[VoiceoverLine] = []
    for index, item in enumerate(raw_lines):
        text = (item.get("text") or "").strip()
        if not text:
            # 无台词镜头不入计划，时间轴上它本就是一段静音。
            continue
        if progress is not None:
            await progress(
                ComposeProgress(
                    "tts",
                    5 + round(25 * done / total),
                    f"正在合成第 {done + 1}/{total} 句配音",
                    done,
                    total,
                )
            )
        audio = await tts.synthesize(config, text=text, voice=voice)
        audio_path = work_dir / f"line_{index:02d}.mp3"
        audio_path.write_bytes(audio)
        plan_lines.append(
            VoiceoverLine(
                path=audio_path,
                sort_order=int(item.get("sort_order", index + 1)),
                start_s=float(item.get("start_s", 0)),
                slot_s=float(item.get("duration_s", 0)),
            )
        )
        done += 1
        if progress is not None:
            await progress(
                ComposeProgress(
                    "tts",
                    5 + round(25 * done / total),
                    f"已完成 {done}/{total} 句配音",
                    done,
                    total,
                )
            )

    if not plan_lines:
        raise PublishComposeError("配音开关已打开，但所有镜头的台词都是空的")
    logger.info(
        "voiceover synthesized: job=%s lines=%s voice=%s model=%s",
        job.id,
        len(plan_lines),
        voice,
        config.model_name,
    )
    return VoiceoverPlan(lines=plan_lines)


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
    report = _progress_reporter(db, job)
    logger.info("publish job started: id=%s project=%s", job.id, job.project_id)
    try:
        await report(ComposeProgress("validating", 2, "正在检查片段文件"))
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
            # 没有配音快照时返回 None，compose 走与以前完全一致的无声拼接路径。
            voiceover = await _synthesize_voiceover(db, job, work_dir, progress=report)
            await compose(sources, out_path, voiceover=voiceover, progress=report)
            # 登记这一步还要过文件头魔数与大小闸门：ffmpeg 真出了意外容器，
            # 这里会抛 MediaFileError，被下面同一档接住。
            await report(ComposeProgress("storing", 98, "正在校验并保存成片"))
            stored = store.store_file(job.project_id, out_path)

        job.rel_path = stored.rel_path
        job.mime = stored.mime
        job.file_size = stored.size
        job.status = PUBLISH_SUCCEEDED
        job.error_msg = None
        await report(ComposeProgress("succeeded", 100, "成片已保存"))
        logger.info(
            "publish job succeeded: id=%s project=%s bytes=%s elapsed=%.1fs",
            job.id,
            job.project_id,
            stored.size,
            time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - 见函数 docstring，失败只落作业行
        previous = (job.request or {}).get("progress") or {}
        job.status = PUBLISH_FAILED
        job.error_msg = str(exc)[:512]
        await report(
            ComposeProgress(
                "failed", int(previous.get("percent", 0)), "合成失败，请查看下方原因"
            )
        )
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
