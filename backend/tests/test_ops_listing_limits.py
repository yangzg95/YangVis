"""批量执行回显的两道上限，与表列表「少列了多少张」的口径。

界面上「已截断」「只列出前 N 张」这两句话的数字都来自服务端。真库凑不出这些
边界（一张表不满 200 行，information_schema 里也没有上千张表），所以把建连换成
脚本化的假连接：验的是真正发出去的语句文本，以及回给前端的那些字段。
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import OpsDatabase
from app.models.schemas import CurrentUser, DbTableItem
from app.routers import ops as ops_router
from app.services.ops import db_exec
from app.services.ops import db_schema

ITEM = SimpleNamespace(id=1, db_type="mysql", writable=False, password_enc=None)

_TABLES_SQL = "SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS, TABLE_COMMENT"
_COUNT_SQL = "SELECT COUNT(*)"


class FakeCursor:
    """按语句前缀答好的假游标；``fetchmany`` 故意不裁剪，让上层自己发现超了。"""

    def __init__(self, script, executed) -> None:
        self._script = script
        self._executed = executed
        self.description: tuple = ()
        self.rowcount = 0
        self._rows: list = []

    def execute(self, sql, args=None) -> None:  # noqa: ANN001 - 形参对齐 pymysql
        text = " ".join(sql.split())
        self._executed.append((text, args))
        for prefix, reply in self._script.items():
            if text.upper().startswith(prefix.upper()):
                self.description = reply.get("description", ())
                self._rows = list(reply.get("rows", []))
                self.rowcount = reply.get("rowcount", len(self._rows))
                return
        raise AssertionError(f"没有为这条语句准备脚本：{sql}")

    def fetchmany(self, size):  # noqa: ANN001 - 形参对齐 pymysql
        return self._rows[:size]

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        return None


class ScriptedConn:
    def __init__(self, script) -> None:
        self.script = script
        self.executed: list = []

    def cursor(self, *args, **kwargs) -> FakeCursor:
        return FakeCursor(self.script, self.executed)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def connect(monkeypatch):
    """建连换成假连接：``_query_mysql`` 与 ``_run_mysql_batch`` 都从这里拿连接。"""

    def install(script):
        conn = ScriptedConn(script)
        monkeypatch.setattr(db_exec, "_connect_mysql", lambda *a, **k: conn)
        return conn

    return install


# ---- 批量执行的上限回显 -------------------------------------------------------


def test_batch_result_echoes_the_limits_the_server_actually_applied(connect):
    settings = get_settings()
    connect({"SELECT 1": {"description": (("a",),), "rows": [(1,)]}})

    result, forbidden = asyncio.run(db_exec.execute_batch(ITEM, "SELECT 1"))

    assert forbidden is False
    assert result.row_limit == int(settings.OPS_SQL_ROW_LIMIT)
    assert result.stmt_timeout_seconds == int(settings.OPS_CMD_TIMEOUT)
    # 没截断时消息照旧只报行数，别把「上限」挂在每一句上。
    assert result.statements[0].message == "1 行"


def test_truncated_statement_names_the_limit_in_its_own_message(connect):
    cap = int(get_settings().OPS_SQL_ROW_LIMIT)
    # 多给一行：驱动侧按 cap+1 取，取满才知道「后面还有」。
    connect({
        "SELECT * FROM orders": {
            "description": (("id",),),
            "rows": [(i,) for i in range(cap + 1)],
        }
    })

    result, _ = asyncio.run(db_exec.execute_batch(ITEM, "SELECT * FROM orders"))

    stmt = result.statements[0]
    assert stmt.truncated is True
    assert stmt.row_count == cap
    # 「200 行」会被当成全量，截断这件事必须写进消息本身，不能只靠一个标记位。
    assert f"上限 {cap}" in stmt.message


def test_a_refused_statement_never_reaches_the_connection(connect):
    conn = connect({})

    result, forbidden = asyncio.run(db_exec.execute_batch(ITEM, "DELETE FROM orders"))

    assert forbidden is True
    assert result.failed == 1
    assert conn.executed == [], "被网关拦下的语句不该占数据库连接"
    # 上限是被拒时也要回显的：界面那句「停止等待后服务端还会跑多久」用的是它。
    assert result.stmt_timeout_seconds == int(get_settings().OPS_CMD_TIMEOUT)


# ---- 表列表：过滤与计数的口径 ------------------------------------------------


def test_unfiltered_table_listing_asks_for_names_only(connect):
    conn = connect({
        _TABLES_SQL: {
            "description": (("TABLE_NAME",), ("TABLE_TYPE",), ("ENGINE",),
                            ("TABLE_ROWS",), ("TABLE_COMMENT",)),
            "rows": [("orders", "BASE TABLE", "InnoDB", 10, None)],
        },
    })

    items = asyncio.run(db_schema.list_tables(ITEM, "shop"))

    assert [item.name for item in items] == ["orders"]
    sql, args = conn.executed[0]
    assert "LIKE" not in sql
    assert args == ("shop",)
    assert sql.endswith("ORDER BY TABLE_TYPE, TABLE_NAME")


def test_table_keyword_goes_to_the_server_as_an_escaped_like_pattern(connect):
    conn = connect({_TABLES_SQL: {"rows": []}})

    asyncio.run(db_schema.list_tables(ITEM, "shop", "user%name"))

    sql, args = conn.executed[0]
    # 转义符用 !，不用反斜杠：NO_BACKSLASH_ESCAPES 下口径不变。
    assert "TABLE_NAME LIKE %s ESCAPE '!'" in sql
    assert args == ("shop", "%user!%name%"), "用户敲的 % 要按字面量匹配"


def test_wildcard_and_escape_char_in_the_keyword_are_all_literal(connect):
    conn = connect({_TABLES_SQL: {"rows": []}})

    asyncio.run(db_schema.list_tables(ITEM, "shop", "a!b_c"))

    assert conn.executed[0][1] == ("shop", "%a!!b!_c%")


def test_list_and_count_share_one_where_clause(connect):
    conn = connect({
        _TABLES_SQL: {"description": (("TABLE_NAME",),), "rows": []},
        _COUNT_SQL: {"description": (("COUNT(*)",),), "rows": [(1234,)]},
    })

    asyncio.run(db_schema.list_tables(ITEM, "shop", "ord"))
    total = asyncio.run(db_schema.count_tables(ITEM, "shop", "ord"))

    listed = next((sql, args) for sql, args in conn.executed if sql.startswith(_TABLES_SQL))
    counted = next((sql, args) for sql, args in conn.executed if sql.startswith(_COUNT_SQL))
    where = "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE %s ESCAPE '!'"
    # 两处同源，否则前端拿 total 减 items 算出来的「少列了 N 张」是错的。
    assert where in listed[0] and where in counted[0]
    assert listed[1] == counted[1] == ("shop", "%ord%")
    assert total == 1234


def test_count_without_a_keyword_only_filters_by_schema(connect):
    conn = connect({_COUNT_SQL: {"description": (("COUNT(*)",),), "rows": [(7,)]}})

    total = asyncio.run(db_schema.count_tables(ITEM, "shop"))

    assert conn.executed[0] == (
        "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
        ("shop",),
    )
    assert total == 7


# ---- HTTP 一层：total 的语义与关键字归一 ----------------------------------------

OWNER = 1


@pytest.fixture()
def env(tmp_path):
    """真路由 + 假服务层：只验 total/like 这一段的契约，不碰任何数据库。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'listing.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()
    mysql = OpsDatabase(
        owner_id=OWNER, name="订单库", db_type="mysql", host="10.0.0.1", port=3306, username="ro"
    )
    db.add(mysql)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=OWNER, username="t", is_admin=True
    )
    yield SimpleNamespace(mysql=mysql.id, client=TestClient(app))
    app.dependency_overrides.clear()


def _stub_listing(monkeypatch) -> list:
    """把表列表与计数换成假数据，并记下路由真正传下来的关键字。"""
    seen: list = []

    async def fake_list(item, schema, like=None):
        seen.append(like)
        return [DbTableItem(name="orders"), DbTableItem(name="users")]

    async def fake_count(item, schema, like=None):
        return 1234

    monkeypatch.setattr(ops_router.db_schema, "list_tables", fake_list)
    monkeypatch.setattr(ops_router.db_schema, "count_tables", fake_count)
    return seen


def test_tables_endpoint_reports_the_true_total_beyond_the_returned_items(env, monkeypatch):
    _stub_listing(monkeypatch)
    res = env.client.get(f"/api/ops/databases/{env.mysql}/schemas/shop/tables")
    body = res.json()["data"]
    # total 是命中条件的全量表数，items 是被上限截过的两页：前端靠这个差值说话。
    assert body["total"] == 1234
    assert len(body["items"]) == 2


def test_tables_endpoint_passes_the_keyword_through_and_blanks_whitespace(env, monkeypatch):
    seen = _stub_listing(monkeypatch)
    env.client.get(
        f"/api/ops/databases/{env.mysql}/schemas/shop/tables", params={"like": "  ord  "}
    )
    assert seen == ["ord"], "两端空白归一掉，别把 ' ord ' 当关键字查"
    env.client.get(
        f"/api/ops/databases/{env.mysql}/schemas/shop/tables", params={"like": "   "}
    )
    assert seen[-1] is None, "全空白的关键字等于没过滤，不该带一个 LIKE 下去"
