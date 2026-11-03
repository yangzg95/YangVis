"""通知渠道 API：CRUD、owner 隔离、掩码保持与连通测试端点。

掩码语义是这层最要紧的钉子：列表里 URL 永远是掩码、更新回传掩码不清凭据、
换凭据后测试结论作废。走独立的 FastAPI app + 依赖覆盖，与既有 API 测试同一
套打法；发送器在 service 模块上打桩，不出网。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crypto import encrypt
from app.database import Base, get_db
from app.deps import require_user
from app.models.entities import NotifyChannel, NotifyLog
from app.models.schemas import CurrentUser
from app.routers import notify as notify_router
from app.services.notify import service as service_module
from app.services.notify.senders import SendResult

OWNER = 1
OTHER = 2

CHANNEL_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'notify-api.db'}", connect_args={"check_same_thread": False}
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


def _payload(**overrides) -> dict:
    data = {
        "name": "值班群机器人",
        "kind": "feishu",
        "webhook_url": CHANNEL_URL,
        "secret": "sec-123",
        "events": ["workflow.failed", "ops.pending"],
    }
    data.update(overrides)
    return data


def _stub_sender(monkeypatch, result: SendResult):
    """把 service 模块里的 send_to_channel 换成不出网的假实现。"""
    calls: list[dict] = []

    async def fake(client, *, kind, webhook_url, secret, event, title, body, text):
        calls.append(
            {"kind": kind, "webhook_url": webhook_url, "secret": secret, "event": event}
        )
        return result

    monkeypatch.setattr(service_module, "send_to_channel", fake)
    return calls


# -- 事件词表 ---------------------------------------------------------------


def test_events_endpoint_returns_catalog(env):
    client, _, _ = env
    resp = client.get("/api/notify/events")
    assert resp.status_code == 200
    data = resp.json()["data"]
    keys = {item["key"] for item in data["items"]}
    assert keys == {"workflow.failed", "workflow.succeeded", "ops.pending", "ops.expired"}
    failed = next(item for item in data["items"] if item["key"] == "workflow.failed")
    assert failed["default_on"] is True
    succeeded = next(item for item in data["items"] if item["key"] == "workflow.succeeded")
    assert succeeded["default_on"] is False


# -- CRUD -------------------------------------------------------------------


def test_create_and_list_channel_masks_url(env):
    client, Session, _ = env
    resp = client.post("/api/notify/channels", json=_payload())
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["name"] == "值班群机器人"
    assert body["kind"] == "feishu"
    # URL 只以掩码出现，明文绝不回客户端。
    assert body["webhook_url"].startswith("sk-****")
    assert CHANNEL_URL not in body["webhook_url"]
    assert body["has_secret"] is True
    assert body["events"] == ["workflow.failed", "ops.pending"]

    listed = client.get("/api/notify/channels").json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["webhook_url"].startswith("sk-****")


def test_create_normalizes_events_and_rejects_bad_url(env):
    client, _, _ = env
    # 未知事件 key 被丢弃、去重。
    resp = client.post(
        "/api/notify/channels",
        json=_payload(events=["workflow.failed", "workflow.failed", "nope"]),
    )
    assert resp.json()["data"]["events"] == ["workflow.failed"]

    # 非 http(s) 地址直接拒。
    for bad in ("ftp://x/hook", "file:///etc/passwd", "not-a-url"):
        resp = client.post("/api/notify/channels", json=_payload(webhook_url=bad))
        assert resp.json()["code"] == -1
        assert "http(s)" in resp.json()["message"]


def test_create_duplicate_name_rejected(env):
    client, _, _ = env
    assert client.post("/api/notify/channels", json=_payload()).status_code == 200
    resp = client.post("/api/notify/channels", json=_payload(webhook_url=CHANNEL_URL + "x"))
    assert resp.json()["code"] == -1
    assert "同名" in resp.json()["message"]


def test_update_with_masked_url_keeps_credentials(env):
    client, Session, _ = env
    created = client.post("/api/notify/channels", json=_payload()).json()["data"]

    # 只改名，但把列表里回显的掩码原样提交——凭据必须保持原样。
    resp = client.put(
        f"/api/notify/channels/{created['id']}",
        json={
            "name": "改名后的机器人",
            "webhook_url": created["webhook_url"],
            "secret": created["webhook_url"],  # 掩码同样适用
        },
    )
    assert resp.status_code == 200
    with Session() as db:
        channel = db.get(NotifyChannel, created["id"])
        assert channel.name == "改名后的机器人"
        assert decrypt_matches(channel.webhook_url_enc, CHANNEL_URL)
        assert decrypt_matches(channel.secret_enc, "sec-123")


def decrypt_matches(ciphertext: bytes, expected: str) -> bool:
    from app.crypto import decrypt

    return decrypt(ciphertext) == expected


def test_update_with_real_url_replaces_and_resets_test(env):
    client, Session, _ = env
    created = client.post("/api/notify/channels", json=_payload()).json()["data"]

    # 先造一份「已测试通过」的旧结论。
    with Session() as db:
        channel = db.get(NotifyChannel, created["id"])
        channel.last_test_ok = True
        db.commit()

    new_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=zzz"
    resp = client.put(
        f"/api/notify/channels/{created['id']}",
        json={"webhook_url": new_url},
    )
    assert resp.status_code == 200
    with Session() as db:
        channel = db.get(NotifyChannel, created["id"])
        assert decrypt_matches(channel.webhook_url_enc, new_url)
        # 换了地址，旧测试结论作废。
        assert channel.last_test_ok is None


def test_update_unknown_events_dropped(env):
    client, _, _ = env
    created = client.post("/api/notify/channels", json=_payload()).json()["data"]
    resp = client.put(
        f"/api/notify/channels/{created['id']}",
        json={"events": ["ops.expired", "ghost-event"]},
    )
    assert resp.json()["data"]["events"] == ["ops.expired"]


def test_owner_isolation(env):
    client, _, make_client = env
    created = client.post("/api/notify/channels", json=_payload()).json()["data"]

    other = make_client(OTHER)
    assert other.get("/api/notify/channels").json()["data"]["total"] == 0
    # 别人的渠道 id：改与删都读作 404，不泄露存在性。
    assert other.put(f"/api/notify/channels/{created['id']}", json={"name": "x"}).status_code == 404
    assert other.delete(f"/api/notify/channels/{created['id']}").status_code == 404


def test_delete_channel(env):
    client, _, _ = env
    created = client.post("/api/notify/channels", json=_payload()).json()["data"]
    assert client.delete(f"/api/notify/channels/{created['id']}").status_code == 200
    assert client.get("/api/notify/channels").json()["data"]["total"] == 0
    assert client.delete(f"/api/notify/channels/{created['id']}").status_code == 404


# -- 连通测试 ---------------------------------------------------------------


def test_test_channel_success_records_result(env, monkeypatch):
    client, Session, _ = env
    created = client.post("/api/notify/channels", json=_payload()).json()["data"]
    calls = _stub_sender(monkeypatch, SendResult(ok=True, elapsed_ms=12))

    resp = client.post(f"/api/notify/channels/{created['id']}/test")
    assert resp.status_code == 200
    assert resp.json()["data"]["success"] is True

    # 发的是解密后的明文地址与密钥，事件用保留字 test。
    assert calls[0]["webhook_url"] == CHANNEL_URL
    assert calls[0]["secret"] == "sec-123"
    assert calls[0]["event"] == "test"

    with Session() as db:
        channel = db.get(NotifyChannel, created["id"])
        assert channel.last_test_ok is True
        assert channel.last_test_at is not None


def test_test_channel_failure_returns_reason(env, monkeypatch):
    client, Session, _ = env
    created = client.post("/api/notify/channels", json=_payload()).json()["data"]
    _stub_sender(monkeypatch, SendResult(ok=False, error="HTTP 403", elapsed_ms=30))

    resp = client.post(f"/api/notify/channels/{created['id']}/test")
    assert resp.json()["data"]["success"] is False
    assert resp.json()["data"]["message"] == "HTTP 403"

    with Session() as db:
        channel = db.get(NotifyChannel, created["id"])
        assert channel.last_test_ok is False
        assert channel.last_test_error == "HTTP 403"


def test_test_channel_missing_is_404(env):
    client, _, _ = env
    assert client.post("/api/notify/channels/999/test").status_code == 404


# -- 发送记录 ---------------------------------------------------------------


def test_logs_endpoint_scopes_by_owner(env):
    client, Session, make_client = env
    with Session() as db:
        db.add(
            NotifyLog(
                owner_id=OWNER,
                channel_id=1,
                channel_name="我的渠道",
                event="workflow.failed",
                title="t",
                status="sent",
                created_at=datetime(2026, 11, 3, 10, 0, 0),
            )
        )
        db.add(
            NotifyLog(
                owner_id=OTHER,
                channel_id=2,
                channel_name="别人的渠道",
                event="workflow.failed",
                title="t",
                status="sent",
                created_at=datetime(2026, 11, 3, 10, 0, 0),
            )
        )
        db.commit()

    data = client.get("/api/notify/logs").json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["channel_name"] == "我的渠道"

    # 隔离是双向的：另一个用户看到的是他自己的那行，而不是 0 行。
    other_data = make_client(OTHER).get("/api/notify/logs").json()["data"]
    assert other_data["total"] == 1
    assert other_data["items"][0]["channel_name"] == "别人的渠道"


def test_logs_limit_is_clamped(env):
    client, Session, _ = env
    with Session() as db:
        for i in range(3):
            db.add(
                NotifyLog(
                    owner_id=OWNER,
                    channel_id=1,
                    channel_name="c",
                    event="workflow.failed",
                    title=f"t{i}",
                    status="sent",
                    created_at=datetime(2026, 11, 3, 10, 0, i),
                )
            )
        db.commit()

    # 倒序：最新在前。
    data = client.get("/api/notify/logs?limit=2").json()["data"]
    assert data["total"] == 2
    assert data["items"][0]["title"] == "t2"
    # 越界 limit 直接被 FastAPI 的 Query 校验挡掉。
    assert client.get("/api/notify/logs?limit=999").status_code == 422
