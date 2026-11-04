"""通知分发：事件 → 订阅渠道 → 发送 → 落 ``notify_log``。

用法（触发点侧只有一行）::

    fire_and_forget(dispatch(owner_id, "workflow.failed", title=..., body=...))

三条不变量：

1. **绝不抛异常**。通知是旁路，调用方把它挂在主流程的关键节点上（工作流收尾、
   审批落库），任何失败都只能进日志文件和 ``notify_log``，不能顺着
   ``fire_and_forget`` 变成一条没人处理的 task 异常。
2. **不复用调用方 session**。触发点多在请求 session 的生命周期边缘（async
   generator 的结尾、启动阶段），这里自开短命 session，照 ``chat_ops.Auditor``
   的模式把同步 commit 放进工作线程。
3. **正文只带摘要**。title/body 由调用方负责脱敏（命令只给首词、SQL 不给全文），
   这里只兜底截断长度。
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
from app.services.notify.senders import TIMEOUT, send_to_channel
from app.timeutil import beijing_now

logger = logging.getLogger("yangvis.notify")

# 与实体列宽对齐：title 128、log 表 error 512（senders 侧已截 512，这里再兜一道）。
_MAX_TITLE = 128
_MAX_BODY = 1024

STATUS_SENT = "sent"
STATUS_FAILED = "failed"


def render_text(title: str, body: str, now: Optional[datetime] = None) -> str:
    """群消息的纯文本形态：text 类机器人只吃这一个字段。

    公开给 service.py 的连通测试复用——测试消息和正式事件必须是同一种
    形态，否则「测试通过、正式收不到」时没法对比排查。

    时间由这里决定而不是由调用方传：这条文本的读者在 +08，而跑 dispatch 的
    容器跟着自己的 ``TZ`` 走（部署侧没配就是 UTC），群里的时间戳会整整早 8 小时。
    ``now`` 只给单测留一个确定性的注入口。
    """
    moment = now if now is not None else beijing_now()
    return f"【yangvis】{title}\n{body}\n时间：{moment.strftime('%Y-%m-%d %H:%M:%S')}"


async def dispatch(owner_id: int, event: str, *, title: str, body: str = "") -> None:
    """把一个事件推给该用户所有订阅了它的启用渠道。

    永远正常返回；任何失败只留下日志。查渠道需要同步 DB 访问，放到工作线程
    里跑，避免在事件循环上做同步 IO（与 Auditor 同一个理由）。
    """
    title = title[:_MAX_TITLE]
    body = body[:_MAX_BODY]
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
    """查启用且订阅了该事件的渠道，解出 URL / secret 的明文。

    解密失败（ENCRYPTION_KEY 换过）的渠道跳过并发 warning：一个渠道的密钥
    失效不该拦住其他渠道的消息。返回纯数据而不是 ORM 对象——session 在本
    函数结束时关闭，detached 对象交给事件循环用是隐患。
    """
    rows: list[dict] = []
    with app_db.SessionLocal() as db:
        channels = db.scalars(
            select(NotifyChannel).where(
                NotifyChannel.owner_id == owner_id,
                NotifyChannel.enabled.is_(True),
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
                    status=status,
                    error=error[:512] if error else None,
                    elapsed_ms=elapsed_ms,
                )
            )
            db.commit()
    except Exception:  # pragma: no cover - 同上，兜底
        logger.exception("failed to write a notify log row")


def prune_notify_logs(days: int = 7) -> int:
    """清掉保留窗口之外的发送记录。挂在启动步骤里，全站口径。

    返回删除行数，供 bootstrap 日志输出。注意与 ``prune_runs`` 不同：这里没有
    「先收尾再清理」的顺序约束——被清掉的只是日志，不是状态本身。
    """
    cutoff = datetime.now() - timedelta(days=days)
    with app_db.SessionLocal() as db:
        result = db.execute(delete(NotifyLog).where(NotifyLog.created_at < cutoff))
        deleted = result.rowcount or 0
        db.commit()
    if deleted:
        logger.info("pruned %d notify log row(s) older than %dd", deleted, days)
    return deleted
