"""通知 webhook 的增删改查、掩码序列化与连通测试。

数据访问按 owner 隔离，他人的渠道 id 一律读作 404。旧华为渠道仅作为数据库
归档保留，不进入当前类型的查询和发送链路；历史 notify_log 仍正常可读。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto import DecryptionError, decrypt, encrypt, is_masked, mask
from app.models.entities import NotifyChannel, NotifyLog
from app.models.schemas import (
    NotifyChannelCreate,
    NotifyChannelItem,
    NotifyChannelUpdate,
    NotifyKind,
)
from app.services.notify.dispatch import WEBHOOK_KINDS, render_text
from app.services.notify.events import normalize_events
from app.services.notify.senders import TIMEOUT, SendResult, send_to_channel

logger = logging.getLogger("yangvis.notify")

_TEST_EVENT = "test"


def _validate_webhook_url(url: str) -> str:
    """只放行 http(s) 地址，其余（file://、javascript: …）直接拒。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("webhook 地址必须是 http(s):// 开头的完整 URL")
    return url.strip()


class NotifyChannelService:
    """针对单个用户的通知渠道做增删改查。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    def _scope(self, stmt: Select, entity=NotifyChannel) -> Select:
        # 旧华为行不出现在渠道 CRUD；发送审计日志没有 kind，仍只按 owner 过滤。
        stmt = stmt.where(entity.owner_id == self._owner_id)
        if entity is NotifyChannel:
            stmt = stmt.where(NotifyChannel.kind.in_(WEBHOOK_KINDS))
        return stmt

    # -- CRUD ---------------------------------------------------------------

    def list_channels(self) -> List[NotifyChannel]:
        stmt = self._scope(select(NotifyChannel)).order_by(NotifyChannel.id.asc())
        return list(self._db.scalars(stmt).all())

    def get_channel(self, channel_id: int) -> NotifyChannel:
        channel = self._db.scalar(
            self._scope(select(NotifyChannel).where(NotifyChannel.id == channel_id))
        )
        if channel is None:
            raise LookupError("渠道不存在")
        return channel

    def create_channel(self, payload: NotifyChannelCreate) -> NotifyChannel:
        url = _validate_webhook_url(payload.webhook_url)
        channel = NotifyChannel(
            owner_id=self._owner_id,
            name=payload.name.strip(),
            kind=payload.kind.value,
            webhook_url_enc=encrypt(url),
            secret_enc=encrypt(payload.secret.strip()) if payload.secret else None,
            events=normalize_events(payload.events),
            enabled=payload.enabled,
        )
        self._db.add(channel)
        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            raise ValueError("同名渠道已存在") from exc
        self._db.refresh(channel)
        logger.info("owner %s created notify channel %s (%s)", self._owner_id, channel.id, channel.name)
        return channel

    def update_channel(self, channel_id: int, payload: NotifyChannelUpdate) -> NotifyChannel:
        channel = self.get_channel(channel_id)
        data = payload.model_dump(exclude_unset=True)

        if data.get("name"):
            channel.name = payload.name.strip()
        if data.get("kind") is not None:
            channel.kind = data["kind"]

        # 空串或掩码都表示「保持原样」。
        url = data.get("webhook_url")
        secret = data.get("secret")
        credentials_changed = False
        if url is not None and url != "" and not is_masked(url):
            channel.webhook_url_enc = encrypt(_validate_webhook_url(url))
            credentials_changed = True
        if secret is not None and secret != "" and not is_masked(secret):
            channel.secret_enc = encrypt(secret.strip())
            credentials_changed = True

        if "events" in data:
            channel.events = normalize_events(data["events"] or [])
        if data.get("enabled") is not None:
            channel.enabled = data["enabled"]

        if credentials_changed:
            channel.last_test_ok = None
            channel.last_test_error = None

        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            raise ValueError("同名渠道已存在") from exc
        self._db.refresh(channel)
        logger.info("owner %s updated notify channel %s", self._owner_id, channel.id)
        return channel

    def delete_channel(self, channel_id: int) -> None:
        channel = self.get_channel(channel_id)
        self._db.delete(channel)
        self._db.commit()
        logger.info("owner %s deleted notify channel %s", self._owner_id, channel_id)

    # -- 连通测试 -----------------------------------------------------------

    async def send_test_message(self, channel: NotifyChannel) -> SendResult:
        """真的发一条测试消息，配置解密失败压成失败结果而不是异常。"""
        try:
            url = decrypt(channel.webhook_url_enc)
            secret = decrypt(channel.secret_enc) if channel.secret_enc else None
        except DecryptionError as exc:
            return SendResult(ok=False, error=str(exc))

        text = render_text("测试消息", "渠道连通测试：收到这条消息说明配置正确。")
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return await send_to_channel(
                client,
                kind=channel.kind,
                webhook_url=url,
                secret=secret,
                event=_TEST_EVENT,
                title="测试消息",
                body="渠道连通测试：收到这条消息说明配置正确。",
                text=text,
            )

    def record_test(self, channel: NotifyChannel, *, ok: bool, error: Optional[str]) -> None:
        channel.last_test_at = datetime.now()
        channel.last_test_ok = ok
        channel.last_test_error = None if ok else (error or "")[:256]
        self._db.commit()

    # -- 发送记录 -----------------------------------------------------------

    def list_logs(self, limit: int = 50) -> List[NotifyLog]:
        stmt = (
            self._scope(select(NotifyLog), entity=NotifyLog)
            .order_by(NotifyLog.id.desc())
            .limit(limit)
        )
        return list(self._db.scalars(stmt).all())

    # -- 序列化 -------------------------------------------------------------

    @staticmethod
    def to_item(channel: NotifyChannel) -> NotifyChannelItem:
        try:
            url = mask(decrypt(channel.webhook_url_enc))
            url_error: Optional[str] = None
        except DecryptionError as exc:
            logger.warning("failed to decrypt the webhook url of channel %s", channel.id)
            url = ""
            url_error = str(exc)

        secret_error: Optional[str] = None
        if channel.secret_enc:
            try:
                decrypt(channel.secret_enc)
            except DecryptionError as exc:
                logger.warning("failed to decrypt the secret of channel %s", channel.id)
                secret_error = str(exc)

        return NotifyChannelItem(
            id=channel.id,
            name=channel.name,
            kind=NotifyKind(channel.kind),
            webhook_url=url,
            webhook_url_error=url_error,
            has_secret=bool(channel.secret_enc and not secret_error),
            secret_error=secret_error,
            events=list(channel.events or []),
            enabled=channel.enabled,
            last_test_at=channel.last_test_at,
            last_test_ok=channel.last_test_ok,
            last_test_error=channel.last_test_error,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )
