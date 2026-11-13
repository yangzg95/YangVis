"""还原/导入的服务层：脚本怎么拆、闸门在哪拦、进度怎么数。

与备份测试同一套打法——连接换成脚本化的假连接，不碰任何真库。验的是四件事：
拆句与逐句定策（查询跳过、网关拦下算失败）、写闸门在开连接之前就拒掉、
逐句独立而连续失败要停手、以及会话时间/不回收这两个还原专用的连接口径。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Dict, List, Optional

import pymysql
import pytest

from app.config import get_settings
from app.services.ops import db_exec as db_exec
from app.services.ops import db_restore as db_restore
from app.services.ops.database import OpsDbError, OpsDbForbidden

ITEM = SimpleNamespace(
    id=7,
    db_type="mysql",
    name="订单库",
    host="10.0.0.1",
    port=3306,
    username="rw",
    db_name="shop",
    writable=True,
)

SCRIPT = """
CREATE TABLE `orders` (`id` int);
INSERT INTO `orders` VALUES (1),(2);
GRANT ALL ON *.* TO 'app'@'localhost';
SELECT COUNT(*) FROM `orders`;
"""

CREATE_SQL = "CREATE TABLE `orders` (`id` int)"
INSERT_SQL = "INSERT INTO `orders` VALUES (1),(2)"


class FakeConn:
    """按脚本答复的假连接：``replies`` 里点名的语句抛对应异常，其余成功。

    ``default_error`` 给「每一句都错」的场景（连环失败、样本上限），
    ``hard_error`` + ``hard_after`` 给「连接本身断了」——那种异常不是 ``pymysql.Error``，
    跑的是第几句都救不回来，整批要就此收手。
    第二次 ``close()`` 照 pymysql 的脾气抛错：收尾路径写重一次就是 500。
    """

    def __init__(
        self,
        replies: Optional[Dict[str, Exception]] = None,
        default_error: Optional[Exception] = None,
        hard_error: Optional[Exception] = None,
        hard_after: Optional[int] = None,
    ) -> None:
        self.replies = replies or {}
        self.default_error = default_error
        self.hard_error = hard_error
        self.hard_after = hard_after
        self.executed: List[str] = []
        self.closed = 0
        self.rolled_back = 0

    def cursor(self, *args, **kwargs) -> "FakeCursor":
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        if self.closed:
            raise pymysql.Error("Already closed")
        self.closed += 1


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def execute(self, sql, args=None) -> None:  # noqa: ANN001 - 形参对齐 pymysql
        if self.conn.hard_error is not None and len(self.conn.executed) >= (self.conn.hard_after or 0):
            raise self.conn.hard_error
        text = " ".join(sql.split())
        self.conn.executed.append(text)
        boom = self.conn.replies.get(text, self.conn.default_error)
        if boom is not None:
            raise boom

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        return None


def _session(conn: FakeConn, script: str = SCRIPT, schema: str = "shop") -> db_restore.RestoreSession:
    items = db_restore.prepare_script(ITEM, script, allow_write=True)
    uses = [t for t in (db_restore._use_target(i.code) for i in items) if t]
    return db_restore.RestoreSession(conn, items, schema, uses, 300.0)


def _run(stream) -> List[dict]:
    async def collect():
        return [event async for event in stream]

    return asyncio.run(collect())


def _statements(count: int) -> str:
    return ";\n".join(f"INSERT INTO t VALUES ({i})" for i in range(count)) + ";"


# ---- 拆句与逐句定策 -------------------------------------------------------------


def test_query_statements_are_skipped_not_executed():
    items = db_restore.prepare_script(ITEM, SCRIPT, allow_write=True)
    by_head = {item.code.split()[0].lower(): item for item in items}
    assert by_head["select"].skip is True and by_head["select"].reason is None
    # 被网关拦下的不是「跳过」：它压根不该进数据库，且要算进失败数里说清楚。
    assert by_head["grant"].reason and by_head["grant"].skip is False
    assert by_head["create"].reason is None and by_head["create"].skip is False


def test_header_comment_does_not_make_a_query_run():
    """备份文件的第一句永远带着标题注释，判定得看去注释之后的正文。

    照着原文判就是把 ``SELECT * FROM big`` 当成写语句送进游标——pymysql 会把
    整个结果集读进内存，而还原根本不需要它的任何一行。
    """
    items = db_restore.prepare_script(ITEM, "-- YangVis 数据库备份\nSELECT * FROM big;\n", True)
    assert items[0].skip is True and items[0].sql.startswith("-- YangVis")


def test_comment_only_script_refuses_before_opening_a_connection():
    with pytest.raises(OpsDbError, match="没有可执行的语句"):
        db_restore.prepare_script(ITEM, "-- 只是注释\n/* 还有这个 */", allow_write=True)


def test_redis_connection_has_no_script_restore():
    redis = SimpleNamespace(**{**ITEM.__dict__, "db_type": "redis"})
    with pytest.raises(OpsDbError, match="Redis"):
        db_restore.prepare_script(redis, SCRIPT, allow_write=True)


@pytest.mark.parametrize("allow_write,writable", [(False, True), (True, False)])
def test_missing_write_pass_is_refused_without_a_connection(allow_write, writable):
    """没有写通行证就拒掉，不退化成只读会话「试一下」。

    试一下的结果是一份脚本从头到尾的权限错误——闸门写在开连接之前，
    用户看到的是一句话，不是几百条同一个报错。
    """
    item = SimpleNamespace(**{**ITEM.__dict__, "writable": writable})
    with pytest.raises(OpsDbForbidden, match="允许写入"):
        db_restore.prepare_script(item, SCRIPT, allow_write=allow_write)


def test_use_statements_are_collected_for_the_ui():
    session = _session(FakeConn(), "USE `archive`;\nINSERT INTO t VALUES (1);\nUSE sales;\n")
    assert session.uses == ["archive", "sales"]


# ---- 执行与计数 ----------------------------------------------------------------


def test_stream_reports_start_progress_done_and_keeps_statement_order():
    conn = FakeConn()
    events = _run(db_restore.stream_restore(_session(conn)))
    assert [event["type"] for event in events] == ["start", "progress", "done"]
    assert events[0]["total"] == 4 and events[0]["schema"] == "shop"
    # 顺序就是文件里的顺序：备份文件的 DDL 永远排在它的 INSERT 前面。
    assert conn.executed[:2] == [CREATE_SQL, INSERT_SQL]
    done = events[-1]
    assert done["succeeded"] == 2 and done["failed"] == 1 and done["skipped"] == 1
    assert done["refused"] == 1 and done["stopped"] is False and done["not_run"] == 0
    assert done["errors"][0]["sql"].startswith("GRANT")


def test_a_single_failure_does_not_stop_the_rest():
    conn = FakeConn({CREATE_SQL: pymysql.Error("Table 'orders' already exists")})
    events = _run(db_restore.stream_restore(_session(conn)))
    assert conn.executed == [CREATE_SQL, INSERT_SQL]  # GRANT 没送进来，SELECT 只是跳过
    assert events[-1]["succeeded"] == 1 and events[-1]["failed"] == 2


def test_stop_on_error_halts_at_the_first_failure():
    conn = FakeConn({CREATE_SQL: pymysql.Error("no such table")})
    events = _run(db_restore.stream_restore(_session(conn), stop_on_error=True))
    assert conn.executed == [CREATE_SQL]
    done = events[-1]
    assert done["stopped"] is True and "第一个错误" in done["stop_reason"]
    assert done["not_run"] == 3


def test_consecutive_failures_trip_the_breaker(monkeypatch):
    """开头就连环错多半是同一个原因，后面几万句不必再刷进界面。"""
    monkeypatch.setattr(db_restore, "MAX_CONSECUTIVE_FAILURES", 3)
    conn = FakeConn(default_error=pymysql.Error("denied for user"))
    events = _run(db_restore.stream_restore(_session(conn, _statements(20))))
    assert len(conn.executed) == 3
    done = events[-1]
    assert done["stopped"] is True and "连续 3 句" in done["stop_reason"]
    assert done["failed"] == 3 and done["total"] == 20


def test_error_samples_are_capped(monkeypatch):
    """样本有上限，计数没有：界面拿到的是「先错在哪几句」，摘要拿到的是全量。"""
    monkeypatch.setattr(db_restore, "MAX_ERROR_SAMPLES", 2)
    monkeypatch.setattr(db_restore, "CHUNK_STATEMENTS", 1)
    conn = FakeConn(default_error=pymysql.Error("boom"))
    events = _run(db_restore.stream_restore(_session(conn, _statements(10))))
    assert len(events[-1]["errors"]) == 2
    assert events[-1]["failed"] == 10


def test_session_failure_stops_the_chunk_and_counts_what_never_ran():
    """连接本身断了：正在执行的这句算失败，后面压根没跑到的单独计数。

    把两者混成一堆「失败」，界面就会说这份脚本跑过了——它没有。
    """
    gone = RuntimeError("MySQL server has gone away")
    conn = FakeConn(hard_error=gone, hard_after=2)
    events = _run(db_restore.stream_restore(_session(conn, _statements(4))))
    done = events[-1]
    assert len(conn.executed) == 2
    assert done["stopped"] is True and "会话中断" in done["stop_reason"]
    assert done["succeeded"] == 2 and done["failed"] == 1 and done["not_run"] == 1


def test_close_runs_once_even_though_two_paths_reach_it():
    conn = FakeConn()
    session = _session(conn)
    _run(db_restore.stream_restore(session))
    session.close()  # 路由层的兜底再来一次：会话自己认第一次。
    assert conn.closed == 1 and conn.rolled_back == 1


# ---- 会话口径：长时限 + 不回收 ---------------------------------------------------


def test_open_restore_asks_for_a_long_lived_unpooled_writable_session(monkeypatch):
    calls: List[dict] = []
    conn = FakeConn()

    def fake_connect(item, password, database=None, writable=False, *, timeout_seconds=None, pool=True):
        calls.append(
            {"database": database, "writable": writable, "timeout": timeout_seconds, "pool": pool}
        )
        return conn

    monkeypatch.setattr(db_restore, "_connect_mysql", fake_connect)
    monkeypatch.setattr(db_restore, "_password_of", lambda item: "pw")
    session = asyncio.run(db_restore.open_restore(ITEM, SCRIPT, "shop", allow_write=True))
    assert calls == [
        {
            "database": "shop",
            "writable": True,
            "timeout": get_settings().OPS_RESTORE_TIMEOUT,
            "pool": False,
        }
    ]
    assert session.total == 4
    session.close()


def test_connect_failure_becomes_a_business_error(monkeypatch):
    """连不上库要落在流之前：那时还能给一个正常的业务错误信封。"""

    def boom(*args, **kwargs):
        raise pymysql.OperationalError("Access denied for user 'rw'")

    monkeypatch.setattr(db_restore, "_connect_mysql", boom)
    monkeypatch.setattr(db_restore, "_password_of", lambda item: "pw")
    with pytest.raises(OpsDbError, match="Access denied"):
        asyncio.run(db_restore.open_restore(ITEM, SCRIPT, "shop", allow_write=True))


def test_restore_connection_keeps_the_long_timeout_and_never_re_enters_the_pool(monkeypatch):
    """还原会话跑过 ``USE``，默认库已经跟着脚本走了：还回池里就是给下一个人换库。"""
    created: List[dict] = []
    real_conn = FakeConn()
    monkeypatch.setattr(db_exec, "_POOL", {})

    def fake_connect(**kwargs):
        created.append(kwargs)
        return real_conn

    monkeypatch.setattr(db_exec.pymysql, "connect", fake_connect)
    conn = db_exec._connect_mysql(
        ITEM, "pw", "shop", writable=True, timeout_seconds=300.0, pool=False
    )
    assert created[0]["read_timeout"] == 300 and created[0]["autocommit"] is True
    assert "SET SESSION TRANSACTION READ ONLY" not in real_conn.executed
    assert "SET SESSION MAX_EXECUTION_TIME" in real_conn.executed[-1]

    conn.close()
    assert real_conn.closed == 1
    assert db_exec._POOL == {}
