"""运维写命令的待确认项（两阶段落库确认）。

写确认不做「propose 之后协程干等用户点按钮」——那是 WebSocket 时代的做法，
依赖确认回复和等待协程在同一个进程里，gunicorn 多 worker 下无法搬到 SSE。
这里换成落库状态机：propose 落一行 pending 就返回，confirm 请求自己把行
认领（CAS）过来、在自己的 worker 上执行并写回结果。确认与执行永远在
同一次 HTTP 请求里，进程边界面天然消失；刷新页面后待确认卡片也能从
表里恢复。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import OpsPendingAction

logger = logging.getLogger("yangvis.ops.actions")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"

# 终态：不再接受任何状态迁移。
TERMINAL_STATUSES = (STATUS_REJECTED, STATUS_EXPIRED, STATUS_EXECUTED, STATUS_FAILED)


def _timeout() -> int:
    return get_settings().OPS_CONFIRM_TIMEOUT


def is_expired(action: OpsPendingAction, *, now: Optional[datetime] = None) -> bool:
    """pending 且超过确认时限。读取路径用这条惰性换算，不每次都回写库。"""
    if action.status != STATUS_PENDING:
        return False
    now = now or datetime.now()
    return action.created_at < now - timedelta(seconds=_timeout())


def display_status(action: OpsPendingAction) -> str:
    """对外展示用的状态：过期的 pending 直接报 expired。"""
    if is_expired(action):
        return STATUS_EXPIRED
    return action.status


def get_owned(db: Session, owner_id: int, action_id: int) -> OpsPendingAction:
    """按 owner 取一条待确认项；拿不到一律 LookupError（防存在性探测）。"""
    action = db.scalar(
        select(OpsPendingAction).where(
            OpsPendingAction.id == action_id,
            OpsPendingAction.owner_id == owner_id,
        )
    )
    if action is None:
        raise LookupError("待确认项不存在")
    return action


def list_for_conversation(
    db: Session, owner_id: int, conversation_id: int
) -> List[OpsPendingAction]:
    """一个会话的全部待确认项，按时间升序（回放顺序）。"""
    return list(
        db.scalars(
            select(OpsPendingAction)
            .where(
                OpsPendingAction.owner_id == owner_id,
                OpsPendingAction.conversation_id == conversation_id,
            )
            .order_by(OpsPendingAction.id.asc())
        ).all()
    )


def claim_for_confirm(db: Session, owner_id: int, action_id: int) -> OpsPendingAction:
    """把一条 pending 认领为 approved。并发/重复点击由这条单语句 CAS 裁决：
    InnoDB 行锁保证只有一个请求能看到 rowcount=1。过期视同已被拒绝——
    没人应答就执行写命令，正是这套设计要防的事。
    """
    cutoff = datetime.now() - timedelta(seconds=_timeout())
    result = db.execute(
        update(OpsPendingAction)
        .where(
            OpsPendingAction.id == action_id,
            OpsPendingAction.owner_id == owner_id,
            OpsPendingAction.status == STATUS_PENDING,
            OpsPendingAction.created_at >= cutoff,
        )
        .values(status=STATUS_APPROVED, resolved_at=datetime.now())
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[union-attr]
        raise LookupError("待确认项不存在、已处理或已超时")
    logger.debug("owner %s claimed ops action %s as approved", owner_id, action_id)
    return get_owned(db, owner_id, action_id)


def claim_for_reject(db: Session, owner_id: int, action_id: int) -> OpsPendingAction:
    """把一条 pending 标记为 rejected。过期的 pending 也允许标记（用户点的
    「拒绝」就是答案，只是来晚了），但已终态的不许重复迁移。"""
    result = db.execute(
        update(OpsPendingAction)
        .where(
            OpsPendingAction.id == action_id,
            OpsPendingAction.owner_id == owner_id,
            OpsPendingAction.status == STATUS_PENDING,
        )
        .values(status=STATUS_REJECTED, resolved_at=datetime.now())
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[union-attr]
        raise LookupError("待确认项不存在或已处理")
    logger.debug("owner %s marked ops action %s as rejected", owner_id, action_id)
    return get_owned(db, owner_id, action_id)


def finish_execution(
    db: Session,
    action: OpsPendingAction,
    *,
    success: bool,
    result: str,
    exit_status: Optional[int],
) -> None:
    """执行完写回结果。approved 是唯一的合法前态——别的方法来路说明状态机
    已经被并发搞乱了，宁可报错也不覆盖。"""
    if action.status != STATUS_APPROVED:
        raise RuntimeError(f"action #{action.id} 状态异常：{action.status}")
    action.status = STATUS_EXECUTED if success else STATUS_FAILED
    action.result = result
    action.exit_status = exit_status
    db.commit()
    logger.debug("ops action %s finished as %s", action.id, action.status)


def reset_stale_ops_actions(db: Session) -> None:
    """把上一个进程遗留的 pending 物化为 expired。

    与 reset_stale_indexing 同一个理由：进程重启之后，那些「等用户确认」的
    行永远等不到回答——发起它们的 SSE 流早就断了。启动时统一收尾，让它们
    在界面上呈现为「等待超时」而不是永远可点。
    """
    cutoff = datetime.now() - timedelta(seconds=_timeout())
    result = db.execute(
        update(OpsPendingAction)
        .where(
            OpsPendingAction.status == STATUS_PENDING,
            OpsPendingAction.created_at < cutoff,
        )
        .values(status=STATUS_EXPIRED, resolved_at=datetime.now())
    )
    db.commit()
    if result.rowcount:  # type: ignore[union-attr]
        logger.info("expired %d stale ops pending action(s)", result.rowcount)  # type: ignore[union-attr]
