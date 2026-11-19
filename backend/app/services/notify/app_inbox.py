"""App 通知收件箱：先持久化，再做进程内实时广播。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from app import database as app_db
from app.models.entities import AppMessage
from app.models.schemas import AppMessageItem
from app.services.notify.app_stream import app_stream

logger = logging.getLogger("yangvis.notify")

_owner_locks: dict[int, asyncio.Lock] = {}


def _insert(owner_id: int, event: str, title: str, body: str) -> AppMessageItem:
    with app_db.SessionLocal() as db:
        try:
            row = AppMessage(
                owner_id=owner_id,
                event=event,
                title=title,
                body=body or None,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return AppMessageItem.model_validate(row)
        except Exception:
            db.rollback()
            raise


async def record_and_publish(
    owner_id: int, event: str, *, title: str, body: str = ""
) -> AppMessageItem:
    """同一 owner 按“提交再广播”串行化，避免实时帧出现 ID 倒序。"""
    lock = _owner_locks.setdefault(owner_id, asyncio.Lock())
    async with lock:
        message = await asyncio.to_thread(_insert, owner_id, event, title, body)
        app_stream.publish(owner_id, message)
        return message


def list_latest(owner_id: int, limit: int = 50) -> list[AppMessageItem]:
    with app_db.SessionLocal() as db:
        rows = db.scalars(
            select(AppMessage)
            .where(AppMessage.owner_id == owner_id)
            .order_by(AppMessage.id.desc())
            .limit(limit)
        ).all()
        return [AppMessageItem.model_validate(row) for row in rows]


def list_after(owner_id: int, after_id: int, limit: int = 100) -> list[AppMessageItem]:
    with app_db.SessionLocal() as db:
        rows = db.scalars(
            select(AppMessage)
            .where(AppMessage.owner_id == owner_id, AppMessage.id > after_id)
            .order_by(AppMessage.id.asc())
            .limit(limit)
        ).all()
        return [AppMessageItem.model_validate(row) for row in rows]


def prune_app_messages(days: int = 30) -> int:
    cutoff = datetime.now() - timedelta(days=days)
    with app_db.SessionLocal() as db:
        result = db.execute(delete(AppMessage).where(AppMessage.created_at < cutoff))
        deleted = result.rowcount or 0
        db.commit()
    if deleted:
        logger.info("pruned %d app message row(s) older than %dd", deleted, days)
    return deleted
