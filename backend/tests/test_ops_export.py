"""「导出全部」的流式取数与四种格式写出测试。

真库上的分批取不到（共享实例，不该为测试造十万行），所以这里把取数那一层换成
脚本化的批次，剩下的编排——偏移推进、收尾、格式文本——全都是原样的代码。
"""
import asyncio
from types import SimpleNamespace

import pymysql
import pytest

from app.models.schemas import DbRowFilter, DbRowSort
from app.services import ops_database as db_ops


class FakeConn:
    """只记录收尾动作的假连接：导出流的 finally 必须碰到它。"""

    def __init__(self, fail_rollback: bool = False) -> None:
        self.closed = 0
        self.rolled_back = 0
        self._fail = fail_rollback

    def rollback(self) -> None:
        self.rolled_back += 1
        if self._fail:
            raise pymysql.Error("rollback boom")

    def close(self) -> None:
        self.closed += 1


# ---- 发给服务端的语句文本（假游标，不碰任何真库） ---------------------------------


class FakeCursor:
    """按语句前缀脚答好的假游标，顺手记下每条实际发出去的 SQL 与参数。"""

    def __init__(self, conn: "ScriptedConn") -> None:
        self.conn = conn
        self.description: tuple = ()
        self._rows: list = []

    def execute(self, sql, args=None) -> None:  # noqa: ANN001 - 形参对齐 pymysql
        self.conn.executed.append((" ".join(sql.split()), args))
        head = self.conn.script_for(sql)
        self.description = head.get("description", ())
        self._rows = head.get("rows", [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        return None


class ScriptedConn(FakeConn):
    def __init__(self, script) -> None:
        super().__init__()
        self.script = script
        self.executed: list = []

    def script_for(self, sql: str) -> dict:
        for prefix, reply in self.script.items():
            if sql.strip().upper().startswith(prefix.upper()):
                return reply
        raise AssertionError(f"没有为这条语句准备脚本：{sql}")

    def cursor(self, *args, **kwargs) -> FakeCursor:
        return FakeCursor(self)


ITEM = SimpleNamespace(id=1, db_type="mysql", writable=True)


@pytest.fixture
def connect(monkeypatch):
    """把 _connect_mysql 换成脚本化的假连接：这些测试只验发出去的语句文本。"""

    def install(script, conn=None):
        conn = conn or ScriptedConn(script)
        monkeypatch.setattr(db_ops, "_connect_mysql", lambda *a, **k: conn)
        return conn

    return install


def test_open_export_builds_count_probe_and_stable_order_without_params(connect):
    conn = connect({
        "SELECT COUNT(*)": {"rows": [(7,)]},
        "SELECT * FROM": {"description": (("id",), ("name",)), "rows": []},
        "SELECT COLUMN_NAME": {"rows": [("id", "PRI"), ("name", "")]},
    })
    # 建连之后的第一条业务语句就是 COUNT(*)，列名靠一条 LIMIT 0 的探针拿。
    session = db_ops._open_export_sync(ITEM, "", "shop", "orders", [], [], "")  # type: ignore[arg-type]
    try:
        assert [sql for sql, _ in conn.executed[:3]] == [
            "SELECT COUNT(*) FROM `shop`.`orders`",
            "SELECT * FROM `shop`.`orders` LIMIT 0",
            "SELECT COLUMN_NAME, COLUMN_KEY FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
        ]
        # 空参数必须是 None：传空序列会让 pymysql 去格式化手输条件里的字面 %。
        assert conn.executed[0][1] is None
        assert conn.executed[2][1] == ("shop", "orders")
        assert session.total == 7
        assert session.columns == ["id", "name"]
        # 没排序就钉主键：OFFSET 分页没有确定顺序会重复/漏行。
        assert session.head_sql == "SELECT * FROM `shop`.`orders` ORDER BY `id` ASC"
    finally:
        session.close()


def test_open_export_without_a_primary_key_orders_by_the_first_column(connect):
    connect({
        "SELECT COUNT(*)": {"rows": [(7,)]},
        "SELECT * FROM": {"description": (("name",), ("created_at",)), "rows": []},
        "SELECT COLUMN_NAME": {"rows": [("name", ""), ("created_at", "")]},
    })
    session = db_ops._open_export_sync(ITEM, "", "shop", "orders", [], [], "")  # type: ignore[arg-type]
    try:
        assert session.head_sql == "SELECT * FROM `shop`.`orders` ORDER BY `name` ASC"
    finally:
        session.close()


def test_open_export_keeps_user_sort_and_filters_as_params(connect):
    conn = connect({
        "SELECT COUNT(*)": {"rows": [(3,)]},
        "SELECT * FROM": {"description": (("id",), ("created_at",))},
    })
    session = db_ops._open_export_sync(  # type: ignore[arg-type]
        ITEM,
        "",
        "shop",
        "orders",
        [DbRowFilter(column="status", op="like", value="付")],
        [DbRowSort(column="created_at", direction="desc")],
        "amount > 10",
    )
    try:
        count_sql, count_args = conn.executed[0]
        assert count_sql == (
            "SELECT COUNT(*) FROM `shop`.`orders` "
            "WHERE (amount > 10) AND `status` LIKE %s"
        )
        # LIKE 的值在服务端包上百分号，前端只给裸词；手输条件照样参数化。
        assert count_args == ("%付%",)
        assert session.head_sql.endswith("ORDER BY `created_at` DESC")
    finally:
        session.close()


def test_export_batch_appends_limit_and_offset_to_the_same_head(connect):
    conn = connect({"SELECT * FROM": {"description": (("id",),), "rows": [(1,), (2,)]}})
    rows = db_ops._fetch_export_batch_sync(
        conn, "SELECT * FROM `shop`.`orders`", ("x",), 1000  # type: ignore[arg-type]
    )
    assert rows == [(1,), (2,)]
    assert conn.executed == [("SELECT * FROM `shop`.`orders` LIMIT 500 OFFSET 1000", ("x",))]


def test_open_export_closes_the_connection_when_a_statement_fails(connect):
    class Boom(ScriptedConn):
        def script_for(self, sql: str) -> dict:
            raise pymysql.Error("no such table")

    conn = Boom({})
    connect({}, conn)
    # 建连之后任何一步炸了都不能把连接漏在那里：响应还没开始，没人会来收尾。
    with pytest.raises(pymysql.Error):
        db_ops._open_export_sync(ITEM, "", "shop", "missing", [], [], "")  # type: ignore[arg-type]
    assert conn.closed == 1
def session(total: int = 3, conn=None) -> db_ops._ExportSession:
    return db_ops._ExportSession(
        conn or FakeConn(),  # type: ignore[arg-type]
        "shop",
        "orders",
        ["id", "name"],
        total,
        "SELECT * FROM `shop`.`orders`",
        None,
    )


async def _collect(it) -> str:
    return "".join([chunk.decode("utf-8") async for chunk in it])


@pytest.fixture
def batches(monkeypatch):
    """把取数换成脚本批次，并记下每批收到的偏移。"""
    recorded: list = []
    state = {"batches": []}

    def fake_fetch(conn, head_sql, args, offset):
        recorded.append(offset)
        return state["batches"].pop(0) if state["batches"] else []

    monkeypatch.setattr(db_ops, "_fetch_export_batch_sync", fake_fetch)

    def load(*groups):
        state["batches"] = [list(g) for g in groups]
        return recorded

    return load


# ---- 条件片段：浏览与导出共用 -----------------------------------------------------


def test_browse_clauses_combine_raw_where_and_structured_filters():
    where_sql, params, order_sql = db_ops._browse_clauses(
        [DbRowFilter(column="status", op="eq", value="paid")],
        [DbRowSort(column="id", direction="desc")],
        "amount > 10",
    )
    assert where_sql == " WHERE (amount > 10) AND `status` = %s"
    assert params == ["paid"]
    assert order_sql == " ORDER BY `id` DESC"


def test_browse_clauses_without_anything_yield_no_where():
    assert db_ops._browse_clauses([], [], "") == ("", [], "")


def test_quote_ident_doubles_backtick_in_table_names():
    assert db_ops._quote_ident("or`der") == "`or``der`"


# ---- 格式写出 -------------------------------------------------------------------


def test_csv_has_bom_crlf_and_quotes_only_when_needed(batches):
    batches([(1, "a,b"), (2, 'say "hi"'), (3, "plain")])
    out = asyncio.run(_collect(db_ops.stream_export(session(3), "csv")))
    assert out.startswith("\ufeffid,name\r\n")
    # 含逗号/引号的格子才加引号，普通值原样留着——整列都裹引号的 CSV 没法 diff。
    assert out.endswith('1,"a,b"\r\n2,"say ""hi"""\r\n3,plain\r\n')


def test_csv_keeps_newlines_inside_quotes(batches):
    batches([(1, "two\nlines")])
    out = asyncio.run(_collect(db_ops.stream_export(session(1), "csv")))
    assert '1,"two\nlines"' in out


def test_json_is_one_object_per_row_with_non_ascii_kept(batches):
    batches([(1, "已付款"), (2, None)])
    out = asyncio.run(_collect(db_ops.stream_export(session(2), "json")))
    assert out == '[\n  {"id": 1, "name": "已付款"},\n  {"id": 2, "name": null}\n]'


def test_empty_json_export_is_still_valid(batches):
    batches()
    out = asyncio.run(_collect(db_ops.stream_export(session(0), "json")))
    assert out == "[\n\n]"


def test_markdown_escapes_pipes_and_folds_newlines(batches):
    batches([(1, "a|b"), (2, "x\ny")])
    out = asyncio.run(_collect(db_ops.stream_export(session(2), "markdown")))
    assert out.splitlines() == [
        "| id | name |",
        "| --- | --- |",
        "| 1 | a\\|b |",
        "| 2 | x<br>y |",
    ]


def test_insert_groups_a_batch_into_one_statement(batches):
    batches([(1, "ok"), (2, None)])
    out = asyncio.run(_collect(db_ops.stream_export(session(2), "insert")))
    assert out == (
        "INSERT INTO `shop`.`orders` (`id`, `name`) VALUES\n"
        "(1, 'ok'),\n"
        "(2, NULL);\n\n"
    )


def test_insert_uses_stringified_values_for_dates_and_bytes(batches):
    import datetime

    batches([(1, datetime.date(2026, 9, 28)), (2, b"\xffbytes")])
    out = asyncio.run(_collect(db_ops.stream_export(session(2), "insert")))
    assert "'2026-09-28'" in out
    # BLOB 走 utf-8 替换解码，与浏览页所见一致：导出文件不会比屏幕上的更准。
    assert "'" + chr(0xFFFD) + "bytes'" in out


# ---- 分批编排 -------------------------------------------------------------------


def full(n: int):
    """凑一批满额的行：短批意味着取完了，测试里要往下走就得给满。"""
    return [(i, f"n{i}") for i in range(n)]


def test_export_stops_at_the_declared_total_without_another_batch(batches):
    recorded = batches(full(db_ops._EXPORT_BATCH), full(db_ops._EXPORT_BATCH), [(9999, "extra")])
    total = db_ops._EXPORT_BATCH * 2
    out = asyncio.run(_collect(db_ops.stream_export(session(total), "csv")))
    # 第三批在总数之外：多取一批就是把屏幕之外的行也塞进文件。
    assert recorded == [0, db_ops._EXPORT_BATCH]
    assert "9999,extra" not in out


def test_export_advances_offsets_by_rows_actually_returned(batches):
    recorded = batches(full(db_ops._EXPORT_BATCH), [(999, "tail")])
    asyncio.run(_collect(db_ops.stream_export(session(db_ops._EXPORT_BATCH + 1), "csv")))
    assert recorded == [0, db_ops._EXPORT_BATCH]


def test_short_batch_ends_the_stream_even_if_count_says_more(batches):
    # 只读事务里 COUNT(*) 与取数同快照，短批就是取完了，不该再按总数空转。
    recorded = batches([(1, "a")])
    asyncio.run(_collect(db_ops.stream_export(session(10), "csv")))
    assert recorded == [0]


def test_connection_is_closed_when_the_client_walks_away(batches):
    batches([(1, "a")] * 50)
    conn = FakeConn()
    chunks = db_ops.stream_export(session(50, conn), "csv")

    async def abort():
        first = await chunks.__anext__()
        await chunks.aclose()
        return first

    asyncio.run(abort())
    assert conn.closed == 1


def test_rollback_failure_still_closes_the_connection(batches):
    batches([(1, "a")])
    conn = FakeConn(fail_rollback=True)
    asyncio.run(_collect(db_ops.stream_export(session(1, conn), "csv")))
    assert conn.closed == 1


def test_unknown_format_is_refused_before_any_row_is_read(batches):
    with pytest.raises(db_ops.OpsDbError):
        asyncio.run(_collect(db_ops.stream_export(session(1), "xlsx")))


def test_open_export_rejects_a_hostile_where_fragment():
    # 手输条件与浏览同一道只读网关：分号后面藏什么都进不到连接里。
    with pytest.raises(db_ops.OpsDbError, match="安全检查"):
        asyncio.run(db_ops.open_export(object(), "shop", "orders", where="1=1; DROP TABLE x"))


def test_where_longer_than_the_browse_limit_is_refused():
    with pytest.raises(db_ops.OpsDbError, match="太长"):
        asyncio.run(db_ops.open_export(object(), "shop", "orders", where="a" * 1025))
