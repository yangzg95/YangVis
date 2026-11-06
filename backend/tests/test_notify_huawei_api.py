"""华为渠道的 service / HTTP 层：建改查、掩码语义、级联删除与连通测试分流。

与 test_notify_channel_api.py 同一套打法：独立 FastAPI app + 依赖覆盖，
发送器在 service 模块上打桩不出网。这层最要紧的钉子：

- client_id 只回掩码、client_secret 只回「有没有」，明文绝不进响应；
- 掩码 / 空串回传表示保持原凭据，只改名不能悄悄清掉密钥；
- 设备 token 按凭据掩码，topic 名可明文回；
- 删渠道连带删 1:1 配置；连通测试在 service 内部分流到华为发送器。
"""
from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.crypto import decrypt, encrypt, mask
from app.database import Base, get_db
from app.deps import require_user
from app.models.entities import NotifyChannel, NotifyHuaweiConfig
from app.models.schemas import CurrentUser
from app.routers import notify as notify_router
from app.services.notify import service as service_module
from app.services.notify.huawei import (
    GRANT_CLIENT_CREDENTIALS,
    GRANT_JWT_BEARER,
)
from app.services.notify.senders import SendResult

OWNER = 1
OTHER = 2

APP_ID = "119672293"
CLIENT_ID = "oauth-client-id"
CLIENT_SECRET = "oauth-client-secret"


@pytest.fixture()
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
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'notify-hw-api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", Session)

    def make_client(owner: int) -> TestClient:
        app = FastAPI()
        app.include_router(notify_router.router, prefix="/api")

        def override_get_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_user] = lambda: CurrentUser(
            user_id=owner, username=f"u{owner}"
        )
        return TestClient(app)

    return make_client(OWNER), Session, make_client


def _hw_payload(**overrides) -> dict:
    data = {
        "name": "华为推送",
        "app_id": APP_ID,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "target_type": "topic",
        "target": "yangvis",
        "events": ["workflow.failed"],
        "enabled": True,
    }
    data.update(overrides)
    return data


def _stub_huawei(monkeypatch, result: SendResult | None = None):
    calls: list[dict] = []

    async def fake(client, **kwargs):
        calls.append(kwargs)
        return result or SendResult(ok=True, elapsed_ms=9)

    monkeypatch.setattr(service_module, "send_huawei_message", fake)
    return calls


# -- 创建与掩码视图 ------------------------------------------------------------


def test_create_huawei_channel_masks_credentials(env):
    client, Session, _ = env
    resp = client.post("/api/notify/channels/huawei", json=_hw_payload())
    assert resp.status_code == 200
    item = resp.json()["data"]

    assert item["kind"] == "huawei"
    assert item["app_id"] == APP_ID
    # client_id 只回掩码；secret 只回有没有。
    assert item["client_id_masked"] == mask(CLIENT_ID)
    assert CLIENT_ID not in item["client_id_masked"]
    assert item["has_client_secret"] is True
    assert item["client_id_error"] is None
    assert item["client_secret_error"] is None
    # 默认走客户端凭证对，没有服务账号。
    assert item["has_service_account"] is False
    assert item["service_account_error"] is None
    # topic 名不敏感，明文回显。
    assert item["target_type"] == "topic"
    assert item["target"] == "yangvis"
    assert item["events"] == ["workflow.failed"]

    # 库里落的是密文：明文既不该出现在密文列，也能被解回来。
    with Session() as db:
        cfg = db.scalar(select(NotifyHuaweiConfig))
        assert decrypt(cfg.client_id_enc) == CLIENT_ID
        assert decrypt(cfg.client_secret_enc) == CLIENT_SECRET
        assert CLIENT_SECRET.encode() not in cfg.client_secret_enc
        # 占位的 webhook 列是空 bytes 而不是 NULL，且解出来是空串。
        channel = db.get(NotifyChannel, item["id"])
        assert channel.webhook_url_enc == b""
        assert channel.kind == "huawei"


def test_token_target_is_masked_in_response(env):
    client, _, _ = env
    resp = client.post(
        "/api/notify/channels/huawei",
        json=_hw_payload(target_type="token", target="device-push-token-1"),
    )
    item = resp.json()["data"]
    assert item["target_type"] == "token"
    assert item["target"] == mask("device-push-token-1")
    assert "device-push-token-1" not in item["target"]


def test_create_duplicate_name_rejected(env):
    client, _, _ = env
    assert client.post("/api/notify/channels/huawei", json=_hw_payload()).status_code == 200
    resp = client.post(
        "/api/notify/channels/huawei", json=_hw_payload(app_id="999999999")
    )
    assert resp.json()["code"] == -1
    assert "同名" in resp.json()["message"]


# -- 服务账号私钥（JWT bearer）---------------------------------------------------


def test_create_with_service_account(env, service_account_text):
    client, Session, _ = env
    payload = _hw_payload(
        client_id="",
        client_secret="",
        service_account=service_account_text,
    )
    resp = client.post("/api/notify/channels/huawei", json=payload)
    assert resp.status_code == 200
    item = resp.json()["data"]

    # 视图只回「有没有」，JSON 原文（含 PEM 私钥）绝不进响应。
    assert item["has_service_account"] is True
    assert item["service_account_error"] is None
    assert item["has_client_secret"] is False
    assert item["client_id_masked"] == ""
    assert "private_key" not in json.dumps(item, ensure_ascii=False)

    with Session() as db:
        cfg = db.scalar(select(NotifyHuaweiConfig))
        # 库里落的是 Fernet 密文，可解密还原成原 JSON；客户端凭证列是空 bytes 占位。
        assert cfg.service_account_enc
        assert decrypt(cfg.service_account_enc) == service_account_text
        assert service_account_text.encode() not in cfg.service_account_enc
        assert cfg.client_id_enc == b"" and cfg.client_secret_enc == b""


def test_create_without_any_credentials_rejected(env):
    client, _, _ = env
    resp = client.post(
        "/api/notify/channels/huawei",
        json=_hw_payload(client_id="", client_secret=""),
    )
    assert resp.json()["code"] == -1
    assert "至少配置一种" in resp.json()["message"]


def test_create_with_broken_service_account_rejected(env):
    client, Session, _ = env
    resp = client.post(
        "/api/notify/channels/huawei",
        json=_hw_payload(
            client_id="", client_secret="", service_account="{not-json"
        ),
    )
    assert resp.json()["code"] == -1
    assert "JSON" in resp.json()["message"]
    # 坏输入必须被挡在库外：一行配置都不该留下。
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(NotifyHuaweiConfig)) == 0


def test_update_replaces_service_account(env, service_account_text):
    client, Session, _ = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]
    assert created["has_service_account"] is False

    resp = client.put(
        f"/api/notify/channels/{created['id']}/huawei",
        json={"service_account": service_account_text},
    )
    assert resp.status_code == 200
    item = resp.json()["data"]
    assert item["has_service_account"] is True

    with Session() as db:
        cfg = db.scalar(
            select(NotifyHuaweiConfig).where(
                NotifyHuaweiConfig.channel_id == created["id"]
            )
        )
        # 替换后两种凭据并存于行上（发送时优先服务账号），原客户端凭证保持不变。
        assert decrypt(cfg.service_account_enc) == service_account_text
        assert decrypt(cfg.client_id_enc) == CLIENT_ID


def test_test_endpoint_uses_service_account_auth(
    env, monkeypatch, service_account_text
):
    client, _, _ = env
    created = client.post(
        "/api/notify/channels/huawei",
        json=_hw_payload(
            client_id="",
            client_secret="",
            service_account=service_account_text,
        ),
    ).json()["data"]
    calls = _stub_huawei(monkeypatch)

    resp = client.post(f"/api/notify/channels/{created['id']}/test")
    assert resp.json()["data"]["success"] is True
    assert len(calls) == 1
    auth = calls[0]["auth"]
    assert auth.mode == GRANT_JWT_BEARER
    assert auth.service_account["sub_account"] == "sub-1"


# -- 详情端点 -----------------------------------------------------------------


def test_get_huawei_detail(env):
    client, _, _ = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]

    resp = client.get(f"/api/notify/channels/{created['id']}/huawei")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == created["id"]


def test_get_huawei_detail_on_webhook_channel_is_business_error(env):
    client, _, _ = env
    webhook = client.post(
        "/api/notify/channels",
        json={
            "name": "飞书群",
            "kind": "feishu",
            "webhook_url": "https://open.feishu.cn/hook/xxx",
            "events": ["workflow.failed"],
        },
    ).json()["data"]

    resp = client.get(f"/api/notify/channels/{webhook['id']}/huawei")
    assert resp.json()["code"] == -1


def test_get_huawei_detail_unknown_id_is_404(env):
    client, _, _ = env
    assert client.get("/api/notify/channels/999/huawei").status_code == 404


# -- 更新：掩码保持与凭据重置 ---------------------------------------------------


def test_update_with_masked_inputs_keeps_credentials(env):
    client, Session, _ = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]

    resp = client.put(
        f"/api/notify/channels/{created['id']}/huawei",
        json={
            "name": "改名后的华为",
            # 列表回显的掩码原样提交：client_id / secret / 目标都必须保持原样。
            "client_id": created["client_id_masked"],
            "client_secret": created["client_id_masked"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "改名后的华为"

    with Session() as db:
        cfg = db.scalar(
            select(NotifyHuaweiConfig).where(
                NotifyHuaweiConfig.channel_id == created["id"]
            )
        )
        assert decrypt(cfg.client_id_enc) == CLIENT_ID
        assert decrypt(cfg.client_secret_enc) == CLIENT_SECRET
        assert cfg.target == "yangvis"


def test_update_real_credentials_replaces_and_resets_test(env):
    client, Session, _ = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]

    # 先造出「已测试通过」的旧结论。
    with Session() as db:
        channel = db.get(NotifyChannel, created["id"])
        channel.last_test_ok = True
        db.commit()

    new_secret = "rotated-client-secret-9"
    resp = client.put(
        f"/api/notify/channels/{created['id']}/huawei",
        json={"client_secret": new_secret},
    )
    assert resp.status_code == 200
    with Session() as db:
        cfg = db.scalar(
            select(NotifyHuaweiConfig).where(
                NotifyHuaweiConfig.channel_id == created["id"]
            )
        )
        assert decrypt(cfg.client_secret_enc) == new_secret
        channel = db.get(NotifyChannel, created["id"])
        # 凭据轮换后，旧连通结论作废。
        assert channel.last_test_ok is None


def test_update_huawei_on_wrong_kind_rejected(env):
    client, _, _ = env
    webhook = client.post(
        "/api/notify/channels",
        json={
            "name": "飞书群",
            "kind": "feishu",
            "webhook_url": "https://open.feishu.cn/hook/xxx",
            "events": ["workflow.failed"],
        },
    ).json()["data"]

    resp = client.put(
        f"/api/notify/channels/{webhook['id']}/huawei", json={"name": "x"}
    )
    assert resp.json()["code"] == -1


def test_update_unknown_channel_is_404(env):
    client, _, _ = env
    resp = client.put("/api/notify/channels/999/huawei", json={"name": "x"})
    assert resp.status_code == 404


# -- 删除与 owner 隔离 ----------------------------------------------------------


def test_delete_channel_cascades_huawei_config(env):
    client, Session, _ = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]

    assert client.delete(f"/api/notify/channels/{created['id']}").status_code == 200
    with Session() as db:
        # 1:1 配置必须随渠道一起清掉，不留孤儿行。
        count = db.scalar(select(func.count()).select_from(NotifyHuaweiConfig))
        assert count == 0
        assert db.get(NotifyChannel, created["id"]) is None


def test_owner_isolation_huawei(env):
    client, _, make_client = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]

    other = make_client(OTHER)
    assert other.get(f"/api/notify/channels/{created['id']}/huawei").status_code == 404
    assert other.put(
        f"/api/notify/channels/{created['id']}/huawei", json={"name": "x"}
    ).status_code == 404
    assert other.delete(f"/api/notify/channels/{created['id']}").status_code == 404


# -- 连通测试分流 ---------------------------------------------------------------


def test_test_endpoint_routes_to_huawei_sender(env, monkeypatch):
    client, Session, _ = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]
    calls = _stub_huawei(monkeypatch)

    resp = client.post(f"/api/notify/channels/{created['id']}/test")
    assert resp.json()["data"]["success"] is True

    # 发的是 HuaweiAuth（client_credentials 模式），事件用保留字 test。
    assert len(calls) == 1
    assert calls[0]["app_id"] == APP_ID
    auth = calls[0]["auth"]
    assert auth.mode == GRANT_CLIENT_CREDENTIALS
    assert auth.client_id == CLIENT_ID
    assert auth.client_secret == CLIENT_SECRET
    assert calls[0]["target"] == "yangvis"
    assert calls[0]["data"] == {"event": "test"}

    with Session() as db:
        channel = db.get(NotifyChannel, created["id"])
        assert channel.last_test_ok is True
        assert channel.last_test_at is not None


def test_test_endpoint_huawei_failure_records_reason(env, monkeypatch):
    client, Session, _ = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]
    _stub_huawei(monkeypatch, SendResult(ok=False, error="HTTP 401", elapsed_ms=11))

    resp = client.post(f"/api/notify/channels/{created['id']}/test")
    body = resp.json()["data"]
    assert body["success"] is False
    assert body["message"] == "HTTP 401"

    with Session() as db:
        channel = db.get(NotifyChannel, created["id"])
        assert channel.last_test_ok is False
        assert channel.last_test_error == "HTTP 401"


def test_test_endpoint_missing_config_returns_failure_not_500(env, monkeypatch):
    client, Session, _ = env
    created = client.post("/api/notify/channels/huawei", json=_hw_payload()).json()["data"]
    _stub_huawei(monkeypatch)

    # 直接抹掉 1:1 配置行：测试按钮应得到一个明确的失败结论，而不是 500。
    with Session() as db:
        cfg = db.scalar(select(NotifyHuaweiConfig))
        db.delete(cfg)
        db.commit()

    resp = client.post(f"/api/notify/channels/{created['id']}/test")
    assert resp.status_code == 200
    assert resp.json()["data"]["success"] is False
