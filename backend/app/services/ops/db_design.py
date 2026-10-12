"""设计表：把「期望定义」和 information_schema 的现状 diff 成 ALTER 语句。

``table_def`` 读回一张表的当前定义（列 / 主键 / 索引 / 注释），
``build_alter_preview`` 是纯函数，把它和前端提交的期望定义比出改名、增删、
类型与位置变化、主键更换、索引重建，产出按动作分组的语句；预览与执行是两次
请求，执行侧逐句再过一遍约束（见下方红线）。

列的类型串是唯一直接进 DDL 的用户文本，所以进来先过正则白名单；名字、注释、
默认值里的引号反斜杠分号一律拒绝——不赌连接的 sql_mode。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

import pymysql

from app.models.entities import OpsDatabase
from app.models.schemas import (
    DbAlterAction,
    DbAlterPreview,
    DbBatchResult,
    DbBatchStatement,
    DbColumnDef,
    DbColumnItem,
    DbIndexDef,
    DbIndexItem,
    DbTableDef,
    DbTableDefUpdate,
)
from app.services.ops.database import OpsDbError
from app.services.ops.db_data import _display_literal
from app.services.ops.db_exec import (
    _password_of,
    _preview_sql,
    _query_mysql,
    _run_mysql_batch,
)
from app.services.ops.db_schema import _quote_ident, list_columns, list_indexes
from app.services.ops.safety import classify_sql_writable
from app.timeutil import db_now

# ---- 设计表（表定义编辑） -------------------------------------------------------
#
# 三条红线，跟行级写同源：
# 1. 语句只由服务端按「期望定义 vs information_schema 现状」生成，值全部走
#    字面量转义、标识符全部反引号转义；
# 2. 每条生成语句再单独过一次可写网关；
# 3. 执行时逐句校验目标就是这张表——设计表页签改不动别的表。
#
# 预览与执行是两次请求，中间表结构可能被别人改过。这里不试图防住这种并发
# （防不住，也没有事务能防住 DDL），失败的那句会带着 MySQL 的原始错误返回，
# 与手敲控制台的批量执行同一套语义。

# 类型串是唯一直接进 DDL 的用户文本（标识符有反引号、值有引号兜着）。
# 收成「类型名 + 可选括号参数 + 可选符号修饰」，再叠加下面对 ; / -- / /* 的
# 一刀切拒绝，注入面就只剩「写一个 MySQL 不认的类型」——那是执行期的事。
_COLUMN_TYPE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*"
    r"(?:\([0-9A-Za-z_,.+()'\- ]{0,120}\))?"
    r"(?:[ ]+(?:unsigned|signed|zerofill))?$",
    re.IGNORECASE,
)

# 名字 / 注释 / 默认值是直接抄进 DDL 文本的用户内容。引号和反斜杠一并拒掉：
# 内联字面量只剩「反斜杠转义」这一层保护，而连接一旦开着 NO_BACKSLASH_ESCAPES，
# ``\'`` 就不是转义而是字符串结束——不赌会话配置，直接不给进来。
_DDL_TEXT_RE = re.compile(r"[\x00'\\;]|--|/\*|\*/")

_TIMESTAMP_DEFAULT_RE = re.compile(r"^current_timestamp(?:\([0-9]\))?$", re.IGNORECASE)


def _check_ddl_text(value: str, label: str) -> None:
    if _DDL_TEXT_RE.search(value):
        raise OpsDbError(f"{label}含有不能进 DDL 的字符（引号、反斜杠、分号或注释符）")


def _default_clause(value: Optional[str]) -> str:
    """DEFAULT 子句的值部分：CURRENT_TIMESTAMP 系列必须裸写，其余是字面量。"""
    if value is not None and _TIMESTAMP_DEFAULT_RE.match(value.strip()):
        return value.strip().upper()
    return _display_literal(value)


def _column_clause(col: DbColumnDef) -> str:
    """一条列定义（不含列名），形如 ``varchar(64) NOT NULL DEFAULT '' COMMENT 'x'``。"""
    _check_ddl_text(col.name, "列名")
    _check_ddl_text(col.column_type, "列类型")
    if not _COLUMN_TYPE_RE.match(col.column_type.strip()):
        raise OpsDbError(f"列类型「{col.column_type}」不是合法的 MySQL 类型写法")
    parts = [_quote_ident(col.name), col.column_type.strip()]
    parts.append("NULL" if col.nullable else "NOT NULL")
    if col.auto_increment:
        parts.append("AUTO_INCREMENT")
    if col.has_default:
        _check_ddl_text(col.default or "", "默认值")
        parts.append(f"DEFAULT {_default_clause(col.default)}")
    if col.on_update_current_timestamp:
        parts.append("ON UPDATE CURRENT_TIMESTAMP")
    if col.comment:
        _check_ddl_text(col.comment, "列注释")
        parts.append(f"COMMENT {_display_literal(col.comment)}")
    return " ".join(parts)


def _position_clause(prev: Optional[str]) -> str:
    return "FIRST" if prev is None else f"AFTER {_quote_ident(prev)}"


def _quoted_list(columns: Sequence[str]) -> str:
    return "(" + ", ".join(_quote_ident(c) for c in columns) + ")"


def _action(
    kind: str,
    target: str,
    action: str,
    note: str,
    destructive: bool = False,
) -> DbAlterAction:
    return DbAlterAction(
        kind=kind,  # type: ignore[arg-type]
        sql=f"ALTER TABLE {target} {action}",
        note=note,
        destructive=destructive,
    )


_TABLE_INFO_SQL = (
    "SELECT ENGINE, TABLE_COLLATION, TABLE_COMMENT FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s"
)


async def table_def(item: OpsDatabase, schema: str, table: str) -> DbTableDef:
    """一张表的当前定义（设计表页签的初值）。"""
    password = _password_of(item)
    try:
        _, rows, _, _ = await asyncio.to_thread(
            _query_mysql, item, password, _TABLE_INFO_SQL, 1, (schema, table)
        )
    except pymysql.Error as exc:
        raise OpsDbError(f"读取表信息失败：{exc}") from exc
    if not rows:
        raise OpsDbError(f"表 {schema}.{table} 不存在")
    engine, collation, comment = (str(rows[0][0] or ""), str(rows[0][1] or ""), str(rows[0][2] or ""))

    columns = await list_columns(item, schema, table)
    indexes = await list_indexes(item, schema, table)
    pk = next((i.columns for i in indexes if i.primary), [])
    return DbTableDef(
        schema_name=schema,
        table=table,
        engine=engine or None,
        collation=collation or None,
        comment=comment,
        columns=columns,
        primary_key=list(pk),
        indexes=[i for i in indexes if not i.primary],
    )


def _column_state(col: DbColumnItem) -> Dict[str, Any]:
    """把 information_schema 的一列折成可与期望定义对比的形态。

    ``has_default`` 这里只能按「COLUMN_DEFAULT 非空」推断：可空且没写 DEFAULT 的列，
    MySQL 在 information_schema 里同样是 NULL，两者区分不出来。所以用户在表单里
    勾上「默认值=NULL」而服务端读回「没有默认值」时，会多生成一句语义为空的
    MODIFY——比漏掉用户显式设置的默认值要安全。
    """
    extra = col.extra.upper()
    return {
        "column_type": col.column_type.strip(),
        "nullable": col.nullable,
        "auto_increment": "AUTO_INCREMENT" in extra,
        "on_update": "ON UPDATE" in extra,
        "has_default": col.default is not None,
        "default": col.default,
        "comment": col.comment,
    }


def _wanted_state(col: DbColumnDef) -> Dict[str, Any]:
    return {
        "column_type": col.column_type.strip(),
        "nullable": col.nullable,
        "auto_increment": col.auto_increment,
        "on_update": col.on_update_current_timestamp,
        # 表达式默认值（CURRENT_TIMESTAMP / (uuid())）读回来带括号或大小写差异，
        # 逐字比较会把「没改」误判成「改了」，所以默认值只在用户显式给值时才参与比较。
        "has_default": col.has_default,
        "default": col.default if col.has_default else None,
        "comment": col.comment,
    }


def _changed(cur: Dict[str, Any], want: DbColumnDef) -> bool:
    """列定义是否变了。

    没勾默认值时 ``default`` 不参与比较：表达式默认值（``CURRENT_TIMESTAMP`` /
    ``(uuid())``）读回来带括号和大小写差异，逐字比会把「没改」判成「改了」，
    白白重建一次表。
    """
    target = _wanted_state(want)
    if not target["has_default"]:
        return any(
            cur[key] != target[key]
            for key in ("column_type", "nullable", "auto_increment", "on_update", "comment")
        )
    return cur != target


def _source_name(col: DbColumnDef, current_by_name: Dict[str, DbColumnItem]) -> Optional[str]:
    """这列对应的当前列名：改名时是旧名，没改名时是同名；对不上就是新增列。"""
    source = col.origin_name or col.name
    return source if source in current_by_name else None


def _position_anchors(desired_survivors: List[str], current_survivors: List[str]) -> set[str]:
    """相对顺序没变的最长一串列，它们不需要重写位置。

    MySQL 改列顺序只能整表重建（``MODIFY ... AFTER`` 走 ALGORITHM=COPY），所以
    「谁挪了位置」要算最少：把期望顺序映射到当前顺序下标，取最长递增子序列，
    序列里的列原地不动，其余才各配一句改位置的 ALTER。交换相邻两列因此只生成
    一条语句，而不是两条。
    """
    if not desired_survivors:
        return set()
    index_in_current = {name: i for i, name in enumerate(current_survivors)}
    seq = [index_in_current[name] for name in desired_survivors]
    # O(n^2) 的 DP 足够：列数上限 512，这里要的是看得懂而不是快。
    length = [1] * len(seq)
    previous = [-1] * len(seq)
    for i in range(len(seq)):
        for j in range(i):
            if seq[j] < seq[i] and length[j] + 1 > length[i]:
                length[i] = length[j] + 1
                previous[i] = j
    anchors: set[str] = set()
    cursor = max(range(len(seq)), key=lambda i: length[i])
    while cursor >= 0:
        anchors.add(desired_survivors[cursor])
        cursor = previous[cursor]
    return anchors


def _diff_columns(
    target: str, current: List[DbColumnItem], desired: List[DbColumnDef]
) -> Tuple[List[DbAlterAction], List[str]]:
    current_by_name = {c.name: c for c in current}
    final_names: set[str] = set()
    desired_by_source: Dict[str, DbColumnDef] = {}
    for col in desired:
        _check_ddl_text(col.name, "列名")
        if col.name in final_names:
            raise OpsDbError(f"列名「{col.name}」重复")
        final_names.add(col.name)
        source = _source_name(col, current_by_name)
        if source is not None:
            desired_by_source[source] = col
        elif col.origin_name:
            raise OpsDbError(f"改名列「{col.origin_name}」在当前表里不存在")

    # 存活列按「改名后的新名」各排一遍当前顺序与期望顺序，位置判定只在它们之间比。
    survivors = [desired_by_source[c.name].name for c in current if c.name in desired_by_source]
    survivor_names = set(survivors)
    anchors = _position_anchors([c.name for c in desired if c.name in survivor_names], survivors)

    actions: List[DbAlterAction] = []
    warnings: List[str] = []
    for position, col in enumerate(desired):
        prev = desired[position - 1].name if position else None
        source = _source_name(col, current_by_name)
        if source is None:
            actions.append(
                _action(
                    "column-add",
                    target,
                    f"ADD COLUMN {_column_clause(col)} {_position_clause(prev)}",
                    "新增列",
                )
            )
            continue

        cur = current_by_name[source]
        renamed = source != col.name
        moved = col.name not in anchors
        changed = _changed(_column_state(cur), col)
        if not (changed or moved or renamed):
            continue
        if "GENERATED" in cur.extra.upper():
            raise OpsDbError(f"列「{source}」是生成列，设计表不支持修改它")

        clause = _column_clause(col)
        if source != col.name:
            kind, note, lead = "column-rename", "列改名，数据保留", f"CHANGE COLUMN {_quote_ident(source)}"
        else:
            kind, lead = "column-modify", "MODIFY COLUMN"
            note = "类型/约束变更" if changed else "调整列顺序"
        action = f"{lead} {clause}"
        if moved:
            action += f" {_position_clause(prev)}"
        actions.append(_action(kind, target, action, note))
        if changed and _column_state(cur)["column_type"] != col.column_type.strip():
            warnings.append(
                f"列「{col.name}」的类型从 {cur.column_type} 改为 {col.column_type}，"
                "超出新类型的数据会被截断或报错"
            )

    dropped = [c.name for c in current if c.name not in desired_by_source]
    for name in dropped:
        if "GENERATED" in (current_by_name[name].extra or "").upper():
            raise OpsDbError(f"列「{name}」是生成列，请到查询控制台手写 DDL 删除")
        actions.append(
            _action(
                "column-drop",
                target,
                f"DROP COLUMN {_quote_ident(name)}",
                "删除列会丢失该列全部数据",
                destructive=True,
            )
        )
    if dropped:
        warnings.append(f"将删除 {len(dropped)} 列，数据不可恢复：{'、'.join(dropped)}")
    return actions, warnings


def _diff_primary(
    target: str, current_pk: List[str], desired_pk: List[str], desired: List[DbColumnDef]
) -> Tuple[List[DbAlterAction], List[str]]:
    if [c.lower() for c in current_pk] == [c.lower() for c in desired_pk]:
        return [], []
    names = {c.name for c in desired}
    missing = [c for c in desired_pk if c not in names]
    if missing:
        raise OpsDbError(f"主键列不在列清单里：{'、'.join(missing)}")

    # 换主键必须一句做完：拆成先 DROP 再 ADD 会留下「没有主键」的中间状态，
    # 而自增列在没有键的表上会被 MySQL 直接拒绝。
    parts: List[str] = []
    warnings: List[str] = []
    if current_pk:
        parts.append("DROP PRIMARY KEY")
    if desired_pk:
        parts.append(f"ADD PRIMARY KEY {_quoted_list(desired_pk)}")
        if not current_pk:
            warnings.append(f"新建主键：{'、'.join(desired_pk)}")
    else:
        warnings.append("表将没有主键：数据浏览页只能退化为全列匹配定位行")
    if not desired_pk and any(c.auto_increment for c in desired):
        warnings.append("自增列要求表上有键，去掉主键后执行可能失败")
    warnings.append("主键变更需要重建表，耗时与锁行为取决于 MySQL 版本和存储引擎")
    return [_action("primary", target, ", ".join(parts), "调整主键")], warnings


def _diff_indexes(
    target: str,
    current: List[DbIndexItem],
    desired: List[DbIndexDef],
    column_names: set[str],
) -> Tuple[List[DbAlterAction], List[str]]:
    for item in desired:
        _check_ddl_text(item.name, "索引名")
        unknown = [c for c in item.columns if c not in column_names]
        if unknown:
            raise OpsDbError(f"索引「{item.name}」引用了不存在的列：{'、'.join(unknown)}")

    current_by_name = {i.name: i for i in current}
    desired_by_name = {i.name: i for i in desired}
    actions: List[DbAlterAction] = []
    for name in sorted(current_by_name.keys() - desired_by_name.keys()):
        actions.append(
            _action("index-drop", target, f"DROP INDEX {_quote_ident(name)}", "删除索引", True)
        )
    for name, item in desired_by_name.items():
        cur = current_by_name.get(name)
        unchanged = (
            cur is not None
            and cur.unique == item.unique
            and [c.lower() for c in cur.columns] == [c.lower() for c in item.columns]
        )
        if unchanged:
            continue
        if cur is not None:
            # 索引不能原地改列，只能先删后建，MySQL 会重建它。
            actions.append(
                _action("index-drop", target, f"DROP INDEX {_quote_ident(name)}", "索引定义变了，先删", True)
            )
        actions.append(
            _action(
                "index-add",
                target,
                f"{'ADD UNIQUE KEY' if item.unique else 'ADD KEY'} {_quote_ident(name)} {_quoted_list(item.columns)}",
                "重建索引" if cur is not None else "新建索引",
            )
        )
    return actions, (["索引重建在大表上会锁表一段时间"] if actions else [])


def build_alter_preview(
    schema: str, table: str, definition: DbTableDef, payload: DbTableDefUpdate
) -> DbAlterPreview:
    """按「当前定义 vs 期望定义」算出要执行的 ALTER 语句。纯函数，不碰数据库。"""
    target = f"{_quote_ident(schema)}.{_quote_ident(table)}"

    # 主键列必须 NOT NULL。与其在换主键那句里补一条 MODIFY，不如在期望定义里
    # 就收紧——后面的列 diff 自然会把 NOT NULL 带上，新增列也一并生效。
    columns = [
        col.model_copy(update={"nullable": False}) if col.name in payload.primary_key else col
        for col in payload.columns
    ]

    actions: List[DbAlterAction] = []
    warnings: List[str] = []
    for part, part_warnings in (
        _diff_columns(target, definition.columns, columns),
        _diff_primary(target, definition.primary_key, payload.primary_key, columns),
        # 索引要能引用刚改名出去的列，所以列 diff 先跑，拿最终列名集合再校验索引。
        _diff_indexes(target, definition.indexes, payload.indexes, {c.name for c in columns}),
    ):
        actions += part
        warnings += part_warnings

    if (payload.comment or "") != (definition.comment or ""):
        _check_ddl_text(payload.comment, "表注释")
        actions.append(
            _action("comment", target, f"COMMENT = {_display_literal(payload.comment)}", "改表注释")
        )

    return DbAlterPreview(actions=actions, warnings=list(dict.fromkeys(warnings)))


async def preview_alter(
    item: OpsDatabase, schema: str, table: str, payload: DbTableDefUpdate
) -> DbAlterPreview:
    return build_alter_preview(schema, table, await table_def(item, schema, table), payload)


async def apply_alter(
    item: OpsDatabase, schema: str, table: str, statements: Sequence[str]
) -> Tuple[DbBatchResult, bool]:
    """执行设计表预览产出的语句：逐句过网关、逐句执行，一句失败不中断。

    返回 (结果, 是否有语句被安全网关拦下)，口径与 :func:`execute_batch` 一致；
    被拒的语句不占数据库连接，直接落成 error 行。
    """
    target = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    started_at = db_now()
    started = time.perf_counter()

    results: List[Optional[DbBatchStatement]] = [None] * len(statements)
    allowed: List[Tuple[int, str]] = []
    any_forbidden = False
    for i, raw in enumerate(statements):
        sql = raw.strip().rstrip(";")
        reason = ""
        if not sql:
            reason = "语句为空"
        elif not sql.startswith(f"ALTER TABLE {target} "):
            # 设计表页签只改得动它打开的那张表。语句本该是预览原样回传的，
            # 途中被人换成别的表就在这里拦下——比对用生成时的精确前缀。
            reason = f"设计表只能修改 {schema}.{table}"
        else:
            verdict = classify_sql_writable(sql)
            if not verdict.allowed:
                reason = verdict.reason or "该语句被安全策略拒绝"
        if reason:
            any_forbidden = True
            results[i] = DbBatchStatement(
                index=i + 1, sql=_preview_sql(sql), status="error", message=reason
            )
        else:
            allowed.append((i, sql))

    if allowed:
        password = _password_of(item)
        try:
            executed = await asyncio.to_thread(
                _run_mysql_batch,
                item,
                password,
                [sql for _, sql in allowed],
                schema,
                True,
            )
        except pymysql.Error as exc:
            raise OpsDbError(f"连接数据库失败：{exc}") from exc
        # 逐句成败回填原位置；被网关拒掉的句子保住它的拒绝原因。
        for (i, sql), result in zip(allowed, executed):
            elapsed, error = result[4], result[5]
            results[i] = DbBatchStatement(
                index=i + 1,
                sql=_preview_sql(sql),
                status="error" if error else "ok",
                message=error or "执行完成",
                elapsed_ms=elapsed,
            )

    settled = [r for r in results if r is not None]
    succeeded = sum(1 for r in settled if r.status == "ok")
    return (
        DbBatchResult(
            statements=settled,
            total=len(settled),
            succeeded=succeeded,
            failed=len(settled) - succeeded,
            started_at=started_at,
            finished_at=db_now(),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        ),
        any_forbidden,
    )
