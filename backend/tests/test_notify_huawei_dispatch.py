"""dispatch 的华为分支：配置加载、跳过语义、留痕与多渠道并存。

发送器协议在 test_notify_huawei.py 里已用 MockTransport 钉死，这里把
``send_huawei_message`` 整个打桩，只验 dispatch 自己的职责：

- 从 1:1 配置构造 HuaweiAuth（client_credentials / 服务账号 JWT 二选一）；
- 配置缺失 / 解不开 / 字段不全的渠道安静跳过，不拦住其他渠道；
- 成功失败都写 notify_log（含新增的 body 列），且永不抛异常。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.crypto import encrypt
from app.database import Base
from app.models.entities import NotifyChannel, NotifyHuaweiConfig, NotifyLog
from app.services.notify import dispatch as dispatch_module
from app.services.notify.dispatch import (
    STATUS_FAILED,
    STATUS_SENT,
    dispatch,
)
from app.services.notify.huawei import (
    GRANT_CLIENT_CREDENTIALS,
    GRANT_JWT_BEARER,
)
from app.services.notify.senders import SendResult

OWNER = 1
EVENT = "workflow.failed"

APP_ID = "119672293"
CLIENT_ID = "oauth-client-id"
CLIENT_SECRET = "oauth-client-secret"
TOPIC = "yangvis"


@pytest.fixture(scope="module")
def service_account_text() -> str:
    """测试用 AGC 服务账号 JSON（现签私钥，非真实凭据）。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {"key_id": "kid-1", "sub_account": "sub-1", "private_key": pem}
    )


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'notify-hw.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture()
def db(engine, monkeypatch):
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    monkeypatch.setattr("app.database.SessionLocal", maker)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def stub_huawei(monkeypatch):
    calls: list[dict] = []
    queue: list[SendResult] = []

    async def fake(client, **kwargs):
        calls.append(kwargs)
        if queue:
            return queue.pop(0)
        return SendResult(ok=True, elapsed_ms=7)

    monkeypatch.setattr(dispatch_module, "send_huawei_message", fake)
    return calls, queue


@pytest.fixture()
def stub_webhook(monkeypatch):
    calls: list[dict] = []

    async def fake(client, *, kind, webhook_url, secret, event, title, body, text):
        calls.append({"kind": kind, "event": event})
        return SendResult(ok=True, elapsed_ms=3)

    monkeypatch.setattr(dispatch_module, "send_to_channel", fake)
    return calls


def _huawei_channel(db, **cfg_overrides) -> NotifyChannel:
    """建华为渠道 + 1:1 配置；默认一份完整可用的凭据。"""
    channel = NotifyChannel(
        owner_id=OWNER,
        name="华为推送",
        kind="huawei",
        # 与 service 同一占位约定：NOT NULL 列存空 bytes。
        webhook_url_enc=encrypt(""),
        secret_enc=None,
        events=[EVENT],
        enabled=True,
    )
    db.add(channel)
    db.flush()
    cfg_kwargs = dict(
        channel_id=channel.id,
        app_id=APP_ID,
        client_id_enc=encrypt(CLIENT_ID),
        client_secret_enc=encrypt(CLIENT_SECRET),
        target_type="topic",
        target=TOPIC,
    )
    cfg_kwargs.update(cfg_overrides)
    db.add(NotifyHuaweiConfig(**cfg_kwargs))
    db.commit()
    return channel


def _logs(db) -> list[NotifyLog]:
    return list(db.scalars(select(NotifyLog).order_by(NotifyLog.id)).all())


def _dispatch(**kwargs):
    options = dict(title="工作流失败：x #1", body="步骤超时")
    options.update(kwargs)
    asyncio.run(dispatch(OWNER, EVENT, **options))


# ---- 正常路径 ---------------------------------------------------------------


def test_dispatch_huawei_sends_decrypted_config_and_logs(db, stub_huawei):
    calls, _ = stub_huawei
    _huawei_channel(db)

    _dispatch()

    assert len(calls) == 1
    sent = calls[0]
    # 传给发送器的是 HuaweiAuth（client_credentials 模式）与主题目标。
    assert sent["app_id"] == APP_ID
    auth = sent["auth"]
    assert auth.mode == GRANT_CLIENT_CREDENTIALS
    assert auth.client_id == CLIENT_ID
    assert auth.client_secret == CLIENT_SECRET
    assert sent["target_type"] == "topic"
    assert sent["target"] == TOPIC
    # 事件名经透传 data 带给 App。
    assert sent["data"] == {"event": EVENT}
    assert sent["title"] == "工作流失败：x #1"
    assert sent["body"] == "步骤超时"

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].status == STATUS_SENT
    assert rows[0].channel_name == "华为推送"
    # body 快照必须落库——App 历史消息页靠它回放。
    assert rows[0].body == "步骤超时"


def test_dispatch_huawei_service_account_builds_jwt_auth(
    db, stub_huawei, service_account_text
):
    calls, _ = stub_huawei
    # JWT 模式：客户端凭证对留空 bytes 占位，凭据落在 service_account_enc。
    _huawei_channel(
        db,
        client_id_enc=encrypt(""),
        client_secret_enc=encrypt(""),
        service_account_enc=encrypt(service_account_text),
    )

    _dispatch()

    assert len(calls) == 1
    auth = calls[0]["auth"]
    assert auth.mode == GRANT_JWT_BEARER
    assert auth.service_account["sub_account"] == "sub-1"
    assert "private_key" in auth.service_account
    # 该模式下 HuaweiAuth 不应携带任何客户端密钥。
    assert auth.client_id == "" and auth.client_secret == ""


def test_dispatch_huawei_undecryptable_service_account_is_skipped(
    db, stub_huawei
):
    calls, _ = stub_huawei
    _huawei_channel(db, service_account_enc=b"not-a-valid-fernet-token")

    _dispatch()

    assert calls == []
    assert _logs(db) == []


def test_dispatch_huawei_token_target_passes_through(db, stub_huawei):
    calls, _ = stub_huawei
    _huawei_channel(db, target_type="token", target="device-push-token-1")

    _dispatch()

    assert calls[0]["target_type"] == "token"
    assert calls[0]["target"] == "device-push-token-1"


# ---- 跳过语义：坏渠道不拦路 ---------------------------------------------------


def test_dispatch_huawei_missing_config_row_is_skipped(db, stub_huawei):
    calls, _ = stub_huawei
    # 只有渠道行、没有 1:1 配置行。
    db.add(
        NotifyChannel(
            owner_id=OWNER,
            name="残缺华为",
            kind="huawei",
            webhook_url_enc=encrypt(""),
            events=[EVENT],
            enabled=True,
        )
    )
    db.commit()

    _dispatch()

    assert calls == []
    assert _logs(db) == []


def test_dispatch_huawei_undecryptable_config_is_skipped(db, stub_huawei):
    calls, _ = stub_huawei
    _huawei_channel(db, client_id_enc=b"not-a-valid-fernet-token")

    _dispatch()

    assert calls == []
    assert _logs(db) == []


def test_dispatch_huawei_incomplete_config_is_skipped(db, stub_huawei):
    calls, _ = stub_huawei
    _huawei_channel(db, target="")

    _dispatch()

    assert calls == []
    assert _logs(db) == []


def test_dispatch_huawei_skip_does_not_block_other_channels(db, stub_huawei, stub_webhook):
    hw_calls, _ = stub_huawei
    # 一个缺配置的华为渠道 + 一个正常飞书渠道。
    db.add(
        NotifyChannel(
            owner_id=OWNER,
            name="残缺华为",
            kind="huawei",
            webhook_url_enc=encrypt(""),
            events=[EVENT],
            enabled=True,
        )
    )
    db.add(
        NotifyChannel(
            owner_id=OWNER,
            name="值班群",
            kind="feishu",
            webhook_url_enc=encrypt("https://open.feishu.cn/hook/xxx"),
            secret_enc=None,
            events=[EVENT],
            enabled=True,
        )
    )
    db.commit()

    _dispatch()

    assert hw_calls == []
    assert len(stub_webhook) == 1
    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].channel_name == "值班群"


# ---- 失败与并存 -------------------------------------------------------------


def test_dispatch_huawei_failure_logs_and_never_raises(db, stub_huawei):
    _, queue = stub_huawei
    queue.append(SendResult(ok=False, error="华为返回 code=80000001 msg=bad", elapsed_ms=20))
    _huawei_channel(db)

    # 合同：发送失败也不允许冒出异常。
    _dispatch()

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].status == STATUS_FAILED
    assert "80000001" in (rows[0].error or "")
    assert rows[0].elapsed_ms == 20


def test_dispatch_mixed_webhook_and_huawei_channels(db, stub_huawei, stub_webhook):
    hw_calls, _ = stub_huawei
    _huawei_channel(db)
    db.add(
        NotifyChannel(
            owner_id=OWNER,
            name="值班群",
            kind="feishu",
            webhook_url_enc=encrypt("https://open.feishu.cn/hook/xxx"),
            secret_enc=None,
            events=[EVENT],
            enabled=True,
        )
    )
    db.commit()

    _dispatch()

    # 两条协议各自的发送器都被命中，且各写一行留痕。
    assert len(hw_calls) == 1
    assert len(stub_webhook) == 1
    rows = _logs(db)
    assert {(r.channel_name, r.status) for r in rows} == {
        ("华为推送", STATUS_SENT),
        ("值班群", STATUS_SENT),
    }


def test_dispatch_huawei_respects_event_subscription(db, stub_huawei):
    calls, _ = stub_huawei
    _huawei_channel(db)
    # 渠道只订阅 EVENT，派发一个没订阅的事件：直接短路，无发送无留痕。
    asyncio.run(dispatch(OWNER, "ops.expired", title="t", body=""))
    assert calls == []
    assert _logs(db) == []
