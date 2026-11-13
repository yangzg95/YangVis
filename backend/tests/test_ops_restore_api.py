"""``POST /ops/databases/*/schemas/*/restore`` 这一层的契约。

还原是写操作里最重的一条，所以这一层只管四件事：闸门与类型判定必须排在读文件
和开会话之前（只读账号不该等完一次上传）、超上限与非 UTF-8 仍是正常的错误信封、
一旦开始就只能吐进度事件、以及最后一句跑完之后审计到底记成了什么。
逐句怎么数、什么时候停手在 ``tests/test_ops_restore.py`` 里钉。

SQLite 跑文件库（收尾审计另开一个 session，内存库对不了话），MySQL 连接换成
假连接——整份脚本从头到尾没碰过任何真库。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.deps import require_user
from app.errors import CODE_OPS_EXEC_FAILED, CODE_OPS_FORBIDDEN
from app.main import app
from app.models.entities import OpsAuditLog, OpsDatabase
from app.models.schemas import CurrentUser
from app.routers import ops as ops_router
from app.services.ops import db_restore as db_restore

OWNER = 1
URL = "/api/ops/databases/{db_id}/schemas/shop/restore"
# 一句被网关拦下的 GRANT：不碰假连接的答复表就能造出「有失败」的现场。
SCRIPT_OK = "CREATE TABLE `orders` (`id` int);\nINSERT INTO `orders` VALUES (1),(2);\n"
SCRIPT_MIXED = SCRIPT_OK + "GRANT ALL ON *.* TO 'app'@'localhost';\nSELECT COUNT(*) FROM `orders`;\n"


class FakeConn:
    """只认「执行成功」的假连接：还原的计数与事件不在这一层验收。"""

    def __init__(self) -> None:
        self.executed: List[str] = []
        self.closed = 0
        self.connect_kwargs: List[dict] = []

    def cursor(self, *args, **kwargs) -> "FakeCursor":
        return FakeCursor(self)

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed += 1


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def execute(self, sql, args=None) -> None:  # noqa: ANN001 - 形参对齐 pymysql
        self.conn.executed.append(" ".join(sql.split()))

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        return None


def _frames(body: str) -> List[Dict]:
    """SSE 帧 → 事件列表（``event`` 与 ``data`` 合成一个字典）。"""
    out: List[Dict] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        event: Optional[str] = None
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if event:
            out.append({"event": event, **(json.loads(data) if data else {})})
    return out


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'restore.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, future=True)
    db = factory()
    writable = OpsDatabase(
        owner_id=OWNER, name="订单库", db_type="mysql", host="10.0.0.1", port=3306,
        username="rw", db_name="shop", writable=True,
    )
    readonly = OpsDatabase(
        owner_id=OWNER, name="只读副本", db_type="mysql", host="10.0.0.2", port=3306,
        username="ro", db_name="shop", writable=False,
    )
    redis = OpsDatabase(owner_id=OWNER, name="缓存", db_type="redis", host="10.0.0.3", port=6379)
    db.add_all([writable, readonly, redis])
    db.commit()

    conn = FakeConn()
    monkeypatch.setattr(db_restore, "_password_of", lambda item: "pw")
    # 会话不回收这件事在服务层测试里钉；这里要的是「一次还原一条连接」。
    monkeypatch.setattr(
        db_restore,
        "_connect_mysql",
        lambda item, password, database=None, **kwargs: conn,
    )
    # 收尾审计走 SessionLocal（请求级那条在流开始前就关了），因此指向同一个文件库。
    monkeypatch.setattr(ops_router, "SessionLocal", factory)

    def login(ops_write: bool = True, is_admin: bool = True):
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[require_user] = lambda: CurrentUser(
            user_id=OWNER, username="t", is_admin=is_admin, ops_write=ops_write
        )

    login()
    yield SimpleNamespace(
        db=db, conn=conn, login=login,
        writable=writable.id, readonly=readonly.id, redis=redis.id,
        client=TestClient(app),
    )
    app.dependency_overrides.clear()


def _post(client, db_id, script: str, **kwargs):
    return client.post(
        URL.format(db_id=db_id),
        files={"file": ("shop.sql", script.encode("utf-8"), "application/sql")},
        **kwargs,
    )


# ---- 排在花钱之前的判定 ----------------------------------------------------------


def test_redis_connection_is_rejected_before_the_upload_is_read(env):
    res = _post(env.client, env.redis, SCRIPT_OK)
    assert res.status_code == 400
    assert "MySQL" in res.json()["message"]
    # 台账类型不对，连文件都不该收进来。
    assert env.conn.executed == []


def test_readonly_connection_refuses_without_opening_a_session(env):
    res = _post(env.client, env.readonly, SCRIPT_OK)
    body = res.json()
    assert res.status_code == 200 and body["code"] == CODE_OPS_FORBIDDEN
    assert env.conn.executed == []
    log = env.db.scalar(select(OpsAuditLog).order_by(OpsAuditLog.id.desc()))
    assert log.verdict == "forbidden" and log.success is False
    assert "[还原 shop]" in log.command


def test_user_without_the_write_pass_is_refused(env):
    # 管理员天然有写权限，所以这条要的是「普通账号 + 没开 ops_write」。
    env.login(ops_write=False, is_admin=False)
    res = _post(env.client, env.writable, SCRIPT_OK)
    assert res.json()["code"] == CODE_OPS_FORBIDDEN
    assert env.conn.executed == []


def test_oversized_script_never_reaches_the_database(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "OPS_RESTORE_MAX_BYTES", 32)
    res = _post(env.client, env.writable, SCRIPT_OK * 10)
    body = res.json()
    assert body["code"] == CODE_OPS_EXEC_FAILED and "文件过大" in body["message"]
    assert env.conn.executed == []
    log = env.db.scalar(select(OpsAuditLog).order_by(OpsAuditLog.id.desc()))
    assert log.success is False and "文件过大" in (log.error or "")


def test_content_length_over_the_cap_is_rejected_early(env, monkeypatch):
    """Content-Length 提前拒绝：不用先收完几十 MB 再说不要。"""
    monkeypatch.setattr(get_settings(), "OPS_RESTORE_MAX_BYTES", 64)
    res = env.client.post(
        URL.format(db_id=env.writable),
        headers={"Content-Length": "900000"},
        files={"file": ("shop.sql", SCRIPT_OK.encode(), "application/sql")},
    )
    assert "文件过大" in res.json()["message"]
    assert env.conn.executed == []


def test_non_utf8_script_is_told_by_byte_offset(env):
    res = env.client.post(
        URL.format(db_id=env.writable),
        files={"file": ("shop.sql", "CREATE TABLE 订单 (id int);".encode("gbk"), "application/sql")},
    )
    body = res.json()
    assert body["code"] == CODE_OPS_EXEC_FAILED
    assert "UTF-8" in body["message"]
    assert env.conn.executed == []


def test_script_without_statements_stays_an_envelope(env):
    res = _post(env.client, env.writable, "-- 只有注释\n")
    body = res.json()
    assert res.status_code == 200 and body["code"] == CODE_OPS_EXEC_FAILED
    assert "没有可执行的语句" in body["message"]


# ---- 进度流与收尾审计 ------------------------------------------------------------


def test_restore_streams_start_progress_done(env):
    res = _post(env.client, env.writable, SCRIPT_MIXED)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    frames = _frames(res.text)
    assert [frame["event"] for frame in frames] == ["start", "progress", "done"]
    assert frames[0]["total"] == 4 and frames[0]["schema"] == "shop"
    assert frames[0]["stmt_timeout_seconds"] == get_settings().OPS_RESTORE_TIMEOUT
    done = frames[-1]
    assert done["succeeded"] == 2 and done["failed"] == 1
    assert done["skipped"] == 1 and done["refused"] == 1
    assert done["errors"][0]["sql"].startswith("GRANT")
    # SELECT 一句都没送进数据库：还原不要结果集，它只会吃内存。
    assert not any(sql.upper().startswith("SELECT") for sql in env.conn.executed)


def test_restore_session_is_writable_and_not_returned_to_the_pool(env, monkeypatch):
    seen: List[dict] = {}

    def spy(item, password, database=None, writable=False, *, timeout_seconds=None, pool=True):
        seen.update(
            {
                "database": database,
                "writable": writable,
                "timeout": timeout_seconds,
                "pool": pool,
                "pw": password,
            }
        )
        return FakeConn()

    monkeypatch.setattr(db_restore, "_connect_mysql", spy)
    _post(env.client, env.writable, SCRIPT_OK)
    assert seen == {
        "database": "shop",
        "writable": True,
        "timeout": get_settings().OPS_RESTORE_TIMEOUT,
        "pool": False,
        "pw": "pw",
    }


def test_successful_restore_is_audited_with_the_counts(env):
    res = _post(env.client, env.writable, SCRIPT_OK)
    assert res.headers["content-type"].startswith("text/event-stream")
    env.db.expire_all()
    log = env.db.scalar(select(OpsAuditLog).order_by(OpsAuditLog.id.desc()))
    assert log.verdict == "write" and log.success is True
    assert "[还原 shop] shop.sql" in log.command
    assert "成功 2" in log.command and "失败 0" in log.command


def test_partial_restore_is_audited_as_a_failure_with_the_first_error(env):
    _post(env.client, env.writable, SCRIPT_MIXED)
    env.db.expire_all()
    log = env.db.scalar(select(OpsAuditLog).order_by(OpsAuditLog.id.desc()))
    assert log.success is False
    assert "成功 2" in log.command and "失败 1" in log.command
    # 头一条错误是网关给的原话，界面与审计读同一句。
    assert log.error and "不允许" in log.error


def test_stop_on_error_is_passed_through(env, monkeypatch):
    stopped: List[bool] = []
    real = db_restore.stream_restore

    def spy(session, stop_on_error=False):
        stopped.append(stop_on_error)
        return real(session, stop_on_error)

    monkeypatch.setattr(db_restore, "stream_restore", spy)
    _post(env.client, env.writable, SCRIPT_OK, params={"stop_on_error": True})
    assert stopped == [True]
