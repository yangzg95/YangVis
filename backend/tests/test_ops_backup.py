"""「一键备份」的备料与 .sql 写出测试：整库，以及用户挑出来的几张表。

与导出测试同一套打法：连接换成脚本化的假连接（不碰任何真库），取数那一层换成
脚本批次，验的是备料阶段发了哪些语句、上限在哪一步拦下、文件文本长什么样。
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pymysql
import pytest

from app.config import get_settings
from app.services.ops import db_backup
from app.services.ops import db_data as db_data
from app.services.ops.database import OpsDbError

# 备料阶段跑在 asyncio.to_thread 之外的同步函数里，测试直接调它即可。
ITEM = SimpleNamespace(id=7, db_type="mysql", name="订单库", host="10.0.0.1", port=3306)


class FakeConn:
    """按 pymysql 的脾气来的假连接：第二次 close() 直接抛 Already closed。

    这不是吹毛求疵——网盘那条路径要在流之外再收尾一次，真实驱动下写错一次就是 500。
    """

    def __init__(self) -> None:
        self.closed = 0
        self.rolled_back = 0
        self.executed: list = []

    def reply_for(self, sql: str, args) -> dict:
        """先按整句精确匹配（同名语句要按表给不同答复），再退到前缀匹配。"""
        script = self.script
        hit = script.get(sql)
        if hit is not None:
            return hit
        for prefix, reply in script.items():
            if sql.upper().startswith(prefix.upper()):
                return reply
        raise AssertionError(f"没有为这条语句准备脚本：{sql}")

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        if self.closed:
            raise pymysql.Error("Already closed")
        self.closed += 1

    def cursor(self, *args, **kwargs) -> "FakeCursor":
        return FakeCursor(self)


class Boom(FakeConn):
    def reply_for(self, sql: str, args) -> dict:
        raise pymysql.Error("no such table")


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.description: tuple = ()
        self._rows: list = []

    def execute(self, sql, args=None) -> None:  # noqa: ANN001 - 形参对齐 pymysql
        text = " ".join(sql.split())
        self.conn.executed.append((text, args))
        reply = self.conn.reply_for(text, args)
        self.description = reply.get("description", ())
        self._rows = reply.get("rows", [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        return None


DDL_ORDERS = "CREATE TABLE `orders` (`id` int NOT NULL, PRIMARY KEY (`id`))"
DDL_ITEMS = "CREATE TABLE `items` (`id` int NOT NULL, PRIMARY KEY (`id`))"
DDL_VIEW = "CREATE VIEW `v_orders` AS select `orders`.`id` AS `id` from `orders`"
TABLES_SQL = (
    "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_TYPE, TABLE_NAME LIMIT 401"
)
SCHEMA_SQL = (
    "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
    "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s"
)


def by_name_sql(names) -> str:
    """选表那条清单查询在假连接里看到的文本（占位符按名字个数展开）。"""
    return db_backup._TABLES_SQL_BY_NAME.format(placeholders=", ".join(["%s"] * len(names)))


def tables_script(rows, *, count=2, columns=("id",), pk=True, charset=("utf8mb4", "utf8mb4_general_ci"), selection=None):
    """一张基表 + 一张视图的通用脚本；``rows`` 是 information_schema 的表清单。

    ``selection`` 给了就按选表那条查询给脚本：整库的清单查询带 LIMIT，两者文本不同，
    必须分别命中，否则就测不出「选表时根本没跑整库那条」。
    """
    script: dict = {SCHEMA_SQL: {"rows": [charset] if charset else []}}
    list_sql = (
        by_name_sql(selection) if selection else TABLES_SQL
    )
    script[list_sql] = {"rows": rows}
    for name, _type in rows:
        ddl = DDL_VIEW if _type == "VIEW" else (DDL_ORDERS if name == "orders" else DDL_ITEMS)
        script[f"SHOW CREATE TABLE `shop`.`{name}`"] = {"rows": [(name, ddl)]}
        if _type == "VIEW":
            continue
        script[f"SELECT COUNT(*) FROM `shop`.`{name}`"] = {"rows": [(count,)]}
        script[f"SELECT * FROM `shop`.`{name}` LIMIT 0"] = {
            "description": tuple((c,) for c in columns)
        }
        script[f"SELECT COLUMN_NAME, COLUMN_KEY FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION"] = {
            "rows": [("id", "PRI") if pk else ("id", "")]
        }
    return script


@pytest.fixture
def connect(monkeypatch):
    def install(script, conn=None):
        conn = conn or FakeConn()
        conn.script = script
        monkeypatch.setattr(db_backup, "_connect_mysql", lambda *a, **k: conn)
        return conn

    return install


async def _collect(it) -> str:
    return "".join([chunk.decode("utf-8") async for chunk in it])


# ---- 备料阶段 -------------------------------------------------------------------


def test_prepare_pairs_ddl_with_a_data_plan_per_base_table(connect):
    conn = connect(tables_script([("orders", "BASE TABLE")]))
    session = db_backup._prepare_backup_sync(ITEM, "", "shop", False)  # type: ignore[arg-type]
    try:
        entry = session.entries[0]
        assert entry.ddl == DDL_ORDERS
        assert session.total_rows == 2
        # 取数计划复用导出的会话对象，同一把连接、带确定排序键。
        assert entry.data is not None and entry.data.conn is conn
        assert entry.data.head_sql == "SELECT * FROM `shop`.`orders` ORDER BY `id` ASC"
        # 逐表 COUNT(*) 在流开始之前跑完：上限判定不能等文件写了一半。
        executed = [sql for sql, _ in conn.executed]
        assert "SELECT COUNT(*) FROM `shop`.`orders`" in executed
    finally:
        session.close()


def test_prepare_keeps_view_definitions_but_skips_their_rows(connect):
    conn = connect(tables_script([("orders", "BASE TABLE"), ("v_orders", "VIEW")]))
    session = db_backup._prepare_backup_sync(ITEM, "", "shop", False)  # type: ignore[arg-type]
    try:
        # 基表排在视图前：还原视图时它依赖的表已经建好。
        assert [e.table for e in session.entries] == ["orders", "v_orders"]
        assert session.entries[1].ddl == DDL_VIEW
        assert session.entries[1].data is None
        assert not any(sql.startswith("SELECT COUNT(*) FROM `shop`.`v_orders`") for sql, _ in conn.executed)
    finally:
        session.close()


def test_structure_only_never_touches_the_data(connect):
    conn = connect(tables_script([("orders", "BASE TABLE")]))
    session = db_backup._prepare_backup_sync(ITEM, "", "shop", True)  # type: ignore[arg-type]
    try:
        assert session.structure_only is True
        assert session.entries[0].data is None
        assert session.total_rows == 0
        assert not any(sql.startswith("SELECT COUNT(*)") for sql, _ in conn.executed)
    finally:
        session.close()


def test_empty_schema_is_refused(connect):
    conn = connect({TABLES_SQL: {"rows": []}, SCHEMA_SQL: {"rows": [("utf8mb4", "utf8mb4_general_ci")]}})
    with pytest.raises(OpsDbError, match="没有表"):
        db_backup._prepare_backup_sync(ITEM, "", "shop", False)  # type: ignore[arg-type]
    # 备料阶段抛错时连接已经建起来了，不能把它留在那儿。
    assert conn.closed == 1


def test_too_many_tables_is_refused(connect):
    conn = connect({TABLES_SQL: {"rows": [(f"t{i}", "BASE TABLE") for i in range(401)]}, SCHEMA_SQL: {"rows": []}})
    with pytest.raises(OpsDbError, match="400 张表"):
        db_backup._prepare_backup_sync(ITEM, "", "shop", False)  # type: ignore[arg-type]
    assert conn.closed == 1


def test_row_cap_is_checked_while_counting(monkeypatch, connect):
    monkeypatch.setattr(get_settings(), "OPS_BACKUP_MAX_ROWS", 5)
    conn = connect(tables_script([("a", "BASE TABLE"), ("b", "BASE TABLE")], count=4))
    with pytest.raises(OpsDbError, match="超过上限"):
        db_backup._prepare_backup_sync(ITEM, "", "shop", False)  # type: ignore[arg-type]
    assert conn.closed == 1


def test_prepare_closes_the_connection_when_a_statement_fails(connect):
    conn = connect({}, Boom())
    with pytest.raises(pymysql.Error):
        db_backup._prepare_backup_sync(ITEM, "", "shop", False)  # type: ignore[arg-type]
    assert conn.closed == 1


# ---- 选表备份 -------------------------------------------------------------------


def test_selection_asks_for_the_named_tables_only(connect):
    rows = [("orders", "BASE TABLE"), ("items", "BASE TABLE")]
    conn = connect(tables_script(rows, selection=["orders", "items"]))
    session = db_backup._prepare_backup_sync(ITEM, "", "shop", False, ["orders", "items"])  # type: ignore[arg-type]
    try:
        assert [e.table for e in session.entries] == ["orders", "items"]
        assert session.selection == ["orders", "items"]
        executed = [sql for sql, _ in conn.executed]
        # 整库那条带 LIMIT 的清单查询一次都不该出现：千表库里挑两张，不该先被上限挡住。
        assert TABLES_SQL not in executed
        assert [a for s, a in conn.executed if s.startswith("SELECT TABLE_NAME")] == [
            ("shop", "orders", "items")
        ]
    finally:
        session.close()


def test_selection_refuses_names_that_are_not_in_the_schema(connect):
    conn = connect(tables_script([("orders", "BASE TABLE")], selection=["orders", "ghost"]))
    with pytest.raises(OpsDbError, match="ghost"):
        db_backup._prepare_backup_sync(ITEM, "", "shop", False, ["orders", "ghost"])  # type: ignore[arg-type]
    # 名字对不上就整份不发：宁可让用户重新勾，也不给一个悄悄少一张表的文件。
    assert conn.closed == 1


def test_selection_strips_blanks_and_duplicates(connect):
    conn = connect(tables_script([("orders", "BASE TABLE")], selection=["orders"]))
    session = db_backup._prepare_backup_sync(ITEM, "", "shop", False, [" orders ", "orders", ""])  # type: ignore[arg-type]
    try:
        assert session.selection == ["orders"]
    finally:
        session.close()
    assert [a for s, a in conn.executed if s.startswith("SELECT TABLE_NAME")] == [("shop", "orders")]


def test_empty_selection_backs_up_the_whole_schema(connect):
    conn = connect(tables_script([("orders", "BASE TABLE")]))
    session = db_backup._prepare_backup_sync(ITEM, "", "shop", False, [])  # type: ignore[arg-type]
    try:
        assert session.selection is None
        assert [e.table for e in session.entries] == ["orders"]
    finally:
        session.close()
    # 清空多选框、null、只有空白，三种都归到「整库」这一种表示。
    assert db_backup._normalize_selection(None) is None
    assert db_backup._normalize_selection([]) is None
    assert db_backup._normalize_selection(["", "  "]) is None


def test_selection_beyond_the_table_cap_is_refused(connect):
    names = [f"t{i}" for i in range(db_backup.TABLE_CAP + 1)]
    conn = connect({SCHEMA_SQL: {"rows": []}})
    with pytest.raises(OpsDbError, match="一次最多备份"):
        db_backup._prepare_backup_sync(ITEM, "", "shop", False, names)  # type: ignore[arg-type]
    assert conn.closed == 1


def test_row_cap_names_the_selection_when_scoped(monkeypatch, connect):
    monkeypatch.setattr(get_settings(), "OPS_BACKUP_MAX_ROWS", 5)
    conn = connect(
        tables_script([("orders", "BASE TABLE"), ("items", "BASE TABLE")], count=4, selection=["orders", "items"])
    )
    with pytest.raises(OpsDbError, match="选中的表累计"):
        db_backup._prepare_backup_sync(ITEM, "", "shop", False, ["orders", "items"])  # type: ignore[arg-type]
    assert conn.closed == 1


# ---- 文件写出 -------------------------------------------------------------------


def session_of(entries, structure_only=False, total_rows=0, conn=None, selection=None) -> db_backup._BackupSession:
    return db_backup._BackupSession(
        conn or FakeConn(),  # type: ignore[arg-type]
        "shop",
        structure_only,
        entries,
        total_rows,
        datetime(2026, 10, 16, 15, 4, 5),
        None,
        None,
        selection,
    )


def plan(total: int, conn=None, table: str = "orders") -> db_data._ExportSession:
    return db_data._ExportSession(
        conn or FakeConn(),  # type: ignore[arg-type]
        "shop",
        table,
        ["id", "name"],
        total,
        "SELECT * FROM `shop`.`orders` ORDER BY `id` ASC",
        None,
    )


@pytest.fixture
def batches(monkeypatch):
    recorded: list = []
    state = {"batches": []}

    def fake_fetch(conn, head_sql, args, offset):
        recorded.append((head_sql, offset))
        return state["batches"].pop(0) if state["batches"] else []

    monkeypatch.setattr(db_data, "_fetch_export_batch_sync", fake_fetch)

    def load(*groups):
        state["batches"] = [list(g) for g in groups]
        return recorded

    return load


def test_backup_file_is_header_then_ddl_then_inserts(batches):
    batches([(1, "ok"), (2, None)])
    out = asyncio.run(
        _collect(db_backup.stream_backup(session_of([db_backup._BackupEntry("orders", DDL_ORDERS, plan(2))], total_rows=2)))
    )
    assert "-- 库：shop" in out
    assert "-- 内容：结构 + 数据" in out
    assert "-- 时间：2026-10-16 15:04:05" in out
    # 整库的文件不写范围行，免得读的人以为少了什么。
    assert "选择：" not in out
    assert out.index("CREATE TABLE `orders`") < out.index("INSERT INTO")
    # DDL 原文照抄、只补分号；INSERT 与「导出全部」同款写法。
    assert f"{DDL_ORDERS};\n" in out
    # 建表语句里只有裸表名，切库那一句缺了就会把表建到执行者的默认库里。
    assert "USE `shop`;\n" in out
    assert out.endswith(
        "INSERT INTO `shop`.`orders` (`id`, `name`) VALUES\n(1, 'ok'),\n(2, NULL);\n\n"
        "SET FOREIGN_KEY_CHECKS = 1;\n"
    )


def test_selected_scope_is_written_in_the_file_header(batches):
    batches()
    entries = [db_backup._BackupEntry("orders", DDL_ORDERS, None)]
    out = asyncio.run(
        _collect(db_backup.stream_backup(session_of(entries, selection=["orders", "v_orders"])))
    )
    # 一份只有三张表的 .sql 与整库的长得一样，范围得写在头注释里，否则还原的人不知道少了几张。
    assert "-- 选择：orders、v_orders\n" in out
    assert "-- 表：1 张" in out


def test_dump_creates_the_database_with_its_own_charset(connect):
    conn = connect(tables_script([("orders", "BASE TABLE")]))
    session = db_backup._prepare_backup_sync(ITEM, "", "shop", True)  # type: ignore[arg-type]
    try:
        assert (session.charset, session.collation) == ("utf8mb4", "utf8mb4_general_ci")
        out = asyncio.run(_collect(db_backup.stream_backup(session)))
        assert (
            "CREATE DATABASE IF NOT EXISTS `shop` "
            "DEFAULT CHARACTER SET `utf8mb4` COLLATE `utf8mb4_general_ci`;\n"
        ) in out
    finally:
        session.close()


def test_dump_without_a_visible_schema_still_targets_the_database(connect):
    # 账号看不到 SCHEMATA 时不猜字符序，但 USE 照写——库名是准确的。
    conn = connect(tables_script([("orders", "BASE TABLE")], charset=None))
    session = db_backup._prepare_backup_sync(ITEM, "", "shop", True)  # type: ignore[arg-type]
    try:
        assert session.charset is None
        out = asyncio.run(_collect(db_backup.stream_backup(session)))
        assert "CREATE DATABASE" not in out
        assert "USE `shop`;" in out
    finally:
        session.close()


def test_backup_turns_foreign_key_checks_off_and_back_on(batches):
    batches()
    out = asyncio.run(_collect(db_backup.stream_backup(session_of([db_backup._BackupEntry("orders", DDL_ORDERS, None)]))))
    # 表按名字排，还原时外键两端未必按依赖顺序出现；头部关、尾部开。
    assert out.index("SET FOREIGN_KEY_CHECKS = 0;") < out.index("CREATE TABLE")
    assert out.endswith("SET FOREIGN_KEY_CHECKS = 1;\n")


def test_structure_only_writes_ddl_only(batches):
    batches()
    out = asyncio.run(
        _collect(db_backup.stream_backup(session_of([db_backup._BackupEntry("orders", DDL_ORDERS, None)], structure_only=True)))
    )
    assert "-- 内容：仅结构" in out
    assert "INSERT INTO" not in out


def test_views_follow_tables_in_the_file(batches):
    batches()
    entries = [
        db_backup._BackupEntry("orders", DDL_ORDERS, None),
        db_backup._BackupEntry("v_orders", DDL_VIEW, None),
    ]
    out = asyncio.run(_collect(db_backup.stream_backup(session_of(entries))))
    assert out.index("表 orders") < out.index("表 v_orders")
    assert DDL_VIEW in out


def test_connection_is_closed_when_the_client_walks_away(batches):
    batches([(1, "a")] * 5)
    # 整库备份只有一条连接：取数计划与会话共用它，收尾也归会话管。
    conn = FakeConn()
    stream = db_backup.stream_backup(
        session_of([db_backup._BackupEntry("orders", DDL_ORDERS, plan(50, conn))], conn=conn)
    )

    async def abort():
        first = await stream.__anext__()
        await stream.aclose()
        return first

    asyncio.run(abort())
    assert conn.closed == 1


# ---- 网盘那份的字节上限 ----------------------------------------------------------


def test_collect_concatenates_the_whole_file(batches, monkeypatch):
    batches([(1, "a"), (2, "b")])
    monkeypatch.setattr(get_settings(), "OPS_BACKUP_MAX_BYTES", 10 * 1024 * 1024)
    conn = FakeConn()
    session = session_of([db_backup._BackupEntry("orders", DDL_ORDERS, plan(2, conn))], conn=conn)
    raw = asyncio.run(db_backup.collect_backup(session))
    text = raw.decode("utf-8")
    assert text.startswith("-- YangVis 数据库备份")
    assert text.endswith("SET FOREIGN_KEY_CHECKS = 1;\n")
    # 攒完整份文件要收尾两次（流跑到头 + collect 的 finally），真实驱动下第二次
    # 会抛 Already closed——会话必须只认第一次，否则整个网盘分支就是 500。
    assert (conn.closed, conn.rolled_back) == (1, 1)


def test_session_close_is_idempotent():
    conn = FakeConn()
    session = session_of([], conn=conn)
    session.close()
    session.close()
    assert conn.closed == 1


def test_collect_refuses_a_dump_beyond_the_netdisk_limit(batches, monkeypatch):
    batches([(1, "a"), (2, "b")])
    monkeypatch.setattr(get_settings(), "OPS_BACKUP_MAX_BYTES", 32)
    conn = FakeConn()
    session = session_of([db_backup._BackupEntry("orders", DDL_ORDERS, plan(2, conn))], conn=conn)
    with pytest.raises(OpsDbError, match="网盘存不下"):
        asyncio.run(db_backup.collect_backup(session))
    # 抛在上限判定处，流停在半路，collect 的 finally 是唯一来得及的收尾。
    assert conn.closed == 1


# ---- 文件名 ----------------------------------------------------------------------


def test_filename_carries_schema_and_beijing_timestamp():
    assert db_backup.backup_filename(session_of([])) == "shop.20261016_150405.sql"
