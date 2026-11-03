"""通知渠道的增删改查、掩码序列化与连通测试。

数据访问按 owner 隔离（与 :class:`NoteService` 同一约定）：所有查询都经过
``_scope``，他人的渠道 id 一律读作 404。

凭据语义与 ``ops_server`` / ``ai_channel`` 一套：webhook_url 与 secret 落库为
Fernet 密文，列表只回掩码；更新时空串或掩码都表示「保持原样」——只改名的
请求会把掩码原样提交上来，把它当新值写回去等于悄悄清掉一个能用的机器人。
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
from app.services.notify.dispatch import render_text
from app.services.notify.events import normalize_events
from app.services.notify.senders import TIMEOUT, send_to_channel

logger = logging.getLogger("yangvis.notify")

# 测试消息固定文案；event 用保留字 "test"，不会与订阅词表冲突。
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

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select, entity=NotifyChannel) -> Select:
        # entity 参数化的原因：logs 查询作用于 NotifyLog，WHERE 里引用错了
        # 实体的 owner_id 不会报错，只会生成一个静默查空的笛卡尔积。
        return stmt.where(entity.owner_id == self._owner_id)

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
            channel.name = data["name"].strip()
        if data.get("kind") is not None:
            channel.kind = data["kind"]

        # 凭据「保持原样」的判定与 AiGatewayService.update_channel 同一约定。
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
            # 换过地址或密钥，之前的测试结论就不再成立。
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

    async def send_test_message(self, channel: NotifyChannel):
        """给渠道发一条测试消息，返回发送器的结果。

        解密失败在这里压成失败结果（而不是抛异常）：测试按钮的语义就是
        「告诉我现在通不通」，密钥解不开也是一种明确的不通。
        """
        from app.services.notify.senders import SendResult

        try:
            url = decrypt(channel.webhook_url_enc)
            secret = decrypt(channel.secret_enc) if channel.secret_enc else None
        except DecryptionError as exc:
            return SendResult(ok=False, error=str(exc))

        now = datetime.now()
        text = render_text("测试消息", "渠道连通测试：收到这条消息说明配置正确。", now)
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
