"""通知分发：事件过滤、容错与 ``notify_log`` 落库。

dispatch 是挂在主流程关键节点上的旁路，它的全部价值就在于「绝不拖垮主流程」：
发送失败、密钥解不开、一个渠道都没有，都必须安静地消化掉。这些分支在页面
上点不出来，只能在这里钉。

发送器（``send_to_channel``）在 test_notify_senders.py 里单独用 MockTransport
验过，这里直接打桩替换，只测 dispatch 自己的逻辑：订阅过滤、owner 隔离、
失败落日志。SQLite 文件库 + monkeypatch ``SessionLocal``，dispatch 内部自开的
session 与测试造数走同一个临时库。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.crypto import encrypt
from app.database import Base
from app.models.entities import NotifyChannel, NotifyLog
from app.services.notify.dispatch import (
    STATUS_FAILED,
    STATUS_SENT,
    dispatch,
    prune_notify_logs,
    render_text,
)
# 模块对象本身：monkeypatch send_to_channel 需要（包 __init__ 不再 re-export
# dispatch 函数，from-import 现在拿到的就是子模块）。
from app.services.notify import dispatch as dispatch_module
from app.services.notify.events import normalize_events
from app.services.notify.senders import SendResult
from app.timeutil import BEIJING_TZ

OWNER = 1
OTHER = 2

EVENT = "workflow.failed"


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'notify.db'}", connect_args={"check_same_thread": False}
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
    # dispatch 内部经 app_db.SessionLocal() 自开短命 session；不替换会写真实库。
    monkeypatch.setattr("app.database.SessionLocal", maker)
    try:
        yield session
    finally:
        session.close()


def _channel(db, owner_id: int = OWNER, **overrides) -> NotifyChannel:
    kwargs = dict(
        owner_id=owner_id,
        name="群机器人",
        kind="feishu",
        webhook_url_enc=encrypt("https://open.feishu.cn/hook/xxx"),
        secret_enc=None,
        events=[EVENT],
        enabled=True,
    )
    kwargs.update(overrides)
    channel = NotifyChannel(**kwargs)
    db.add(channel)
    db.commit()
    return channel


class _Stub:
    """send_to_channel 的替身：记录调用，结果可逐测试配置。

    ``queue`` 非空时按序弹出（每次调用一个结果），空了用 ``default_result``。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queue: list[SendResult] = []
        self.default_result = SendResult(ok=True, elapsed_ms=5)


@pytest.fixture()
def stub_send(monkeypatch) -> _Stub:
    stub = _Stub()

    async def fake(client, *, kind, webhook_url, secret, event, title, body, text):
        stub.calls.append(
            {
                "kind": kind,
                "webhook_url": webhook_url,
                "secret": secret,
                "event": event,
                "title": title,
                "body": body,
                "text": text,
            }
        )
        if stub.queue:
            return stub.queue.pop(0)
        return stub.default_result

    monkeypatch.setattr(dispatch_module, "send_to_channel", fake)
    return stub


def _logs(db) -> list[NotifyLog]:
    return list(db.scalars(select(NotifyLog).order_by(NotifyLog.id)).all())


# ---- 基本分发 ---------------------------------------------------------------


def test_dispatch_sends_to_subscribed_channel(db, stub_send):
    _channel(db)
    asyncio.run(dispatch(OWNER, EVENT, title="工作流失败：x #1", body="步骤超时"))

    assert len(stub_send.calls) == 1
    sent = stub_send.calls[0]
    assert sent["event"] == EVENT
    assert sent["webhook_url"] == "https://open.feishu.cn/hook/xxx"
    # 群消息的文本形态：标题带前缀、正文、时间行。
    assert sent["text"].startswith("【yangvis】工作流失败：x #1\n步骤超时\n时间：")

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].status == STATUS_SENT
    assert rows[0].event == EVENT
    assert rows[0].channel_name == "群机器人"


def test_dispatch_skips_unsubscribed_disabled_and_foreign(db, stub_send):
    # 未订阅该事件 / 停用 / 别人的渠道：三种都不该收到，也都不该落日志。
    _channel(db, name="未订阅", events=["ops.pending"])
    _channel(db, name="停用", enabled=False)
    _channel(db, owner_id=OTHER, name="别人的")

    asyncio.run(dispatch(OWNER, EVENT, title="t", body=""))

    assert stub_send.calls == []
    assert _logs(db) == []


def test_dispatch_without_channels_is_silent(db, stub_send):
    # 一个渠道都没有：安静返回，不报错、不落任何行。
    asyncio.run(dispatch(OWNER, EVENT, title="t", body=""))
    assert stub_send.calls == []
    assert _logs(db) == []


def test_dispatch_failure_logs_and_never_raises(db, stub_send):
    _channel(db, name="坏渠道")
    stub_send.default_result = SendResult(ok=False, error="HTTP 500", elapsed_ms=42)

    # 不抛异常是本函数的合同：调用方把它 fire_and_forget 挂在主流程上。
    asyncio.run(dispatch(OWNER, EVENT, title="t", body=""))

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].status == STATUS_FAILED
    assert rows[0].error == "HTTP 500"
    assert rows[0].elapsed_ms == 42


def test_dispatch_continues_after_one_channel_fails(db, stub_send):
    _channel(db, name="第一个")
    _channel(db, name="第二个")
    stub_send.queue = [
        SendResult(ok=False, error="HTTP 500", elapsed_ms=10),
        SendResult(ok=True, elapsed_ms=1),
    ]

    asyncio.run(dispatch(OWNER, EVENT, title="t", body=""))

    # 第一个失败不拦住第二个，且两条都留了各自的日志。
    rows = _logs(db)
    assert [(r.channel_name, r.status) for r in rows] == [
        ("第一个", STATUS_FAILED),
        ("第二个", STATUS_SENT),
    ]


def test_dispatch_skips_undecryptable_channel(db, stub_send):
    # ENCRYPTION_KEY 换过之后，密文解不开的渠道应被跳过，不影响其他渠道。
    _channel(db, name="密钥失效", webhook_url_enc=b"not-a-valid-fernet-token")
    _channel(db, name="正常")

    asyncio.run(dispatch(OWNER, EVENT, title="t", body=""))

    assert [c["webhook_url"] for c in stub_send.calls] == ["https://open.feishu.cn/hook/xxx"]
    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].channel_name == "正常"


def test_dispatch_truncates_long_title_and_body(db, stub_send):
    _channel(db)
    asyncio.run(dispatch(OWNER, EVENT, title="标" * 500, body="正" * 5000))
    assert len(stub_send.calls[0]["title"]) == 128
    assert len(stub_send.calls[0]["body"]) == 1024


# ---- 文本渲染 ---------------------------------------------------------------


def test_render_text_shape():
    now = datetime(2026, 11, 3, 14, 22, 31)
    text = render_text("工作流失败：x #1", "步骤超时", now)
    assert text == "【yangvis】工作流失败：x #1\n步骤超时\n时间：2026-11-03 14:22:31"


def test_render_text_time_comes_from_beijing_now(monkeypatch):
    # 默认值必须走 beijing_now 而不是 datetime.now：app 容器没配 TZ 时后者是
    # UTC，群里收到的消息时间比北京时间早 8 小时（真机回报过）。
    monkeypatch.setattr(
        dispatch_module,
        "beijing_now",
        lambda: datetime(2026, 11, 4, 16, 28, 9, tzinfo=BEIJING_TZ),
    )
    assert render_text("测试消息", "正文") == "【yangvis】测试消息\n正文\n时间：2026-11-04 16:28:09"


# ---- 事件词表 ---------------------------------------------------------------


def test_normalize_events_dedupes_and_drops_unknown():
    assert normalize_events(["workflow.failed", "workflow.failed", "nope", ""]) == [
        "workflow.failed"
    ]
    assert normalize_events(None) == []
    assert normalize_events(["ops.pending", "ops.expired"]) == ["ops.pending", "ops.expired"]


# ---- 日志清理 ---------------------------------------------------------------


def test_prune_notify_logs_removes_only_expired(db):
    # created_at 显式传本地时间：SQLite 的 server_default 落 UTC，拿它和
    # datetime.now() 比会错 8 小时（见 test_ops_confirm 的时间口径说明）。
    db.add(
        NotifyLog(
            owner_id=OWNER,
            channel_id=1,
            channel_name="旧渠道",
            event=EVENT,
            title="t",
            status=STATUS_SENT,
            created_at=datetime.now() - timedelta(days=8),
        )
    )
    db.add(
        NotifyLog(
            owner_id=OWNER,
            channel_id=1,
            channel_name="新渠道",
            event=EVENT,
            title="t",
            status=STATUS_SENT,
            created_at=datetime.now() - timedelta(days=1),
        )
    )
    db.commit()

    deleted = prune_notify_logs(days=7)
    assert deleted == 1
    remaining = _logs(db)
    assert len(remaining) == 1
    assert remaining[0].channel_name == "新渠道"


def test_prune_notify_logs_nothing_to_delete(db):
    assert prune_notify_logs(days=7) == 0


def test_migrate_schema_adds_notify_log_body_to_legacy_db(engine, monkeypatch):
    """旧库（notify_log 还没有 body 列）启动迁移：补列、保数据、可重复执行。"""
    from sqlalchemy import inspect, text

    from app import bootstrap

    # bootstrap 用的是 import 时绑定的 engine / SessionLocal，须 patch 模块属性。
    monkeypatch.setattr(bootstrap, "engine", engine, raising=False)
    monkeypatch.setattr(
        bootstrap, "SessionLocal", sessionmaker(bind=engine, future=True)
    )

    # 造出升级前的旧表：当前 schema 的 notify_log 带 body，整体重建为无 body 形态。
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE notify_log"))
        conn.execute(
            text(
                "CREATE TABLE notify_log ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id BIGINT NOT NULL, "
                "channel_id BIGINT NOT NULL, channel_name VARCHAR(64) NOT NULL, "
                "event VARCHAR(32) NOT NULL, title VARCHAR(128) NOT NULL, "
                "status VARCHAR(8) NOT NULL, error VARCHAR(512), elapsed_ms BIGINT, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO notify_log (owner_id, channel_id, channel_name, event, title, status) "
                "VALUES (1, 1, '旧渠道', 'workflow.failed', '旧消息', 'sent')"
            )
        )

    bootstrap.migrate_schema()

    columns = {c["name"] for c in inspect(engine).get_columns("notify_log")}
    assert "body" in columns
    with engine.begin() as conn:
        # 历史行保留，新列可空所以为 NULL。
        title, body = conn.execute(text("SELECT title, body FROM notify_log")).one()
        assert title == "旧消息" and body is None

    # 幂等：列已存在时再跑一次必须是空操作而不是报错。
    bootstrap.migrate_schema()


def test_migrate_schema_adds_huawei_service_account_column(engine, monkeypatch):
    """旧库（notify_huawei_config 还没有 service_account_enc）启动迁移补列。"""
    from sqlalchemy import inspect, text

    from app import bootstrap

    monkeypatch.setattr(bootstrap, "engine", engine, raising=False)
    monkeypatch.setattr(
        bootstrap, "SessionLocal", sessionmaker(bind=engine, future=True)
    )

    # 重建为升级前的旧表形态：只有客户端凭证对两列，没有服务账号列。
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE notify_huawei_config"))
        conn.execute(
            text(
                "CREATE TABLE notify_huawei_config ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id BIGINT NOT NULL, "
                "app_id VARCHAR(32) NOT NULL, client_id_enc BLOB NOT NULL, "
                "client_secret_enc BLOB NOT NULL, target_type VARCHAR(8) NOT NULL, "
                "target VARCHAR(512) NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )

    bootstrap.migrate_schema()

    columns = {c["name"] for c in inspect(engine).get_columns("notify_huawei_config")}
    assert "service_account_enc" in columns

    # 幂等：再跑一次不报错。
    bootstrap.migrate_schema()


def test_bootstrap_prune_notify_logs_step_cleans_expired(db):
    """启动步骤表指向的包装函数确实在清理（防步骤名/指向写错后静默失效）。"""
    from app.bootstrap import prune_notify_logs as bootstrap_step

    db.add(
        NotifyLog(
            owner_id=OWNER,
            channel_id=1,
            channel_name="旧渠道",
            event=EVENT,
            title="t",
            status=STATUS_SENT,
            created_at=datetime.now() - timedelta(days=30),
        )
    )
    db.commit()

    bootstrap_step()
    assert _logs(db) == []
