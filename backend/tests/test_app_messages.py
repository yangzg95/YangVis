"""移动 App 收件箱：静态令牌、增量读取与 SSE 补拉/实时衔接。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base
from app.models.entities import AppMessage
from app.models.schemas import AppMessageItem
from app.routers import app_messages
from app.services.notify.app_inbox import prune_app_messages
from app.services.notify.app_stream import app_stream

OWNER = 1
OTHER = 2
APP_TOKEN = "app-static-token-123"
AUTH = {"Authorization": f"Bearer {APP_TOKEN}"}


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
    monkeypatch.setattr(settings, "NOTIFY_APP_OWNER_ID", OWNER)

    app = FastAPI()
    app.include_router(app_messages.router, prefix="/api")
    try:
        yield TestClient(app), Session, settings
    finally:
        app_stream.close_all()
        engine.dispose()


def _message(
    db,
    *,
    owner_id: int = OWNER,
    title: str,
    body: str | None = None,
    event: str = "workflow.failed",
    created_at: datetime | None = None,
) -> AppMessage:
    row = AppMessage(
        owner_id=owner_id,
        event=event,
        title=title,
        body=body,
        created_at=created_at or datetime(2026, 11, 6, 10, 0, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _publish(row: AppMessage) -> None:
    app_stream.publish(row.owner_id, AppMessageItem.model_validate(row))


# -- 鉴权与隐藏 ---------------------------------------------------------------


def test_endpoint_404_when_token_or_owner_unset(env, monkeypatch):
    client, _, settings = env
    monkeypatch.setattr(settings, "NOTIFY_APP_TOKEN", "")
    assert client.get("/api/app/v1/messages", headers=AUTH).status_code == 404

    monkeypatch.setattr(settings, "NOTIFY_APP_TOKEN", APP_TOKEN)
    monkeypatch.setattr(settings, "NOTIFY_APP_OWNER_ID", None)
    assert client.get("/api/app/v1/messages", headers=AUTH).status_code == 404
    assert client.get("/api/app/v1/messages/stream", headers=AUTH).status_code == 404


def test_missing_and_wrong_token_are_401(env):
    client, _, _ = env
    assert client.get("/api/app/v1/messages").status_code == 401
    assert client.get(
        "/api/app/v1/messages", headers={"Authorization": "Bearer wrong-token"}
    ).status_code == 401
    assert client.get(
        "/api/app/v1/messages", headers={"Authorization": "wrong-token"}
    ).status_code == 401


def test_raw_token_without_bearer_prefix_accepted(env):
    client, Session, _ = env
    with Session() as db:
        _message(db, title="一条")
    resp = client.get(
        "/api/app/v1/messages", headers={"Authorization": APP_TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 1


# -- 最新记录与增量补拉 --------------------------------------------------------


def test_latest_messages_are_owner_scoped_and_descending(env):
    client, Session, _ = env
    with Session() as db:
        _message(db, title="较早一条", body="正文甲")
        _message(db, owner_id=OTHER, title="别人的")
        _message(db, title="较新一条", body="正文乙")

    resp = client.get("/api/app/v1/messages", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    assert [item["title"] for item in data["items"]] == ["较新一条", "较早一条"]
    first = data["items"][0]
    assert first["body"] == "正文乙"
    assert first["event"] == "workflow.failed"
    assert "channel_name" not in first
    assert "status" not in first and "error" not in first


def test_after_id_is_strict_ascending_and_owner_scoped(env):
    client, Session, _ = env
    with Session() as db:
        first = _message(db, title="第一条")
        second = _message(db, title="第二条")
        _message(db, owner_id=OTHER, title="别人的")
        third = _message(db, title="第三条")
        first_id, second_id, third_id = first.id, second.id, third.id

    resp = client.get(
        f"/api/app/v1/messages?after_id={first_id}&limit=2", headers=AUTH
    )
    items = resp.json()["data"]["items"]
    assert [item["id"] for item in items] == [second_id, third_id]
    assert [item["title"] for item in items] == ["第二条", "第三条"]

    resp = client.get(f"/api/app/v1/messages?after_id={third_id}", headers=AUTH)
    assert resp.json()["data"]["items"] == []


def test_limit_and_after_id_are_validated(env):
    client, _, _ = env
    assert client.get("/api/app/v1/messages?limit=999", headers=AUTH).status_code == 422
    assert client.get("/api/app/v1/messages?after_id=-1", headers=AUTH).status_code == 422


# -- SSE：注册后补拉，ready 后只发实时新消息 -----------------------------------


def test_stream_bridges_backlog_and_live_messages_without_gap(env):
    _, Session, _ = env

    async def run():
        with Session() as db:
            first = _message(db, title="历史消息")

        response = await app_messages.stream_messages(after_id=0, owner_id=OWNER)
        # 路由已经注册连接、生成器还没开始补拉：这条消息同时进 backlog 和实时队列。
        with Session() as db:
            second = _message(db, title="窗口期消息")
            _publish(second)

        stream = response.body_iterator
        first_frame = await asyncio.wait_for(anext(stream), timeout=1)
        second_frame = await asyncio.wait_for(anext(stream), timeout=1)
        ready_frame = await asyncio.wait_for(anext(stream), timeout=1)

        assert first_frame.startswith(f"id: {first.id}\nevent: message\n")
        assert second_frame.startswith(f"id: {second.id}\nevent: message\n")
        assert ready_frame == f'event: ready\ndata: {{"after_id": {second.id}}}\n\n'
        assert app_stream.connection_count(OWNER) == 1

        # 队列里的第二条是 backlog 的重复；越过它后应直接收到实时第三条。
        with Session() as db:
            third = _message(db, title="实时消息")
            _publish(third)
        third_frame = await asyncio.wait_for(anext(stream), timeout=1)
        assert third_frame.startswith(f"id: {third.id}\nevent: message\n")
        assert "窗口期消息" not in third_frame

        app_stream.close_all()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()
        assert app_stream.connection_count(OWNER) == 0

    asyncio.run(run())


def test_slow_connection_is_closed_for_reconnect_recovery(env):
    _, Session, _ = env
    with Session() as db:
        row = _message(db, title="消息")
        item = AppMessageItem.model_validate(row)

    connection = app_stream.register(OWNER)
    for _ in range(128):
        connection.publish(item)
    assert connection.queue.full()

    connection.publish(item)
    assert connection.closed is True
    assert app_stream.connection_count(OWNER) == 0

    async def drain():
        for _ in range(127):
            assert await connection.next_message() == item
        # 队列满时丢掉最早一帧，关闭哨兵迫使客户端断线后按游标补拉。
        assert await connection.next_message() is None

    asyncio.run(drain())


def test_prune_app_messages_keeps_thirty_day_window(env):
    _, Session, _ = env
    with Session() as db:
        _message(
            db,
            title="过期消息",
            created_at=datetime.now() - timedelta(days=31),
        )
        _message(
            db,
            title="保留消息",
            created_at=datetime.now() - timedelta(days=1),
        )

    assert prune_app_messages(days=30) == 1
    with Session() as db:
        assert [row.title for row in db.scalars(select(AppMessage))] == ["保留消息"]


def test_bootstrap_prune_step_targets_app_inbox(env):
    from app.bootstrap import prune_app_messages as bootstrap_step

    _, Session, _ = env
    with Session() as db:
        _message(
            db,
            title="过期消息",
            created_at=datetime.now() - timedelta(days=31),
        )
    bootstrap_step()
    with Session() as db:
        assert db.scalar(select(AppMessage)) is None


def test_jwt_history_uses_same_independent_inbox(env, monkeypatch):
    """控制台/App 内历史接口按 JWT 用户读收件箱，不读 webhook 审计日志。"""
    from app.deps import require_user
    from app.models.schemas import CurrentUser
    from app.routers import notify

    _, Session, _ = env
    with Session() as db:
        _message(db, title="我的消息")
        _message(db, owner_id=OTHER, title="别人的消息")
        assert len(list(db.scalars(select(AppMessage)).all())) == 2

    app = FastAPI()
    app.include_router(notify.router, prefix="/api")
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=OWNER, username="owner", is_admin=True
    )
    client = TestClient(app)
    resp = client.get("/api/notify/messages")
    assert resp.status_code == 200
    assert [item["title"] for item in resp.json()["data"]["items"]] == ["我的消息"]
