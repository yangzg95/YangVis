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
from app.models.entities import NotifyChannel, NotifyHuaweiConfig, NotifyLog
from app.models.schemas import (
    HuaweiChannelCreate,
    HuaweiChannelItem,
    HuaweiChannelUpdate,
    HuaweiTargetType,
    NotifyChannelCreate,
    NotifyChannelItem,
    NotifyChannelUpdate,
    NotifyKind,
)
from app.services.notify.dispatch import render_text
from app.services.notify.events import normalize_events
from app.services.notify.huawei import TARGET_TOPIC, send_huawei_message
from app.services.notify.senders import TIMEOUT, SendResult, send_to_channel

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
        # 华为渠道的 1:1 配置没有物理外键，需在这里显式连带删除，避免留孤儿行。
        if channel.kind == NotifyKind.HUAWEI.value:
            cfg = self._db.scalar(
                select(NotifyHuaweiConfig).where(
                    NotifyHuaweiConfig.channel_id == channel.id
                )
            )
            if cfg is not None:
                self._db.delete(cfg)
        self._db.delete(channel)
        self._db.commit()
        logger.info("owner %s deleted notify channel %s", self._owner_id, channel_id)

    # -- 华为 HMS Push 渠道 ---------------------------------------------------

    def get_huawei_config(self, channel: NotifyChannel) -> NotifyHuaweiConfig:
        cfg = self._db.scalar(
            select(NotifyHuaweiConfig).where(
                NotifyHuaweiConfig.channel_id == channel.id
            )
        )
        if cfg is None:
            raise LookupError("华为推送配置缺失")
        return cfg

    def create_huawei_channel(self, payload: HuaweiChannelCreate) -> NotifyChannel:
        """建华为渠道：NotifyChannel 占名（webhook 列存空 bytes 占位）+ 1:1 配置。"""
        channel = NotifyChannel(
            owner_id=self._owner_id,
            name=payload.name.strip(),
            kind=NotifyKind.HUAWEI.value,
            # 该列 NOT NULL，但华为渠道没有 webhook；encrypt("") 返回空 bytes（非 NULL）。
            webhook_url_enc=encrypt(""),
            secret_enc=None,
            events=normalize_events(payload.events),
            enabled=payload.enabled,
        )
        # flush 就可能撞 uk_notify_owner_name（与 commit 一样抛 IntegrityError），
        # 所以 add/flush/insert 全包进 try：重名要翻译成业务错误而不是 500。
        try:
            self._db.add(channel)
            # flush 拿到 channel.id，才能写 1:1 配置的外键值。
            self._db.flush()
            self._db.add(
                NotifyHuaweiConfig(
                    channel_id=channel.id,
                    app_id=payload.app_id.strip(),
                    client_id_enc=encrypt(payload.client_id.strip()),
                    client_secret_enc=encrypt(payload.client_secret.strip()),
                    target_type=payload.target_type.value,
                    target=payload.target.strip(),
                )
            )
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            raise ValueError("同名渠道已存在") from exc
        self._db.refresh(channel)
        logger.info(
            "owner %s created huawei notify channel %s (%s)",
            self._owner_id,
            channel.id,
            channel.name,
        )
        return channel

    def update_huawei_channel(
        self, channel_id: int, payload: HuaweiChannelUpdate
    ) -> NotifyChannel:
        channel = self.get_channel(channel_id)
        if channel.kind != NotifyKind.HUAWEI.value:
            raise ValueError("该渠道不是华为推送渠道")
        cfg = self.get_huawei_config(channel)

        credentials_changed = False
        if payload.name is not None:
            channel.name = payload.name.strip()
        if payload.events is not None:
            channel.events = normalize_events(payload.events)
        if payload.enabled is not None:
            channel.enabled = payload.enabled

        if payload.app_id is not None:
            cfg.app_id = payload.app_id.strip()
            credentials_changed = True
        # 空串或掩码都表示「保持原样」，与 webhook 渠道同一套凭据语义。
        if payload.client_id and not is_masked(payload.client_id):
            cfg.client_id_enc = encrypt(payload.client_id.strip())
            credentials_changed = True
        if payload.client_secret and not is_masked(payload.client_secret):
            cfg.client_secret_enc = encrypt(payload.client_secret.strip())
            credentials_changed = True
        if payload.target_type is not None:
            cfg.target_type = payload.target_type.value
            credentials_changed = True
        if payload.target and not is_masked(payload.target):
            cfg.target = payload.target.strip()
            credentials_changed = True

        if credentials_changed:
            # 凭据/目标变了，之前的连通测试结论不再成立。
            channel.last_test_ok = None
            channel.last_test_error = None

        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            raise ValueError("同名渠道已存在") from exc
        self._db.refresh(channel)
        logger.info("owner %s updated huawei notify channel %s", self._owner_id, channel.id)
        return channel

    @staticmethod
    def to_huawei_item(
        channel: NotifyChannel, cfg: NotifyHuaweiConfig
    ) -> HuaweiChannelItem:
        """华为渠道的掩码视图：client_id 回掩码，secret 只回有没有。"""
        client_id_masked = ""
        client_id_error: Optional[str] = None
        try:
            client_id_masked = mask(decrypt(cfg.client_id_enc))
        except DecryptionError as exc:
            logger.warning("failed to decrypt huawei client_id of channel %s", channel.id)
            client_id_error = str(exc)

        has_client_secret = True
        client_secret_error: Optional[str] = None
        try:
            decrypt(cfg.client_secret_enc)
        except DecryptionError as exc:
            logger.warning("failed to decrypt huawei client_secret of channel %s", channel.id)
            has_client_secret = False
            client_secret_error = str(exc)

        # target 明文落库：topic 名不敏感可原样回；设备 token 能定向推送，回掩码。
        target = cfg.target if cfg.target_type == TARGET_TOPIC else mask(cfg.target)

        return HuaweiChannelItem(
            id=channel.id,
            name=channel.name,
            kind=NotifyKind.HUAWEI,
            app_id=cfg.app_id,
            client_id_masked=client_id_masked,
            has_client_secret=has_client_secret,
            client_id_error=client_id_error,
            client_secret_error=client_secret_error,
            target_type=HuaweiTargetType(cfg.target_type),
            target=target,
            events=list(channel.events or []),
            enabled=channel.enabled,
            last_test_at=channel.last_test_at,
            last_test_ok=channel.last_test_ok,
            last_test_error=channel.last_test_error,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )

    # -- 连通测试 -----------------------------------------------------------

    async def send_test_message(self, channel: NotifyChannel):
        """给渠道发一条测试消息，返回发送器的结果。

        解密失败在这里压成失败结果（而不是抛异常）：测试按钮的语义就是
        「告诉我现在通不通」，密钥解不开也是一种明确的不通。
        """
        if channel.kind == NotifyKind.HUAWEI.value:
            return await self._send_huawei_test(channel)

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

    async def _send_huawei_test(self, channel: NotifyChannel) -> SendResult:
        """华为渠道的连通测试：真的发一条测试通知到配置的主题/设备。

        与 webhook 测试同一语义（端到端发一条），配置缺失或解密失败压成
        失败结果，不抛异常。
        """
        try:
            cfg = self.get_huawei_config(channel)
            client_id = decrypt(cfg.client_id_enc)
            client_secret = decrypt(cfg.client_secret_enc)
        except (LookupError, DecryptionError) as exc:
            return SendResult(ok=False, error=str(exc))

        if not (cfg.app_id and client_id and client_secret and cfg.target):
            return SendResult(ok=False, error="华为推送配置不完整")

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return await send_huawei_message(
                client,
                app_id=cfg.app_id,
                client_id=client_id,
                client_secret=client_secret,
                target_type=cfg.target_type,
                target=cfg.target,
                title="测试消息",
                body="渠道连通测试：收到这条通知说明华为推送配置正确。",
                data={"event": _TEST_EVENT},
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
