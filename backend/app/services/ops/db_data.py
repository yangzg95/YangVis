"""数据浏览、全量导出与行级写。

浏览（Navicat 式列表）：筛选操作符到 SQL 片段的映射是写死的 ``_FILTER_SQL``，
值一律走驱动参数化；列名不查元数据白名单，靠 ``_quote_ident`` 的反引号转义
兜住。手输的 ``where`` 拼进完整 SELECT 先过一次只读网关，长度另有 1024 字符
上限；分页上限 ``MAX_PAGE_SIZE`` 由执行通道给出。

导出：整表数据不进内存——同一把只读连接上按批 LIMIT/OFFSET 取
（``_EXPORT_BATCH``），边取边以 csv / json / markdown / INSERT 四种格式流出去。
用户没给排序时补一个确定排序键（主键，没主键退到第一列），否则同一行会在批
之间重复或整个漏掉。

行级改值与删除的语句全由服务端生成：列名先对 information_schema 核一遍真实
存在，定位按主键（没主键退化为全列 ``<=>`` 匹配，NULL 天然对得上）再带
LIMIT 1，执行走参数化、值永不进 SQL 文本；只有一条展示用语句按字面量转义，
用它过可写网关做兜底判定，并落审计。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import (
    Any,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

import pymysql

from app.config import get_settings
from app.models.entities import OpsDatabase
from app.models.schemas import (
    DbRowFilter,
    DbRowSort,
    DbRowWriteResult,
    DbRowsResult,
)
from app.services.ops.database import OpsDbError, OpsDbForbidden
from app.services.ops.db_exec import (
    MAX_PAGE_SIZE,
    _connect_mysql,
    _password_of,
    _stringify,
)
from app.services.ops.db_schema import _quote_ident
from app.services.ops.safety import classify_sql, classify_sql_writable

logger = logging.getLogger("yangvis.ops_database")

# 筛选操作符 → SQL 片段。%s 走驱动参数化，值永不进 SQL 文本。
_FILTER_SQL = {
    "eq": "= %s",
    "ne": "!= %s",
    "like": "LIKE %s",
    "not_like": "NOT LIKE %s",
    "lt": "< %s",
    "lte": "<= %s",
    "gt": "> %s",
    "gte": ">= %s",
    "is_null": "IS NULL",
    "is_not_null": "IS NOT NULL",
}


def _build_where(filters: Sequence[DbRowFilter]) -> Tuple[str, List[Any]]:
    """把筛选条件拼成参数化的 WHERE 子句（不含 WHERE 关键字）。"""
    clauses: List[str] = []
    params: List[Any] = []
    for f in filters:
        fragment = _FILTER_SQL.get(f.op)
        if fragment is None:
            raise OpsDbError(f"不支持的筛选操作：{f.op}")
        clauses.append(f"{_quote_ident(f.column)} {fragment}")
        if "%s" in fragment:
            value = f.value or ""
            params.append(f"%{value}%" if f.op in ("like", "not_like") else value)
    return " AND ".join(clauses), params


def _build_order_by(sorts: Sequence[DbRowSort]) -> str:
    if not sorts:
        return ""
    keys = ", ".join(
        f"{_quote_ident(s.column)} {'DESC' if s.direction == 'desc' else 'ASC'}" for s in sorts
    )
    return f" ORDER BY {keys}"


def _browse_clauses(
    filters: Sequence[DbRowFilter], sorts: Sequence[DbRowSort], where: str
) -> Tuple[str, List[Any], str]:
    """浏览与导出共用的 WHERE / ORDER BY 片段。

    两处必须给出同一套条件语义，否则「导出全部」和屏幕上这一页筛的就不是
    同一批行了。返回 ``(where_sql, params, order_sql)``。
    """
    structured, params = _build_where(filters)
    parts = [f"({where})"] if where else []
    if structured:
        parts.append(structured)
    where_sql = f" WHERE {' AND '.join(parts)}" if parts else ""
    return where_sql, params, _build_order_by(sorts)


def _browse_rows_sync(
    item: OpsDatabase,
    password: str,
    schema: str,
    table: str,
    page: int,
    page_size: int,
    filters: Sequence[DbRowFilter] = (),
    sorts: Sequence[DbRowSort] = (),
    where: str = "",
) -> Tuple[List[str], List[Sequence[Any]], int, str]:
    """同一连接里跑 COUNT(*) + 一页数据，保证分页总数与内容一致。

    返回值最后一项是数据 SELECT 的展示文本（参数已代回字面量），
    给前端底部的「执行的 SQL」栏用。
    """
    target = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    where_sql, params, order_sql = _browse_clauses(filters, sorts, where)
    # 空参数必须传 None 而不是空序列：pymysql 对非 None 参数会做一次 % 格式化，
    # 手输条件里的字面 %（如 LIKE '%张%'）会被误当占位符，直接抛 ValueError。
    args = tuple(params) or None
    data_sql = (
        f"SELECT * FROM {target}{where_sql}{order_sql} "
        f"LIMIT {page_size} OFFSET {(page - 1) * page_size}"
    )
    conn = _connect_mysql(item, password)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {target}{where_sql}", args)
            total = int(cur.fetchone()[0])
            cur.execute(data_sql, args)
            columns = [d[0] for d in (cur.description or [])]
            rows = list(cur.fetchall())
            # 展示用 SQL：mogrify 把参数代回字面量，即实际发往服务端的文本。
            display_sql = cur.mogrify(data_sql, args)
        conn.rollback()
    finally:
        conn.close()
    return columns, rows, total, display_sql


async def browse_rows(
    item: OpsDatabase,
    schema: str,
    table: str,
    page: int,
    page_size: int,
    filters: Sequence[DbRowFilter] = (),
    sorts: Sequence[DbRowSort] = (),
    where: str = "",
) -> DbRowsResult:
    """数据浏览的一页。

    ``where`` 是用户手输的原始条件片段，不能参数化，所以拼进完整 SELECT
    先过一遍只读网关（与查询页签同一套判定），多语句 / 写操作 / 危险函数
    在这里直接拒掉；连接层的 TRANSACTION READ ONLY 是兜底。
    """
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), MAX_PAGE_SIZE)
    where = (where or "").strip()
    if len(where) > 1024:
        raise OpsDbError("筛选条件太长（上限 1024 字符）")
    if where:
        target = f"{_quote_ident(schema)}.{_quote_ident(table)}"
        verdict = classify_sql(f"SELECT * FROM {target} WHERE ({where})")
        if not verdict.allowed:
            raise OpsDbError(f"筛选条件未通过安全检查：{verdict.reason}")

    password = _password_of(item)
    started = time.perf_counter()
    try:
        columns, rows, total, display_sql = await asyncio.to_thread(
            _browse_rows_sync, item, password, schema, table, page, page_size, filters, sorts, where
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取数据失败：{exc}") from exc
    elapsed = int((time.perf_counter() - started) * 1000)
    return DbRowsResult(
        columns=columns,
        rows=[[_stringify(v) for v in row] for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        elapsed_ms=elapsed,
        sql=display_sql,
    )


# ---- 全量导出（服务端流式） ------------------------------------------------------
#
# 浏览页一次最多 500 行，「导出全部」要的却是满足条件的每一行，不能让前端翻页
# 拼文件。这里在同一把只读连接上按批 LIMIT/OFFSET 取，取一批就吐一批：峰值内存
# 是一个批次，不是整张表；会话仍是 TRANSACTION READ ONLY，与浏览同一条保证。

# 每批取多少行。直接吃浏览的页大小，两处口径不会分叉。
_EXPORT_BATCH = MAX_PAGE_SIZE


class _ExportSession:
    """一次导出的现场：连接、列名、总行数、下一批的偏移。

    pymysql 的连接不是线程安全的，这里靠「同一时刻只有一个线程碰它」保证——
    异步生成器逐批 await，天然串行。
    """

    def __init__(
        self,
        conn: pymysql.connections.Connection,
        schema: str,
        table: str,
        columns: List[str],
        total: int,
        head_sql: str,
        args: Optional[Tuple[Any, ...]],
    ) -> None:
        self.conn = conn
        self.schema = schema
        self.table = table
        self.columns = columns
        self.total = total
        # 不含 LIMIT/OFFSET 的那半条 SELECT，每批只补上偏移。
        self.head_sql = head_sql
        self.args = args
        self.offset = 0

    def close(self) -> None:
        try:
            self.conn.rollback()
        except pymysql.Error:
            # 收尾失败不该盖住导出本身的结果；连接照关，事务留在服务端自行回收。
            logger.debug("export session rollback failed", exc_info=True)
        finally:
            self.conn.close()


def _stable_order_sync(cur: Any, schema: str, table: str, columns: Sequence[str]) -> str:
    """没排序时补一个确定的排序键。

    OFFSET 分页靠顺序说话：同一行在两批里都出现，文件就重复；都不出现，就漏行。
    钉主键，没主键钉第一列。
    """
    cur.execute(_COLUMNS_META_SQL, (schema, table))
    pk = [str(row[0]) for row in cur.fetchall() if str(row[1] or "").upper() == "PRI"]
    keys = pk or list(columns[:1])
    return _build_order_by([DbRowSort(column=name, direction="asc") for name in keys])


def _open_export_sync(
    item: OpsDatabase,
    password: str,
    schema: str,
    table: str,
    filters: Sequence[DbRowFilter],
    sorts: Sequence[DbRowSort],
    where: str,
) -> _ExportSession:
    """开只读会话，数好行数、取到列名，把连接原样交给导出流。"""
    target = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    where_sql, params, order_sql = _browse_clauses(filters, sorts, where)
    args = tuple(params) or None
    conn = _connect_mysql(item, password)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {target}{where_sql}", args)
            total = int(cur.fetchone()[0])
            # LIMIT 0 只为拿 description：列名要在第一批之前就有，空表也得有个表头。
            cur.execute(f"SELECT * FROM {target}{where_sql}{order_sql} LIMIT 0", args)
            columns = [d[0] for d in (cur.description or [])]
            if not order_sql:
                order_sql = _stable_order_sync(cur, schema, table, columns)
    except Exception:
        conn.close()
        raise
    return _ExportSession(
        conn, schema, table, columns, total, f"SELECT * FROM {target}{where_sql}{order_sql}", args
    )


async def open_export(
    item: OpsDatabase,
    schema: str,
    table: str,
    filters: Sequence[DbRowFilter] = (),
    sorts: Sequence[DbRowSort] = (),
    where: str = "",
) -> _ExportSession:
    """导出前的准备。条件校验、连不上、超上限都在这一步抛，还能走正常 HTTP 报错。

    ``where`` 的安全检查与 :func:`browse_rows` 同一套：手输片段拼进完整 SELECT
    过一遍只读网关，多语句 / 写操作 / 危险函数直接拒。
    """
    where = (where or "").strip()
    if len(where) > 1024:
        raise OpsDbError("筛选条件太长（上限 1024 字符）")
    if where:
        target = f"{_quote_ident(schema)}.{_quote_ident(table)}"
        verdict = classify_sql(f"SELECT * FROM {target} WHERE ({where})")
        if not verdict.allowed:
            raise OpsDbError(f"筛选条件未通过安全检查：{verdict.reason}")

    password = _password_of(item)
    try:
        session = await asyncio.to_thread(
            _open_export_sync, item, password, schema, table, filters, sorts, where
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取数据失败：{exc}") from exc

    max_rows = get_settings().OPS_EXPORT_MAX_ROWS
    if session.total > max_rows:
        session.close()
        raise OpsDbError(f"要导出 {session.total} 行，超过上限 {max_rows} 行，请先加筛选条件")
    return session


def _fetch_export_batch_sync(
    conn: pymysql.connections.Connection, head_sql: str, args: Optional[Tuple[Any, ...]], offset: int
) -> List[Sequence[Any]]:
    with conn.cursor() as cur:
        cur.execute(f"{head_sql} LIMIT {_EXPORT_BATCH} OFFSET {offset}", args)
        return list(cur.fetchall())


async def _export_batches(session: _ExportSession) -> AsyncIterator[List[Sequence[Any]]]:
    """一批一批地取，取满总数或取到短批就收。

    短批意味着没有下一批了：连接是只读事务，InnoDB 的可重复读让 COUNT(*) 和
    后面每批看的是同一个快照，一批不满额就说明快照里只剩这些行。
    """
    while session.offset < session.total:
        rows = await asyncio.to_thread(
            _fetch_export_batch_sync, session.conn, session.head_sql, session.args, session.offset
        )
        if not rows:
            return
        session.offset += len(rows)
        yield rows
        if len(rows) < _EXPORT_BATCH:
            return


# 出现这些字符就得整格加引号，否则分隔符会和内容里的同类字符混在一起。
_CSV_SPECIALS = (",", '"', "\n", "\r")


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(_stringify(value))
    if any(ch in text for ch in _CSV_SPECIALS):
        # 内部引号翻倍是 CSV 唯一的转义写法，前端 csvLine 同样如此。
        return '"' + text.replace('"', '""') + '"'
    return text


def _csv_line(values: Sequence[Any]) -> str:
    return ",".join(_csv_cell(v) for v in values)


def _md_cell(value: Any) -> str:
    """Markdown 单元格：竖线转义，换行折成 <br>，否则整行表格会被劈开。"""
    text = "" if value is None else str(_stringify(value))
    return text.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


async def _stream_csv(session: _ExportSession) -> AsyncIterator[str]:
    # BOM：Excel 只认它，缺了就把 UTF-8 按本地代码页解，中文全是乱码。
    yield "\ufeff" + _csv_line(session.columns) + "\r\n"
    async for rows in _export_batches(session):
        yield "".join(_csv_line(row) + "\r\n" for row in rows)


async def _stream_json(session: _ExportSession) -> AsyncIterator[str]:
    # 数组边界手写，而不是攒成 list 再 dumps：一次导出可能几十万行。
    yield "[\n"
    first = True
    async for rows in _export_batches(session):
        parts = []
        for row in rows:
            record = {name: _stringify(v) for name, v in zip(session.columns, row)}
            parts.append(("  " if first else ",\n  ") + json.dumps(record, ensure_ascii=False))
            first = False
        yield "".join(parts)
    yield "\n]"


async def _stream_markdown(session: _ExportSession) -> AsyncIterator[str]:
    yield _md_row(session.columns) + "\n"
    yield "| " + " | ".join("---" for _ in session.columns) + " |\n"
    async for rows in _export_batches(session):
        yield "".join(_md_row(row) + "\n" for row in rows)


def _md_row(values: Sequence[Any]) -> str:
    return "| " + " | ".join(_md_cell(v) for v in values) + " |"


async def _stream_insert(session: _ExportSession) -> AsyncIterator[str]:
    target = f"{_quote_ident(session.schema)}.{_quote_ident(session.table)}"
    cols = ", ".join(_quote_ident(c) for c in session.columns)
    async for rows in _export_batches(session):
        # 一批一条多行 INSERT：几十万行逐句写，文件大到没法在编辑器里翻开。
        values = ",\n".join(
            "(" + ", ".join(_display_literal(_stringify(v)) for v in row) + ")" for row in rows
        )
        yield f"INSERT INTO {target} ({cols}) VALUES\n{values};\n\n"


# 格式名 → 写入器。路由用同样的键做 pattern 校验与 media type 映射。
_EXPORT_STREAMERS: Dict[str, Any] = {
    "csv": _stream_csv,
    "json": _stream_json,
    "markdown": _stream_markdown,
    "insert": _stream_insert,
}


async def stream_export(session: _ExportSession, fmt: str) -> AsyncIterator[bytes]:
    """导出流：文本按格式写入器产出，出去的是 UTF-8 字节。

    客户端中途取消也会走 finally——连接挂在生成器上，不收尾就漏一条会话。
    """
    streamer = _EXPORT_STREAMERS.get(fmt)
    if streamer is None:
        raise OpsDbError(f"不支持的导出格式：{fmt}")
    try:
        async for text in streamer(session):
            yield text.encode("utf-8")
    finally:
        session.close()


# ---- 行级写（数据浏览页的改值 / 删除记录） --------------------------------------
#
# 与手敲控制台的差异只在 SQL 来源：控制台执行用户敲的原文，这里由服务端按
# 「列名 + 原值」生成单条 UPDATE/DELETE。值一律参数化，WHERE 一律带主键
# （没主键退化为全列 NULL 安全匹配）+ LIMIT 1——空 WHERE 的整表写在协议上
# 就不存在。writable × ops_write 双闸门在路由层，与控制台同一处判定。

# 只取列名和键类型两列：行定位要的是「哪些列真实存在、哪些是主键」。
_COLUMNS_META_SQL = (
    "SELECT COLUMN_NAME, COLUMN_KEY FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION"
)


def _display_literal(value: Any) -> str:
    """把值渲染成 MySQL 字面量，仅供审计展示、导出文件和网关兜底判定——
    本函数产出的文本从不直接交给连接执行，所以走的是最严的转义。

    转义按默认 sql_mode（反斜杠是转义符）来；``NO_BACKSLASH_ESCAPES`` 下
    ``\\n`` 会变成两个字符而不是换行，取回的语句值得瞄一眼再跑。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x00", "\\0")
    )
    return f"'{escaped}'"


def _build_row_write(
    verb: str,
    schema: str,
    table: str,
    key: Dict[str, Any],
    sets: Optional[Dict[str, Any]],
    columns_meta: List[Tuple[str, str]],
) -> Tuple[str, List[Any], str]:
    """拼行级写语句。返回 (参数化语句, 参数, 展示用语句)。

    ``key`` 是前端给回的整行原值：有主键时 WHERE 只取主键子集（浏览是
    SELECT *，主键列必然在 key 里），没主键时退化为全列 ``<=>`` 匹配，
    与 Navicat 的定位方式一致；NULL 值靠 ``<=>`` 天然匹配，不用特判。
    """
    target = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    real_columns = {name for name, _ in columns_meta}
    for source, label in ((key, "行定位"), (sets or {}, "修改")):
        unknown = [c for c in source if c not in real_columns]
        if unknown:
            raise OpsDbError(f"{label}包含表中不存在的列：{'、'.join(sorted(unknown))}")

    pk = [name for name, col_key in columns_meta if col_key.upper() == "PRI"]
    locator = pk or list(key.keys())
    missing = [c for c in locator if c not in key]
    if missing:
        raise OpsDbError(f"行定位缺少主键列：{'、'.join(missing)}")

    where_sql = " AND ".join(f"{_quote_ident(c)} <=> %s" for c in locator)
    where_display = " AND ".join(
        f"{_quote_ident(c)} <=> {_display_literal(key[c])}" for c in locator
    )
    params: List[Any] = [key[c] for c in locator]

    if verb == "delete":
        statement = f"DELETE FROM {target} WHERE {where_sql} LIMIT 1"
        display = f"DELETE FROM {target} WHERE {where_display} LIMIT 1"
    else:
        set_sql = ", ".join(f"{_quote_ident(c)} = %s" for c in sets or {})
        set_display = ", ".join(
            f"{_quote_ident(c)} = {_display_literal(v)}" for c, v in (sets or {}).items()
        )
        statement = f"UPDATE {target} SET {set_sql} WHERE {where_sql} LIMIT 1"
        display = f"UPDATE {target} SET {set_display} WHERE {where_display} LIMIT 1"
        params = list((sets or {}).values()) + params
    return statement, params, display


def _row_write_sync(
    item: OpsDatabase,
    password: str,
    schema: str,
    table: str,
    verb: str,
    key: Dict[str, Any],
    sets: Optional[Dict[str, Any]],
    holder: Dict[str, str],
) -> int:
    """在可写会话里生成并执行行级写语句，返回影响行数。

    展示 SQL 一过生成就放进 ``holder``——执行失败时路由层仍要拿它落审计。
    展示 SQL 同时过一遍可写网关兜底：生成语句必然通过，过不了说明生成
    逻辑本身出错了，拦下来比放行安全。执行用参数化版本，值永不进 SQL 文本。
    """
    conn = _connect_mysql(item, password, writable=True)
    try:
        with conn.cursor() as cur:
            cur.execute(_COLUMNS_META_SQL, (schema, table))
            meta = [(str(r[0]), str(r[1] or "")) for r in cur.fetchall()]
            if not meta:
                raise OpsDbError(f"表 {schema}.{table} 不存在或没有列")
            statement, params, display = _build_row_write(verb, schema, table, key, sets, meta)
            holder["display"] = display
            verdict = classify_sql_writable(display)
            if not verdict.allowed:
                raise OpsDbForbidden(f"生成的写语句未通过安全检查：{verdict.reason}")
            cur.execute(statement, params)
            affected = cur.rowcount
    finally:
        conn.close()
    if affected == 0:
        raise OpsDbError("未匹配到行，数据可能已被他人修改，请刷新后重试")
    return affected


async def _row_write(
    item: OpsDatabase,
    schema: str,
    table: str,
    verb: str,
    key: Dict[str, Any],
    sets: Optional[Dict[str, Any]],
    error_label: str,
) -> Tuple[str, DbRowWriteResult]:
    """行级写的公共异步包装。返回 (展示用 SQL, 结果)，展示 SQL 供路由落审计。

    失败路径上展示 SQL 挂在异常的 ``display`` 属性上——审计要记的就是
    这条生成语句，不能因为执行炸了就连它一起丢。
    """
    password = _password_of(item)
    holder: Dict[str, str] = {}
    started = time.perf_counter()
    try:
        affected = await asyncio.to_thread(
            _row_write_sync, item, password, schema, table, verb, key, sets, holder
        )
    except pymysql.Error as exc:
        err = OpsDbError(f"{error_label}：{exc}")
        err.display = holder.get("display", "")  # type: ignore[attr-defined]
        raise err from exc
    except (OpsDbError, OpsDbForbidden) as exc:
        exc.display = holder.get("display", "")  # type: ignore[attr-defined]
        raise
    elapsed = int((time.perf_counter() - started) * 1000)
    return holder.get("display", ""), DbRowWriteResult(affected=affected, elapsed_ms=elapsed)


async def update_row(
    item: OpsDatabase,
    schema: str,
    table: str,
    key: Dict[str, Any],
    sets: Dict[str, Any],
) -> Tuple[str, DbRowWriteResult]:
    """数据浏览页的行内改值。writable × ops_write 双闸门在路由层。"""
    return await _row_write(item, schema, table, "update", key, sets, "更新失败")


async def delete_row(
    item: OpsDatabase,
    schema: str,
    table: str,
    key: Dict[str, Any],
) -> Tuple[str, DbRowWriteResult]:
    """数据浏览页的删除记录，语义同 :func:`update_row`。"""
    return await _row_write(item, schema, table, "delete", key, None, "删除失败")
