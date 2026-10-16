"""整库或选表的备份：一个 schema（或其中挑出的几张表）的建表 DDL 与全量 INSERT，一次点完。

与「导出全部」的分工是表级与库级：那条吐的是屏幕上这套筛选条件命中的行，这一条
要的是库里的每一张表——或者用户挑出来的那几张。两者共用同一把只读会话、同一份
分批取数与同一种 INSERT
写法（:class:`db_data._ExportSession`），所以两处产出的语句文本不会分叉。

一致性靠 MySQL 的只读事务：连接开在 ``SET SESSION TRANSACTION READ ONLY``，
InnoDB 的可重复读让整库从头到尾看的是同一个快照——与 mysqldump
``--single-transaction`` 同级，不需要锁表。

产出的是一个 .sql 文本流：每张表先 ``SHOW CREATE TABLE`` 原文，再按批多行
INSERT。备份文件不带 ``DROP TABLE``——还原到已有同名表的库要先把表清掉，
这是刻意的：一个手滑就能删库的备份文件不该默认长成那样。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, List, Optional

import pymysql

from app.config import get_settings
from app.models.entities import OpsDatabase
from app.services.ops.database import OpsDbError
from app.services.ops.db_data import (
    _ExportSession,
    _stable_order_sync,
    _stream_insert,
)
from app.services.ops.db_exec import _connect_mysql, _password_of
from app.services.ops.db_schema import _quote_ident
from app.timeutil import db_now

logger = logging.getLogger("yangvis.ops_database")

# 一次备份最多带多少张表。整库那条清单查询靠它兜底：表多到几百张的库，逐表取定义
# 本身就会把这次请求拖到超时。选表时同样用它限制一次能点多少张。
TABLE_CAP = 400


@dataclass
class _BackupEntry:
    """一张表的备份现场：DDL 原文，以及（要数据时）它的取数会话。

    ``data`` 复用导出的 :class:`_ExportSession`，但它不拥有连接——整库备份只有
    一条会话，收尾由 :class:`_BackupSession` 统一负责。
    """

    table: str
    ddl: str
    data: Optional[_ExportSession]


class _BackupSession:
    """一次备份的现场：连接、库名、逐表条目与行数合计。"""

    def __init__(
        self,
        conn: pymysql.connections.Connection,
        schema: str,
        structure_only: bool,
        entries: List[_BackupEntry],
        total_rows: int,
        created_at: datetime,
        charset: Optional[str] = None,
        collation: Optional[str] = None,
        selection: Optional[List[str]] = None,
    ) -> None:
        self.conn = conn
        self.schema = schema
        self.structure_only = structure_only
        self.entries = entries
        self.total_rows = total_rows
        self.created_at = created_at
        self.charset = charset
        self.collation = collation
        # 用户挑出来的表名（已去空白去重）；None 是整库。文件头要把范围写清楚，
        # 否则一份只有三张表的 .sql 看起来像全库备份。
        self.selection = selection
        self._closed = False

    def close(self) -> None:
        """收尾。只认第一次：pymysql 的 ``close()`` 第二次直接抛 ``Already closed``，
        而下载流与网盘攒字节两处都会走到这里（后者还可能停在半路）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.rollback()
        except pymysql.Error:
            # 收尾失败不该盖住备份本身的结果；连接照关，事务留给服务端回收。
            logger.debug("backup session rollback failed", exc_info=True)
        finally:
            self.conn.close()


# 表清单按 TABLE_TYPE 排：BASE TABLE 天然在 VIEW 前面，还原视图时它依赖的表已经建好。
_TABLES_SQL = (
    "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_TYPE, TABLE_NAME LIMIT " + str(TABLE_CAP + 1)
)

# 选表时按名字直接点名查：整库那条清单查询带着 TABLE_CAP 的 LIMIT，一千张表的库里
# 只想备份其中三张，不该先被上限挡在门外。排序与整库那条一致，还原顺序不变。
_TABLES_SQL_BY_NAME = (
    "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ({placeholders}) "
    "ORDER BY TABLE_TYPE, TABLE_NAME"
)

# 建库语句要带上库自己的字符序，否则还原出来的库跟着服务端默认值走，
# 原本存得下的中文列可能直接报错。
_SCHEMA_CHARSET_SQL = (
    "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
    "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s"
)


def _table_ddl_sync(cur, schema: str, table: str) -> str:  # noqa: ANN001 - 形参是 pymysql 游标
    """一张表的定义原文。视图同形，返回的是 ``CREATE VIEW ...``。"""
    cur.execute(f"SHOW CREATE TABLE {_quote_ident(schema)}.{_quote_ident(table)}")
    row = cur.fetchone()
    if not row or len(row) < 2:
        raise OpsDbError(f"表 {schema}.{table} 不存在")
    return str(row[1])


def _normalize_selection(tables: Optional[List[str]]) -> Optional[List[str]]:
    """选表名单：去空白、按原顺序去重；一个都不剩时当作「整库」。

    前端的多选框清空和「故意整库」在载荷上长得一样（空数组 / null / [""]），
    这里一次收拢，后面只用 ``None`` 一种表示整库。
    """
    if not tables:
        return None
    picked: List[str] = []
    for raw in tables:
        name = str(raw or "").strip()
        if name and name not in picked:
            picked.append(name)
    return picked or None


def _list_backup_tables_sync(cur, schema: str, selection: Optional[List[str]]) -> List[tuple]:
    """这次要备份的表：(表名, 表类型)，基表在视图前。"""
    if selection is None:
        cur.execute(_TABLES_SQL, (schema,))
        rows = [(str(n), str(t)) for n, t in cur.fetchall()]
        if not rows:
            raise OpsDbError(f"库 {schema} 里没有表")
        if len(rows) > TABLE_CAP:
            raise OpsDbError(f"这个库有超过 {TABLE_CAP} 张表，请改为选表备份")
        return rows

    if len(selection) > TABLE_CAP:
        raise OpsDbError(f"一次最多备份 {TABLE_CAP} 张表，当前选了 {len(selection)} 张")
    placeholders = ", ".join(["%s"] * len(selection))
    cur.execute(_TABLES_SQL_BY_NAME.format(placeholders=placeholders), (schema, *selection))
    rows = [(str(n), str(t)) for n, t in cur.fetchall()]
    # 名字来自前端，可能是刚被别人删掉的表：宁可不备份，也不能静默少一张。
    missing = [name for name in selection if name not in {row[0] for row in rows}]
    if missing:
        raise OpsDbError(f"这些表不在库 {schema} 里：{'、'.join(missing)}")
    return rows


def _prepare_backup_sync(
    item: OpsDatabase,
    password: str,
    schema: str,
    structure_only: bool,
    tables: Optional[List[str]] = None,
) -> _BackupSession:
    """开只读会话，把每张表的 DDL 与取数计划备齐。

    行数在这一步逐表数好：上限要在响应开始之前判定，否则用户拿到的是一个
    悄悄少了几张表的半截文件。
    """
    conn = _connect_mysql(item, password)
    selection = _normalize_selection(tables)
    scope = "选中的表" if selection else "这个库"
    max_rows = get_settings().OPS_BACKUP_MAX_ROWS
    entries: List[_BackupEntry] = []
    total_rows = 0
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_CHARSET_SQL, (schema,))
            meta = cur.fetchone()
            charset = str(meta[0]) if meta and meta[0] else None
            collation = str(meta[1]) if meta and meta[1] else None

            tables_rows = _list_backup_tables_sync(cur, schema, selection)

            for name, table_type in tables_rows:
                ddl = _table_ddl_sync(cur, schema, name)
                # 视图的数据是从基表算出来的，把它 SELECT 出来存成 INSERT 既不是
                # 备份原件、还可能撞上不可更新视图；只留定义。
                if structure_only or str(table_type).upper() != "BASE TABLE":
                    entries.append(_BackupEntry(table=name, ddl=ddl, data=None))
                    continue

                target = f"{_quote_ident(schema)}.{_quote_ident(name)}"
                cur.execute(f"SELECT COUNT(*) FROM {target}")
                count = int(cur.fetchone()[0])
                total_rows += count
                if total_rows > max_rows:
                    raise OpsDbError(
                        f"{scope}累计 {total_rows} 行，超过上限 {max_rows} 行，请只备份结构或按表导出"
                    )
                # LIMIT 0 只为拿列名：空表也得有 INSERT 之外的列清单可依。
                cur.execute(f"SELECT * FROM {target} LIMIT 0")
                columns = [d[0] for d in (cur.description or [])]
                order_sql = _stable_order_sync(cur, schema, name, columns)
                plan = _ExportSession(
                    conn, schema, name, columns, count,
                    f"SELECT * FROM {target}{order_sql}", None,
                )
                entries.append(_BackupEntry(table=name, ddl=ddl, data=plan))
    except Exception:
        conn.close()
        raise
    return _BackupSession(
        conn, schema, structure_only, entries, total_rows, db_now(), charset, collation, selection
    )


async def open_backup(
    item: OpsDatabase,
    schema: str,
    structure_only: bool = False,
    tables: Optional[List[str]] = None,
) -> _BackupSession:
    """备份前的准备。连不上、库为空、表名对不上、超上限都在这一步抛，还能走正常 HTTP 报错。"""
    password = _password_of(item)
    try:
        return await asyncio.to_thread(
            _prepare_backup_sync, item, password, schema, structure_only, tables
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取备份数据失败：{exc}") from exc


def backup_filename(session: _BackupSession) -> str:
    """下载与网盘同用的文件名：库名 + 备份时刻到秒。"""
    return f"{session.schema}.{session.created_at.strftime('%Y%m%d_%H%M%S')}.sql"


async def _text_chunks(session: _BackupSession) -> AsyncIterator[str]:
    """备份文件的文本流：头注释 → 建库切库 → 逐表 DDL 与数据 → 尾注释。

    ``USE`` 那段不能省：建表语句里只有裸表名，缺了当前库就会跟着执行者客户端的
    默认库走，把表建到别的库里去。
    """
    create_db = ""
    if session.charset:
        q_schema = _quote_ident(session.schema)
        create_db = f"CREATE DATABASE IF NOT EXISTS {q_schema} DEFAULT CHARACTER SET {_quote_ident(session.charset)}"
        if session.collation:
            create_db += f" COLLATE {_quote_ident(session.collation)}"
        create_db += ";\n"
    # 选表备份出来的文件和整库的只差这一段注释与后面的正文，范围必须写在脸上。
    scoped = f"-- 选择：{'、'.join(session.selection)}\n" if session.selection else ""
    yield (
        f"-- YangVis 数据库备份\n"
        f"-- 库：{session.schema}\n"
        f"-- 时间：{session.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"-- 内容：{'仅结构' if session.structure_only else '结构 + 数据'}\n"
        f"{scoped}"
        f"-- 表：{len(session.entries)} 张\n\n"
        "SET NAMES utf8mb4;\n"
        f"{create_db}"
        f"USE {_quote_ident(session.schema)};\n"
        "SET FOREIGN_KEY_CHECKS = 0;\n\n"
    )
    for entry in session.entries:
        yield f"-- ----------------------------\n-- 表 {entry.table}\n-- ----------------------------\n"
        yield f"{entry.ddl};\n\n"
        if entry.data is None:
            continue
        async for statement in _stream_insert(entry.data):
            yield statement
    yield "SET FOREIGN_KEY_CHECKS = 1;\n"


async def stream_backup(session: _BackupSession) -> AsyncIterator[bytes]:
    """下载用的字节流：峰值内存是一批行，不是整库。

    客户端中途取消也会走 finally——连接挂在会话上，不收尾就漏一条。
    """
    try:
        async for text in _text_chunks(session):
            yield text.encode("utf-8")
    finally:
        session.close()


async def collect_backup(session: _BackupSession) -> bytes:
    """攒成一份完整字节，交给网盘那条只能吃 ``bytes`` 的上传接口。

    上限按 UTF-8 字节数算：网盘接口按 4MB 切片、单次请求 60 秒超时，一个几百 MB
    的备份在那头既不省时间也不省内存，所以这里宁可在传之前先拒掉。
    """
    limit = get_settings().OPS_BACKUP_MAX_BYTES
    parts: List[bytes] = []
    size = 0
    try:
        async for chunk in stream_backup(session):
            size += len(chunk)
            if size > limit:
                raise OpsDbError(
                    f"备份已超过 {limit // (1024 * 1024)}MB，网盘存不下这么大的文件，请改为仅备份结构"
                )
            parts.append(chunk)
    finally:
        # 流跑到头时它自己会收尾；超上限抛出时流还停在半路，这段才是唯一的收尾。
        # 两次都调没问题——_BackupSession.close() 只认第一次。
        session.close()
    return b"".join(parts)
