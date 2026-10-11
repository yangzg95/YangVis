"""运维数据库的连接与执行通道。

「默认只读」这句约束不靠命令解析兜底。解析（:mod:`app.services.ops_safety`）只是
第一道门，真正的保证在连接本身：MySQL 会话开在 ``READ ONLY`` 事务里，并带上
``MAX_EXECUTION_TIME``——即便解析被绕过，服务端自己会拒绝写入。Redis 没有只读
会话这种东西，因此走白名单 + 硬禁用清单，并且不允许任何脚本类命令。

连接台账上的 ``writable`` 开关只对手敲控制台生效：打开后会话不再 READ ONLY、
网关换用可写判定（DML/DDL 放行，高危函数与账户级操作仍拦）。元数据浏览和
AI 通道永远走只读路径，与开关无关。

本模块是这一层的最底下：``_connect_mysql`` / ``_query_mysql`` / ``_connect_redis``
这些原语、单句与批量执行、以及取台账密码的 ``_password_of`` 都在这里，
结构浏览 / 数据浏览 / 设计表 / Redis 键四个模块统统往上接，它自己只依赖台账模块。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import (
    Any,
    List,
    Optional,
    Sequence,
    Tuple,
)

import pymysql

from app.config import get_settings
from app.crypto import DecryptionError, decrypt
from app.models.entities import OpsDatabase
from app.models.schemas import (
    DatabaseType,
    DbBatchResult,
    DbBatchStatement,
    DbExecuteResult,
)
from app.services.ops_database import OpsDbError, OpsDbForbidden
from app.services.ops_safety import (
    Verdict,
    classify_redis,
    classify_redis_writable,
    classify_sql,
    classify_sql_writable,
    ensure_limit,
    split_sql_script,
)
from app.timeutil import db_now

logger = logging.getLogger("yangvis.ops_database")

# ---- 执行 -------------------------------------------------------------------


def _password_of(item: OpsDatabase) -> str:
    try:
        return decrypt(item.password_enc)
    except DecryptionError as exc:
        raise OpsDbError(str(exc)) from exc


def _connect_mysql(
    item: OpsDatabase,
    password: str,
    database: Optional[str] = None,
    writable: bool = False,
) -> pymysql.connections.Connection:
    """建立一条 MySQL 会话，默认带只读保证。

    所有 MySQL 通道——控制台、元数据查询、数据浏览——都必须从这里出去。
    只读保证在 ``SET SESSION TRANSACTION READ ONLY`` 这一行上，而不是在
    SQL 解析上；``writable=True``（连接开了写开关的手敲控制台）跳过这行，
    并打开 autocommit——批量执行的语义是逐句独立，一句出错不影响其他句
    落盘，和 Navicat 的批量执行一致。

    ``database`` 是本次会话的默认库；缺省回落到台账里的 ``db_name``。
    """
    settings = get_settings()
    try:
        conn = pymysql.connect(
            host=item.host,
            port=item.port,
            user=item.username or "",
            password=password,
            database=database or item.db_name or None,
            connect_timeout=int(settings.OPS_SSH_TIMEOUT),
            read_timeout=int(settings.OPS_CMD_TIMEOUT),
            write_timeout=int(settings.OPS_CMD_TIMEOUT),
            charset="utf8mb4",
            autocommit=writable,
        )
    except pymysql.Error as exc:
        # 连接失败值得排查（网络/凭据/实例挂了）；查询失败高频，走 BusinessError 链不记。
        logger.warning(
            "mysql connection to database %s (%s:%s) failed: %s",
            item.id,
            item.host,
            item.port,
            exc,
        )
        raise
    try:
        with conn.cursor() as cur:
            if not writable:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            try:
                cur.execute("SET SESSION MAX_EXECUTION_TIME=%s", (int(settings.OPS_CMD_TIMEOUT * 1000),))
            except pymysql.Error:
                # MariaDB 用的是 max_statement_time（单位为秒），语法不同。
                try:
                    cur.execute("SET SESSION max_statement_time=%s", (settings.OPS_CMD_TIMEOUT,))
                except pymysql.Error:
                    logger.debug("target does not support a statement timeout knob")
    except Exception:
        conn.close()
        raise
    return conn


def _query_mysql(
    item: OpsDatabase,
    password: str,
    sql: str,
    cap: int,
    args: Optional[Sequence[Any]] = None,
    database: Optional[str] = None,
    writable: bool = False,
) -> Tuple[List[str], List[Sequence[Any]], bool, int]:
    """同步执行一条 MySQL 语句，最多取 ``cap`` 行。由调用方丢进线程池。

    返回 (columns, rows, truncated, affected)：``affected`` 是游标的
    rowcount，DML 的「影响行数」从这里来（SELECT 时语义是结果行数，没人读它）。
    """
    conn = _connect_mysql(item, password, database, writable=writable)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            columns = [d[0] for d in (cur.description or [])]
            rows = list(cur.fetchmany(cap + 1)) if columns else []
            affected = cur.rowcount
        conn.rollback()
    finally:
        conn.close()

    truncated = len(rows) > cap
    return columns, rows[:cap], truncated, affected


def _run_mysql(
    item: OpsDatabase,
    password: str,
    sql: str,
    database: Optional[str] = None,
    writable: bool = False,
) -> Tuple[List[str], List[Sequence[Any]], bool, int]:
    """控制台通道：行数上限吃全局配置。"""
    return _query_mysql(
        item, password, sql, get_settings().OPS_SQL_ROW_LIMIT,
        database=database, writable=writable,
    )


def _run_mysql_batch(
    item: OpsDatabase,
    password: str,
    statements: Sequence[str],
    database: Optional[str] = None,
    writable: bool = False,
) -> List[Tuple[List[str], List[Sequence[Any]], bool, int, int, Optional[str]]]:
    """同一条会话里逐句执行。每句返回 (columns, rows, truncated, affected, elapsed_ms, error)。

    单句出错不中断：异常文本放进 error 继续下一句——让用户一次看全所有
    失败比 fail-fast 更有用（Navicat 的批量执行同样是 continue-on-error）。
    可写会话是 autocommit 的，成功的语句各自落盘，失败的语句不影响它们。
    由调用方丢进线程池。
    """
    cap = get_settings().OPS_SQL_ROW_LIMIT
    conn = _connect_mysql(item, password, database, writable=writable)
    results: List[Tuple[List[str], List[Sequence[Any]], bool, int, int, Optional[str]]] = []
    try:
        with conn.cursor() as cur:
            for sql in statements:
                started = time.perf_counter()
                try:
                    cur.execute(sql)
                    columns = [d[0] for d in (cur.description or [])]
                    rows = list(cur.fetchmany(cap + 1)) if columns else []
                    results.append(
                        (columns, rows[:cap], len(rows) > cap, cur.rowcount,
                         int((time.perf_counter() - started) * 1000), None)
                    )
                except pymysql.Error as exc:
                    results.append(
                        ([], [], False, 0,
                         int((time.perf_counter() - started) * 1000), str(exc))
                    )
        conn.rollback()
    finally:
        conn.close()
    return results


def _stringify(value: Any) -> Any:
    """把驱动返回的值收敛成 JSON 能表达的形态。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def execute(
    item: OpsDatabase,
    command: str,
    schema: Optional[str] = None,
    *,
    allow_write: bool = False,
) -> DbExecuteResult:
    """执行一条命令。被安全策略挡下时抛 :class:`OpsDbForbidden`。

    可写需要两端同时成立：调用方是人手敲的控制台（``allow_write=True``）
    *并且*连接开了「允许写入」。AI 通道不传 ``allow_write``，即使连接开了
    写开关，AI 走的仍是只读判定 + 只读会话。

    ``schema`` 是 MySQL 控制台的默认库（查询界面上的库选择器），Redis 忽略。
    """
    db_type = DatabaseType(item.db_type)
    writable = allow_write and bool(item.writable)
    if db_type is DatabaseType.MYSQL:
        verdict: Verdict = (
            classify_sql_writable(command) if writable else classify_sql(command)
        )
    else:
        verdict = classify_redis_writable(command) if writable else classify_redis(command)
    if not verdict.allowed:
        raise OpsDbForbidden(verdict.reason or "该命令被安全策略拒绝")

    password = _password_of(item)
    started = time.perf_counter()

    if db_type is DatabaseType.MYSQL:
        sql = ensure_limit(command.strip().rstrip(";"), get_settings().OPS_SQL_ROW_LIMIT)
        try:
            columns, rows, truncated, affected = await asyncio.to_thread(
                _run_mysql, item, password, sql, schema, writable
            )
        except pymysql.Error as exc:
            raise OpsDbError(f"执行失败：{exc}") from exc
        elapsed = int((time.perf_counter() - started) * 1000)
        return DbExecuteResult(
            columns=columns,
            rows=[[_stringify(v) for v in row] for row in rows],
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed,
            text=None if columns else f"执行完成，影响 {affected} 行",
        )

    return await _execute_redis(item, password, command, started)


# ---- 批量执行（Navicat 式：整段脚本逐句跑） --------------------------------------

# 单批次的语句数上限：防止把整份迁移脚本粘进来打满执行线程。
_BATCH_MAX_STATEMENTS = 100

# 消息列表里的 SQL 预览长度：一眼认出是哪条就够，完整脚本在审计记录里。
_SQL_PREVIEW_LEN = 200


def _preview_sql(sql: str) -> str:
    """消息列表用的单行预览：压掉换行和多余空白，超长截断。"""
    text = " ".join(sql.split())
    return text if len(text) <= _SQL_PREVIEW_LEN else text[:_SQL_PREVIEW_LEN] + "…"


async def execute_batch(
    item: OpsDatabase,
    command: str,
    schema: Optional[str] = None,
    *,
    allow_write: bool = False,
) -> Tuple[DbBatchResult, bool]:
    """把一段脚本拆成语句逐句执行（仅 MySQL；Redis 没有「语句」概念）。

    ``allow_write`` 与连接的写开关同时成立时按可写判定放行 DML/DDL，并在
    可写会话里执行（口径与 :func:`execute` 相同）。
    返回 (结果, 是否有语句被安全网关拦下)——后者只给路由层记审计 verdict 用。
    """
    if DatabaseType(item.db_type) is not DatabaseType.MYSQL:
        raise OpsDbError("Redis 控制台不支持批量执行")

    statements = split_sql_script(command)
    if not statements:
        raise OpsDbError("没有可执行的语句")
    if len(statements) > _BATCH_MAX_STATEMENTS:
        raise OpsDbError(f"一次最多执行 {_BATCH_MAX_STATEMENTS} 条语句")

    started_at = db_now()
    started = time.perf_counter()

    # 先整体过一遍安全网关：被拒的语句不进数据库连接，直接落成 error 占位；
    # 放行的语句补上行数上限——连接里只会出现网关放行的 SQL，纵深防御更整齐。
    writable = allow_write and bool(item.writable)
    gate = classify_sql_writable if writable else classify_sql
    results: List[Optional[DbBatchStatement]] = [None] * len(statements)
    allowed: List[Tuple[int, str]] = []
    any_forbidden = False
    for i, stmt in enumerate(statements):
        verdict = gate(stmt)
        if verdict.allowed:
            allowed.append((i, stmt))
        else:
            any_forbidden = True
            results[i] = DbBatchStatement(
                index=i + 1,
                sql=_preview_sql(stmt),
                status="error",
                message=verdict.reason or "该语句被安全策略拒绝",
            )

    if allowed:
        limit = get_settings().OPS_SQL_ROW_LIMIT
        capped = [ensure_limit(stmt, limit) for _, stmt in allowed]
        password = _password_of(item)
        try:
            executed = await asyncio.to_thread(
                _run_mysql_batch, item, password, capped, schema, writable
            )
        except pymysql.Error as exc:
            raise OpsDbError(f"连接数据库失败：{exc}") from exc
        for (i, stmt), (columns, rows, truncated, affected, elapsed, error) in zip(allowed, executed):
            if error is not None:
                results[i] = DbBatchStatement(
                    index=i + 1,
                    sql=_preview_sql(stmt),
                    status="error",
                    message=error,
                    elapsed_ms=elapsed,
                )
            else:
                done_text = f"执行完成，影响 {affected} 行"
                results[i] = DbBatchStatement(
                    index=i + 1,
                    sql=_preview_sql(stmt),
                    status="ok",
                    message=f"{len(rows)} 行" if columns else done_text,
                    elapsed_ms=elapsed,
                    columns=columns,
                    rows=[[_stringify(v) for v in row] for row in rows],
                    row_count=len(rows),
                    truncated=truncated,
                    text=None if columns else done_text,
                )

    finished_at = db_now()
    settled = [r for r in results if r is not None]
    succeeded = sum(1 for r in settled if r.status == "ok")
    return (
        DbBatchResult(
            statements=settled,
            total=len(settled),
            succeeded=succeeded,
            failed=len(settled) - succeeded,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        ),
        any_forbidden,
    )


def _default_redis_db(item: OpsDatabase) -> int:
    """台账里 db_name 字段对 Redis 而言是库序号（"0"）。"""
    return int(item.db_name) if (item.db_name or "").isdigit() else 0


def _connect_redis(item: OpsDatabase, password: str, db: Optional[int] = None):
    """建立一条 Redis 客户端连接。调用方负责 ``await client.aclose()``。"""
    import redis.asyncio as aioredis

    settings = get_settings()
    return aioredis.Redis(
        host=item.host,
        port=item.port,
        username=item.username or None,
        password=password or None,
        db=_default_redis_db(item) if db is None else db,
        socket_connect_timeout=settings.OPS_SSH_TIMEOUT,
        socket_timeout=settings.OPS_CMD_TIMEOUT,
        decode_responses=True,
    )


async def _execute_redis(
    item: OpsDatabase, password: str, command: str, started: float
) -> DbExecuteResult:
    from redis.exceptions import RedisError

    settings = get_settings()
    parts = command.split()
    client = _connect_redis(item, password)
    try:
        raw = await client.execute_command(*parts)
    except RedisError as exc:
        raise OpsDbError(f"执行失败：{exc}") from exc
    finally:
        await client.aclose()

    elapsed = int((time.perf_counter() - started) * 1000)
    return _redis_result(raw, elapsed, settings.OPS_SQL_ROW_LIMIT)


def _redis_result(raw: Any, elapsed_ms: int, limit: int) -> DbExecuteResult:
    """把 Redis 千奇百怪的回复形态压成统一结构。"""
    if isinstance(raw, dict):
        rows = [[str(k), _stringify(v)] for k, v in list(raw.items())[:limit]]
        return DbExecuteResult(
            columns=["field", "value"],
            rows=rows,
            row_count=len(rows),
            truncated=len(raw) > limit,
            elapsed_ms=elapsed_ms,
        )

    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
        rows = [[_stringify(v)] for v in values[:limit]]
        return DbExecuteResult(
            columns=["value"],
            rows=rows,
            row_count=len(rows),
            truncated=len(values) > limit,
            elapsed_ms=elapsed_ms,
        )

    text = "" if raw is None else str(raw)
    if len(text) > get_settings().OPS_OUTPUT_LIMIT:
        text = text[: get_settings().OPS_OUTPUT_LIMIT] + "\n…（已截断）"
    return DbExecuteResult(text=text or "(nil)", elapsed_ms=elapsed_ms)


# 数据浏览的页大小上限。
MAX_PAGE_SIZE = 500


# ---- 连通性测试 ---------------------------------------------------------------


async def test_connection(item: OpsDatabase) -> str:
    """连通性测试。用最无害的命令探活。"""
    probe = "SELECT VERSION()" if item.db_type == DatabaseType.MYSQL.value else "PING"
    result = await execute(item, probe)
    if result.rows:
        return str(result.rows[0][0])
    return result.text or "连接成功"
