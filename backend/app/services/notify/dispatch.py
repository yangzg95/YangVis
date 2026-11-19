"""通知分发：事件先落 App 收件箱，再实时广播并发送外部 webhook。

三条不变量：

1. **绝不抛异常**。通知是旁路，任何失败都只能进日志文件和 ``notify_log``，
   不能顺着后台任务变成一条没人处理的 task 异常。
2. **不复用调用方 session**。这里自开短命 session，把同步 commit 放进工作线程。
3. **正文只带摘要**。title/body 由调用方负责脱敏，这里只兜底截断长度。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy import delete, select

from app import database as app_db
from app.crypto import decrypt
from app.models.entities import NotifyChannel, NotifyLog
from app.services.notify.app_inbox import record_and_publish
from app.services.notify.senders import TIMEOUT, send_to_channel
from app.timeutil import beijing_now

logger = logging.getLogger("yangvis.notify")

WEBHOOK_KINDS = frozenset({"feishu", "wecom", "dingtalk", "generic"})

# 与实体列宽对齐：title 128、log 表 error 512（senders 侧已截 512，这里再兜一道）。
_MAX_TITLE = 128
_MAX_BODY = 1024

STATUS_SENT = "sent"
STATUS_FAILED = "failed"


def render_text(title: str, body: str, now: Optional[datetime] = None) -> str:
    """群消息的纯文本形态：text 类机器人只吃这一个字段。"""
    moment = now if now is not None else beijing_now()
    return f"【yangvis】{title}\n{body}\n时间：{moment.strftime('%Y-%m-%d %H:%M:%S')}"


async def dispatch(owner_id: int, event: str, *, title: str, body: str = "") -> None:
    """保存一条 App 通知，并推给该用户订阅了事件的启用 webhook。"""
    title = title[:_MAX_TITLE]
    body = body[:_MAX_BODY]
    try:
        await record_and_publish(owner_id, event, title=title, body=body)
    except Exception:
        # App 落库失败不拦其他渠道；广播也只在这条提交成功后发生。
        logger.exception("failed to record app notification %s", event)

    try:
        channels = await asyncio.to_thread(_subscribed_channels, owner_id, event)
        if not channels:
            return
        text = render_text(title, body)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for channel in channels:
                result = await send_to_channel(
                    client,
                    kind=channel["kind"],
                    webhook_url=channel["url"],
                    secret=channel["secret"],
                    event=event,
                    title=title,
                    body=body,
                    text=text,
                )
                await asyncio.to_thread(
                    _write_log,
                    owner_id=owner_id,
                    channel_id=channel["id"],
                    channel_name=channel["name"],
                    event=event,
                    title=title,
                    body=body,
                    status=STATUS_SENT if result.ok else STATUS_FAILED,
                    error=result.error,
                    elapsed_ms=result.elapsed_ms,
                )
                if not result.ok:
                    logger.warning(
                        "notify channel %s (%s) failed: %s",
                        channel["id"],
                        channel["name"],
                        result.error,
                    )
    except Exception:  # pragma: no cover - 最外层兜底，任何意外都不外溢
        logger.exception("failed to dispatch notify event %s", event)


def _subscribed_channels(owner_id: int, event: str) -> list[dict]:
    """查启用且订阅了事件的 webhook，解出 URL/secret 后返回纯数据。"""
    rows: list[dict] = []
    with app_db.SessionLocal() as db:
        channels = db.scalars(
            select(NotifyChannel).where(
                NotifyChannel.owner_id == owner_id,
                NotifyChannel.enabled.is_(True),
                NotifyChannel.kind.in_(WEBHOOK_KINDS),
            )
        ).all()
        for channel in channels:
            if event not in (channel.events or []):
                continue
            try:
                url = decrypt(channel.webhook_url_enc)
                secret = decrypt(channel.secret_enc) if channel.secret_enc else None
            except Exception:
                logger.warning(
                    "notify channel %s (%s) has an undecryptable secret; skipped",
                    channel.id,
                    channel.name,
                )
                continue
            if not url:
                continue
            rows.append(
                {
                    "id": channel.id,
                    "name": channel.name,
                    "kind": channel.kind,
                    "url": url,
                    "secret": secret,
                }
            )
    return rows


def _write_log(
    *,
    owner_id: int,
    channel_id: int,
    channel_name: str,
    event: str,
    title: str,
    body: str,
    status: str,
    error: Optional[str],
    elapsed_ms: Optional[int],
) -> None:
    """一次发送一行留痕。失败也不抛——日志写不进去不该再去打扰主流程。"""
    try:
        with app_db.SessionLocal() as db:
            db.add(
                NotifyLog(
                    owner_id=owner_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    event=event,
                    title=title,
                    body=body or None,
                    status=status,
                    error=error[:512] if error else None,
                    elapsed_ms=elapsed_ms,
                )
            )
            db.commit()
    except Exception:  # pragma: no cover - 同上，兜底
        logger.exception("failed to write a notify log row")


def prune_notify_logs(days: int = 7) -> int:
    """清掉保留窗口之外的发送记录。挂在启动步骤里，全站口径。"""
    cutoff = datetime.now() - timedelta(days=days)
    with app_db.SessionLocal() as db:
        result = db.execute(delete(NotifyLog).where(NotifyLog.created_at < cutoff))
        deleted = result.rowcount or 0
        db.commit()
    if deleted:
        logger.info("pruned %d notify log row(s) older than %dd", deleted, days)
    return deleted
