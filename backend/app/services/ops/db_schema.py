"""元数据浏览：库 / 表 / 列 / 索引 / 外键 / DDL / ER 图 / 补全。

SQL 全部在服务端拼，用户能控制的只有标识符（库名 / 表名）和分页参数：标识符走
``_quote_ident`` 的反引号转义，值走驱动参数化，会话本身仍是只读——三层各管各的。
系统库排序时沉底，各类列表都带行数上限（``_BROWSE_CAP`` / ``_COMPLETION_CAP``），
ER 图另有表数与列数两道截断，超了就置 ``truncated`` 而不是悄悄少画。

Redis 连接展开的是逻辑库，库列表由 :mod:`app.services.ops.db_redis` 给出。
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

import pymysql

from app.models.entities import OpsDatabase
from app.models.schemas import (
    DatabaseType,
    DbColumnItem,
    DbCompletionColumn,
    DbCompletionTable,
    DbErd,
    DbErdColumn,
    DbErdRelation,
    DbErdTable,
    DbForeignKeyItem,
    DbIndexItem,
    DbSchemaItem,
    DbTableDdl,
    DbTableItem,
)
from app.services.ops.database import OpsDbError
from app.services.ops.db_exec import _password_of, _query_mysql
from app.services.ops.db_redis import _list_redis_dbs

# ---- 浏览（Navicat 式界面的数据源） -------------------------------------------
#
# 这一节的 SQL 全部在服务端拼，用户能控制的只有标识符（库名 / 表名）和分页参数。
# 标识符走反引号转义，值走参数化，会话本身仍是 READ ONLY——三层各管各的。

# 浏览类查询的行数上限。schema/table 列表一般不大，但不给上限就是给自己挖坑。
_BROWSE_CAP = 1000

# 补全元数据按「列」计数，大库一个 schema 几百张表乘几十列，上限得放宽些。
_COMPLETION_CAP = 20000



def _quote_ident(name: str) -> str:
    """反引号转义一个 MySQL 标识符。"""
    if not name or "\x00" in name:
        raise OpsDbError("非法的库名或表名")
    return "`" + name.replace("`", "``") + "`"


async def list_schemas(item: OpsDatabase) -> List[DbSchemaItem]:
    """连接展开的第一层：MySQL 列 schema，Redis 列逻辑库。"""
    if DatabaseType(item.db_type) is DatabaseType.REDIS:
        return await _list_redis_dbs(item)

    sql = (
        "SELECT s.SCHEMA_NAME, COUNT(t.TABLE_NAME) "
        "FROM information_schema.SCHEMATA s "
        "LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME "
        "GROUP BY s.SCHEMA_NAME "
        "ORDER BY s.SCHEMA_NAME IN ('information_schema','mysql','performance_schema','sys'), "
        "s.SCHEMA_NAME"
    )
    password = _password_of(item)
    try:
        _, rows, _, _ = await asyncio.to_thread(_query_mysql, item, password, sql, _BROWSE_CAP)
    except pymysql.Error as exc:
        raise OpsDbError(f"读取库列表失败：{exc}") from exc
    return [DbSchemaItem(name=str(r[0]), kind="schema", object_count=int(r[1])) for r in rows]


async def list_tables(item: OpsDatabase, schema: str) -> List[DbTableItem]:
    """一个 schema 下的表与视图。"""
    sql = (
        "SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS, TABLE_COMMENT "
        "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s "
        "ORDER BY TABLE_TYPE, TABLE_NAME"
    )
    password = _password_of(item)
    try:
        _, rows, _, _ = await asyncio.to_thread(
            _query_mysql, item, password, sql, _BROWSE_CAP, (schema,)
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取表列表失败：{exc}") from exc
    return [
        DbTableItem(
            name=str(r[0]),
            table_type=str(r[1] or "BASE TABLE"),
            engine=str(r[2]) if r[2] else None,
            rows_estimate=int(r[3]) if r[3] is not None else None,
            comment=str(r[4]) if r[4] else None,
        )
        for r in rows
    ]


async def list_columns(item: OpsDatabase, schema: str, table: str) -> List[DbColumnItem]:
    """一张表的列定义，按建表顺序。"""
    sql = (
        "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, "
        "EXTRA, COLUMN_COMMENT, ORDINAL_POSITION "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
        "ORDER BY ORDINAL_POSITION"
    )
    password = _password_of(item)
    try:
        _, rows, _, _ = await asyncio.to_thread(
            _query_mysql, item, password, sql, _BROWSE_CAP, (schema, table)
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取表结构失败：{exc}") from exc
    return [
        DbColumnItem(
            name=str(r[0]),
            column_type=str(r[1]),
            nullable=str(r[2]).upper() == "YES",
            column_key=str(r[3] or ""),
            default=None if r[4] is None else str(r[4]),
            extra=str(r[5] or ""),
            comment=str(r[6] or ""),
            position=int(r[7]),
        )
        for r in rows
    ]


async def list_completion_meta(item: OpsDatabase, schema: str) -> List[DbCompletionTable]:
    """查询编辑器的补全元数据：一个 schema 下所有表的列，一次聚合完。

    编辑器补全要的是「表 → 列」映射，逐表调 list_columns 是 N+1；
    这里直接扫 information_schema.COLUMNS 按表归拢。
    """
    sql = (
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION"
    )
    password = _password_of(item)
    try:
        _, rows, _, _ = await asyncio.to_thread(
            _query_mysql, item, password, sql, _COMPLETION_CAP, (schema,)
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取补全元数据失败：{exc}") from exc
    tables: dict[str, List[DbCompletionColumn]] = {}
    for table_name, column_name, column_type in rows:
        tables.setdefault(str(table_name), []).append(
            DbCompletionColumn(name=str(column_name), column_type=str(column_type or ""))
        )
    return [DbCompletionTable(name=name, columns=columns) for name, columns in tables.items()]


    return [DbCompletionTable(name=name, columns=columns) for name, columns in tables.items()]


# ---- 结构详情（DDL / 索引 / 外键 / ER 图） --------------------------------------
#
# DDL 走 SHOW CREATE TABLE：服务端已经把它拼成建表时的完整定义，我们不做二次
# 拼装，免得字符集、生成列、分区这些细节在翻译中丢失。其余三样读
# information_schema，与上面的浏览查询同一条只读会话。

# ER 图最多画多少张表。mermaid 的 erDiagram 没有虚拟化，上百个实体会直接把
# 画布糊成一团，所以宁可在服务端裁掉并如实标 truncated。
_ERD_MAX_TABLES = 40

# 一个实体最多显示几条属性。挑列时主键/唯一/索引列优先（它们才是连线两端）。
_ERD_MAX_COLUMNS = 12


async def table_ddl(item: OpsDatabase, schema: str, table: str) -> DbTableDdl:
    """一张表的建表 DDL 原文。

    ``SHOW CREATE TABLE`` 对视图返回的是 ``CREATE VIEW ...``，这里不区分：
    结构页签要的就是「这张表/视图在服务器上的定义」。
    """
    sql = f"SHOW CREATE TABLE {_quote_ident(schema)}.{_quote_ident(table)}"
    password = _password_of(item)
    try:
        # 第 2 列是语句本身（第 1 列是表名）；SHOW CREATE VIEW 同形。
        _, rows, _, _ = await asyncio.to_thread(_query_mysql, item, password, sql, 1)
    except pymysql.Error as exc:
        raise OpsDbError(f"读取建表语句失败：{exc}") from exc
    if not rows or len(rows[0]) < 2:
        raise OpsDbError(f"表 {schema}.{table} 不存在")
    return DbTableDdl(schema_name=schema, table=table, ddl=str(rows[0][1]))


async def list_indexes(item: OpsDatabase, schema: str, table: str) -> List[DbIndexItem]:
    """一张表的索引，联合索引按列序归拢。"""
    sql = (
        "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, CARDINALITY, "
        "INDEX_TYPE, COMMENT "
        "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
        "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
    )
    password = _password_of(item)
    try:
        _, rows, _, _ = await asyncio.to_thread(
            _query_mysql, item, password, sql, _BROWSE_CAP, (schema, table)
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取索引失败：{exc}") from exc

    grouped: Dict[str, DbIndexItem] = {}
    for row in rows:
        name = str(row[0])
        entry = grouped.get(name)
        if entry is None:
            entry = DbIndexItem(
                name=name,
                primary=name == "PRIMARY",
                unique=str(row[1]) in ("0", "False"),
                index_type=str(row[5] or ""),
                cardinality=int(row[4]) if row[4] is not None else None,
                comment=str(row[6] or ""),
            )
            grouped[name] = entry
        entry.columns.append(str(row[3]))
    # 主键排最前，其余按名字；和 Navicat 的索引列表顺序一致。
    return sorted(grouped.values(), key=lambda i: (not i.primary, i.name))


async def list_foreign_keys(item: OpsDatabase, schema: str, table: str) -> List[DbForeignKeyItem]:
    """一张表上的外键约束（联合外键按列序合并成一条）。"""
    sql = (
        "SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA, "
        "k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, "
        "IFNULL(r.UPDATE_RULE,''), IFNULL(r.DELETE_RULE,'') "
        "FROM information_schema.KEY_COLUMN_USAGE k "
        "LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
        "ON r.CONSTRAINT_SCHEMA = k.TABLE_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
        "WHERE k.TABLE_SCHEMA=%s AND k.TABLE_NAME=%s AND k.REFERENCED_TABLE_NAME IS NOT NULL "
        "ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION"
    )
    password = _password_of(item)
    try:
        _, rows, _, _ = await asyncio.to_thread(
            _query_mysql, item, password, sql, _BROWSE_CAP, (schema, table)
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取外键失败：{exc}") from exc

    grouped: Dict[str, DbForeignKeyItem] = {}
    for row in rows:
        name = str(row[0])
        entry = grouped.get(name)
        if entry is None:
            entry = DbForeignKeyItem(
                name=name,
                ref_schema=str(row[2] or ""),
                ref_table=str(row[3] or ""),
                on_update=str(row[5] or ""),
                on_delete=str(row[6] or ""),
            )
            grouped[name] = entry
        entry.columns.append(str(row[1]))
        entry.ref_columns.append(str(row[4] or ""))
    return sorted(grouped.values(), key=lambda f: f.name)


# 关系两端都取：TABLE_NAME 侧是「引用别人的人」，REFERENCED_TABLE_NAME 侧是「被引用的人」。
_ERD_RELATIONS_SQL = (
    "SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, "
    "REFERENCED_COLUMN_NAME "
    "FROM information_schema.KEY_COLUMN_USAGE "
    "WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME IS NOT NULL "
    "ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION"
)

# 选中表的列一次查完：逐表查是 N+1，40 张表就是 40 次往返。
_ERD_COLUMNS_SQL = (
    "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY "
    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
    "ORDER BY TABLE_NAME, ORDINAL_POSITION"
)


async def erd(item: OpsDatabase, schema: str) -> DbErd:
    """一个 schema 的实体关系数据：表、挑出来的属性、外键连线。

    表选取以「参与外键关系者优先」——孤立的宽表画出来只是一堆没有连线的方块，
    对理解模型没有帮助，超上限时先裁掉它们。
    """
    password = _password_of(item)
    try:
        _, rel_rows, _, _ = await asyncio.to_thread(
            _query_mysql, item, password, _ERD_RELATIONS_SQL, _BROWSE_CAP, (schema,)
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取外键关系失败：{exc}") from exc

    # 约束名 → 关系，联合外键的多列按位置并进同一对。
    edges: Dict[str, DbErdRelation] = {}
    involved: set[str] = set()
    for name, table_name, column_name, ref_table, ref_column in rel_rows:
        key = str(name)
        edge = edges.get(key)
        if edge is None:
            edge = DbErdRelation(
                name=key, from_table=str(table_name), to_table=str(ref_table)
            )
            edges[key] = edge
        edge.from_columns.append(str(column_name))
        edge.to_columns.append(str(ref_column or ""))
        involved.add(str(table_name))
        involved.add(str(ref_table))

    tables = await list_tables(item, schema)
    all_names = [t.name for t in tables]
    ordered = sorted(involved) + [n for n in all_names if n not in involved]
    selected = ordered[:_ERD_MAX_TABLES]
    truncated = len(ordered) > _ERD_MAX_TABLES
    in_selected = set(selected)

    # 裁掉指向未选中表的连线，否则前端拿到的图里有悬空实体。
    relations = [
        e for e in edges.values() if e.from_table in in_selected and e.to_table in in_selected
    ]

    try:
        _, col_rows, _, _ = await asyncio.to_thread(
            _query_mysql, item, password, _ERD_COLUMNS_SQL, _COMPLETION_CAP, (schema,)
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取表结构失败：{exc}") from exc

    raw_columns: Dict[str, List[DbErdColumn]] = {name: [] for name in selected}
    for table_name, column_name, column_type, column_key in col_rows:
        name = str(table_name)
        bucket = raw_columns.get(name)
        if bucket is not None:
            bucket.append(
                DbErdColumn(
                    name=str(column_name),
                    column_type=str(column_type or ""),
                    key=str(column_key or ""),
                )
            )

    type_by_name = {t.name: t.table_type for t in tables}
    table_items = []
    for name in selected:
        cols = raw_columns[name]
        # 有键的列（PRI/UNI/MUL）是连线的两端，先入图；剩下的按建表顺序补到上限。
        keyed_cols = [c for c in cols if c.key]
        plain_cols = [c for c in cols if not c.key]
        table_items.append(
            DbErdTable(
                name=name,
                table_type=type_by_name.get(name, "BASE TABLE"),
                columns=(keyed_cols + plain_cols)[:_ERD_MAX_COLUMNS],
            )
        )

    return DbErd(schema_name=schema, tables=table_items, relations=relations, truncated=truncated)
