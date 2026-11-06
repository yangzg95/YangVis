"""华为渠道的 service / HTTP 层：建改查、掩码语义、级联删除与连通测试分流。

与 test_notify_channel_api.py 同一套打法：独立 FastAPI app + 依赖覆盖，
发送器在 service 模块上打桩不出网。这层最要紧的钉子：

- client_id 只回掩码、client_secret 只回「有没有」，明文绝不进响应；
- 掩码 / 空串回传表示保持原凭据，只改名不能悄悄清掉密钥；
- 设备 token 按凭据掩码，topic 名可明文回；
- 删渠道连带删 1:1 配置；连通测试在 service 内部分流到华为发送器。
"""
from __future__ import annotations

import pytest
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
from app.services.notify.senders import SendResult

OWNER = 1
OTHER = 2

APP_ID = "119672293"
CLIENT_ID = "oauth-client-id"
CLIENT_SECRET = "oauth-client-secret"


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

    # 发的是解密后的明文 OAuth 凭据，事件用保留字 test。
    assert len(calls) == 1
    assert calls[0]["app_id"] == APP_ID
    assert calls[0]["client_id"] == CLIENT_ID
    assert calls[0]["client_secret"] == CLIENT_SECRET
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
