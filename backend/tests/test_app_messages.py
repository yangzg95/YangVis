"""移动 App 历史消息端点：静态令牌鉴权、404 隐藏、只回成功送达与归属过滤。

独立 FastAPI app + get_db 覆盖，不挂 require_user（这套端点本来就不走登录
JWT）。令牌与归属范围都在 settings 单例上用 monkeypatch 切换，用例结束自动还原。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.models.entities import NotifyLog
from app.routers import app_messages
from app.services.notify.dispatch import STATUS_FAILED, STATUS_SENT

OWNER = 1
OTHER = 2
APP_TOKEN = "app-static-token-123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'app-messages.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", Session)

    settings = get_settings()
    monkeypatch.setattr(settings, "NOTIFY_APP_TOKEN", APP_TOKEN)
    monkeypatch.setattr(settings, "NOTIFY_APP_OWNER_ID", None)

    app = FastAPI()
    app.include_router(app_messages.router, prefix="/api")

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), Session, settings


def _log(db, *, owner_id: int, title: str, status: str, body: str | None = None) -> None:
    db.add(
        NotifyLog(
            owner_id=owner_id,
            channel_id=10,
            channel_name="华为推送",
            event="workflow.failed",
            title=title,
            body=body,
            status=status,
            created_at=datetime(2026, 11, 6, 10, 0, 0),
        )
    )
    db.commit()


# -- 鉴权与隐藏 ---------------------------------------------------------------


def test_endpoint_404_when_token_unset(env, monkeypatch):
    client, _, settings = env
    monkeypatch.setattr(settings, "NOTIFY_APP_TOKEN", "")
    # 未配置令牌 = 功能对外不可见，不回 401 以免暴露端点存在。
    resp = client.get("/api/app/v1/messages", headers={"Authorization": f"Bearer {APP_TOKEN}"})
    assert resp.status_code == 404


def test_missing_and_wrong_token_are_401(env):
    client, _, _ = env
    assert client.get("/api/app/v1/messages").status_code == 401
    assert client.get(
        "/api/app/v1/messages", headers={"Authorization": "Bearer wrong-token"}
    ).status_code == 401
    # 裸 token 形态（extract_bearer_token 两种都收），错的照样拒。
    assert client.get(
        "/api/app/v1/messages", headers={"Authorization": "wrong-token"}
    ).status_code == 401


def test_correct_token_returns_200(env):
    client, _, _ = env
    resp = client.get(
        "/api/app/v1/messages", headers={"Authorization": f"Bearer {APP_TOKEN}"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []


# -- 数据口径 -----------------------------------------------------------------


def test_lists_only_sent_rows_desc_with_body(env):
    client, Session, _ = env
    with Session() as db:
        _log(db, owner_id=OWNER, title="较早一条", status=STATUS_SENT, body="正文甲")
        _log(db, owner_id=OWNER, title="发送失败", status=STATUS_FAILED, body="不该出现")
        _log(db, owner_id=OWNER, title="较新一条", status=STATUS_SENT, body="正文乙")

    resp = client.get(
        "/api/app/v1/messages", headers={"Authorization": f"Bearer {APP_TOKEN}"}
    )
    data = resp.json()["data"]
    # 失败行不进历史；倒序，新行在前。
    assert data["total"] == 2
    titles = [item["title"] for item in data["items"]]
    assert titles == ["较新一条", "较早一条"]
    first = data["items"][0]
    assert first["body"] == "正文乙"
    assert first["channel_name"] == "华为推送"
    assert first["event"] == "workflow.failed"
    # 响应信封里不该带 status / error 这些服务端排障字段。
    assert "status" not in first and "error" not in first


def test_raw_token_without_bearer_prefix_accepted(env):
    client, Session, _ = env
    with Session() as db:
        _log(db, owner_id=OWNER, title="一条", status=STATUS_SENT)
    resp = client.get(
        "/api/app/v1/messages", headers={"Authorization": APP_TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 1


def test_owner_filter_excludes_other_owners(env, monkeypatch):
    client, Session, settings = env
    with Session() as db:
        _log(db, owner_id=OWNER, title="我的", status=STATUS_SENT)
        _log(db, owner_id=OTHER, title="别人的", status=STATUS_SENT)

    # 不限制归属（单用户自用部署的默认）：两行都可见。
    resp = client.get(
        "/api/app/v1/messages", headers={"Authorization": f"Bearer {APP_TOKEN}"}
    )
    assert resp.json()["data"]["total"] == 2

    # 限定归属后，别人的行被过滤。
    monkeypatch.setattr(settings, "NOTIFY_APP_OWNER_ID", OWNER)
    resp = client.get(
        "/api/app/v1/messages", headers={"Authorization": f"Bearer {APP_TOKEN}"}
    )
    titles = [item["title"] for item in resp.json()["data"]["items"]]
    assert titles == ["我的"]


def test_limit_is_clamped(env):
    client, _, _ = env
    resp = client.get(
        "/api/app/v1/messages?limit=999",
        headers={"Authorization": f"Bearer {APP_TOKEN}"},
    )
    assert resp.status_code == 422
