"""数据库台账与执行通道。

「默认只读」这句约束不靠命令解析兜底。解析（``ops_safety``）只是第一道门，
真正的保证在连接本身：MySQL 会话开在 ``READ ONLY`` 事务里，并带上
``MAX_EXECUTION_TIME``——即便解析被绕过，服务端自己会拒绝写入。Redis 没有
只读会话这种东西，因此走白名单 + 硬禁用清单，并且不允许任何脚本类命令。

连接台账上的 ``writable`` 开关只对手敲控制台生效：打开后会话不再 READ ONLY、
网关换用可写判定（DML/DDL 放行，高危函数与账户级操作仍拦）。元数据浏览和
AI 通道永远走只读路径，与开关无关。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import pymysql
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import DecryptionError, decrypt, encrypt, is_masked, mask
from app.models.entities import OpsDatabase, OpsSqlFavorite
from app.models.schemas import (
    DatabaseType,
    DbAlterAction,
    DbAlterPreview,
    DbBatchResult,
    DbBatchStatement,
    DbColumnDef,
    DbColumnItem,
    DbCompletionColumn,
    DbCompletionTable,
    DbErd,
    DbErdColumn,
    DbErdRelation,
    DbErdTable,
    DbExecuteResult,
    DbForeignKeyItem,
    DbIndexDef,
    DbIndexItem,
    DbRowFilter,
    DbRowSort,
    DbRowsResult,
    DbRowWriteResult,
    DbSchemaItem,
    DbTableDef,
    DbTableDefUpdate,
    DbTableDdl,
    DbTableItem,
    OpsDatabaseCreate,
    OpsDatabaseItem,
    OpsDatabaseUpdate,
    RedisElementAddRequest,
    RedisElementDeleteRequest,
    RedisKeyCreateRequest,
    RedisKeyDeleteRequest,
    RedisKeyDetail,
    RedisKeyItem,
    RedisScanResult,
    RedisStringUpdateRequest,
    RedisTtlRequest,
    RedisWriteResult,
    SqlFavoriteCreate,
    SqlFavoriteUpdate,
)
from app.services.ops_safety import (
    Verdict,
    classify_redis,
    classify_redis_writable,
    classify_sql,
    classify_sql_writable,
    ensure_limit,
    split_sql_script,
)

logger = logging.getLogger("yangvis.ops_database")


class OpsDbError(RuntimeError):
    """连接或执行数据库命令失败。"""


class OpsDbForbidden(RuntimeError):
    """命令被安全策略挡下。与执行失败区分开，前端要给不同的提示。"""


class OpsDatabaseService:
    """数据库台账。台账全员共用：``owner_id`` 只记录登记人/审计归属，
    不再作为可见性过滤；增删改由路由层限定管理员。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    def _scope(self, stmt: Select) -> Select:
        # 台账全局化后不再按 owner 过滤；保留这个方法只是为了让各查询点的
        # 结构不变。
        return stmt

    def _require_unique_name(self, name: str, *, exclude_id: Optional[int] = None) -> None:
        """台账共用后名称全局唯一。DB 层仍只有 (owner_id, name) 复合索引，
        全局唯一靠这层预查强制（存量历史重名不强刷，只挡新增/改名）。"""
        stmt = select(OpsDatabase.id).where(OpsDatabase.name == name)
        if exclude_id is not None:
            stmt = stmt.where(OpsDatabase.id != exclude_id)
        if self._db.scalar(stmt) is not None:
            raise ValueError(f"数据库名称「{name}」已存在")

    # -- 查询 ---------------------------------------------------------------

    def list(
        self, keyword: Optional[str] = None, db_type: Optional[DatabaseType] = None
    ) -> List[OpsDatabase]:
        stmt = self._scope(select(OpsDatabase))
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(OpsDatabase.name.like(like) | OpsDatabase.host.like(like))
        if db_type is not None:
            stmt = stmt.where(OpsDatabase.db_type == db_type.value)
        return list(self._db.scalars(stmt.order_by(OpsDatabase.id.desc())).all())

    def get(self, database_id: int) -> OpsDatabase:
        item = self._db.scalar(self._scope(select(OpsDatabase).where(OpsDatabase.id == database_id)))
        if item is None:
            raise LookupError("数据库不存在")
        return item

    # -- 变更操作 -----------------------------------------------------------

    def create(self, payload: OpsDatabaseCreate) -> OpsDatabase:
        self._require_unique_name(payload.name.strip())

        item = OpsDatabase(
            owner_id=self._owner_id,
            name=payload.name.strip(),
            db_type=payload.db_type.value,
            host=payload.host.strip(),
            port=payload.port,
            username=(payload.username or "").strip() or None,
            password_enc=encrypt(payload.password or ""),
            db_name=(payload.db_name or "").strip() or None,
            writable=payload.writable,
            color=payload.color,
            remark=payload.remark,
        )
        self._db.add(item)
        self._commit_unique(f"数据库名称「{item.name}」已存在")
        self._db.refresh(item)
        logger.info(
            "owner %s created database %s (%s %s:%s, writable=%s)",
            self._owner_id,
            item.id,
            item.db_type,
            item.host,
            item.port,
            item.writable,
        )
        return item

    def update(self, database_id: int, payload: OpsDatabaseUpdate) -> OpsDatabase:
        item = self.get(database_id)
        data = payload.model_dump(exclude_unset=True)

        for field in ("name", "host", "username", "db_name", "remark"):
            value = data.get(field)
            if value is not None:
                setattr(item, field, value.strip() if isinstance(value, str) else value)

        if data.get("name") is not None:
            self._require_unique_name(item.name, exclude_id=item.id)

        if data.get("port") is not None:
            item.port = data["port"]
        if data.get("db_type") is not None:
            item.db_type = data["db_type"].value
        if data.get("writable") is not None:
            item.writable = data["writable"]
        # 颜色要能改回「无」，None 是合法值，不走上面 None 即跳过的通用循环。
        if "color" in data:
            item.color = data["color"] or None

        password = data.get("password")
        password_changed = bool(password and not is_masked(password))
        if password_changed:
            item.password_enc = encrypt(password)

        # 只有连接信息（地址/凭据/库名/类型）变化才让上次探测结果作废——
        # 改备注、颜色、写开关不影响连通性，与 ops_server.update 保持一致。
        endpoint_changed = any(
            data.get(f) is not None for f in ("host", "port", "username", "db_name", "db_type")
        )
        if endpoint_changed or password_changed:
            item.last_check_ok = False
            item.last_check_error = None

        self._commit_unique(f"数据库名称「{item.name}」已存在")
        self._db.refresh(item)
        logger.info(
            "owner %s updated database %s (writable_toggled=%s, password_changed=%s)",
            self._owner_id,
            item.id,
            data.get("writable") is not None,
            password_changed,
        )
        return item

    def delete(self, database_id: int) -> None:
        item = self.get(database_id)
        self._db.delete(item)
        self._db.commit()
        logger.info("owner %s deleted database %s (%s)", self._owner_id, item.id, item.name)

    def record_check(self, item: OpsDatabase, *, ok: bool, message: str = "") -> None:
        item.last_checked_at = datetime.now(timezone.utc)
        item.last_check_ok = ok
        item.last_check_error = None if ok else message[:512]
        self._db.commit()

    # -- 序列化 -------------------------------------------------------------

    def to_item(self, item: OpsDatabase) -> OpsDatabaseItem:
        try:
            secret = decrypt(item.password_enc)
            password = mask(secret) if secret else ""
            error = None
        except DecryptionError as exc:
            # 同 ops_server：解密失败意味着密钥轮换或数据损坏，必须留痕。
            logger.warning("failed to decrypt the password of database %s", item.id)
            password = ""
            error = str(exc)

        return OpsDatabaseItem(
            id=item.id,
            name=item.name,
            db_type=DatabaseType(item.db_type),
            host=item.host,
            port=item.port,
            username=item.username,
            password=password,
            password_error=error,
            db_name=item.db_name,
            writable=item.writable,
            color=item.color,
            remark=item.remark,
            last_checked_at=item.last_checked_at,
            last_check_ok=item.last_check_ok,
            last_check_error=item.last_check_error,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def _commit_unique(self, message: str) -> None:
        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            raise ValueError(message) from exc


class OpsSqlFavoriteService:
    """单个用户的 SQL 收藏夹。

    「通用收藏」（``database_id`` 为空）和绑定某个连接的收藏存在同一张表里；
    查询页的列表 = 当前连接的收藏 + 通用收藏。
    """

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(OpsSqlFavorite.owner_id == self._owner_id)

    def list(self, database_id: Optional[int] = None) -> List[OpsSqlFavorite]:
        stmt = self._scope(select(OpsSqlFavorite))
        if database_id is not None:
            stmt = stmt.where(
                (OpsSqlFavorite.database_id == database_id)
                | (OpsSqlFavorite.database_id.is_(None))
            )
        # 绑定连接的收藏排在通用收藏前面；MySQL 不认 NULLS LAST，用 IS NULL 代替。
        stmt = stmt.order_by(OpsSqlFavorite.database_id.is_(None), OpsSqlFavorite.id.desc())
        return list(self._db.scalars(stmt).all())

    def get(self, favorite_id: int) -> OpsSqlFavorite:
        item = self._db.scalar(
            self._scope(select(OpsSqlFavorite).where(OpsSqlFavorite.id == favorite_id))
        )
        if item is None:
            raise LookupError("收藏不存在")
        return item

    def create(self, payload: SqlFavoriteCreate) -> OpsSqlFavorite:
        item = OpsSqlFavorite(
            owner_id=self._owner_id,
            database_id=self._check_database(payload.database_id),
            title=payload.title.strip(),
            content=payload.content.strip(),
            remark=payload.remark,
        )
        self._db.add(item)
        self._db.commit()
        self._db.refresh(item)
        return item

    def update(self, favorite_id: int, payload: SqlFavoriteUpdate) -> OpsSqlFavorite:
        item = self.get(favorite_id)
        data = payload.model_dump(exclude_unset=True)

        if data.get("title") is not None:
            item.title = data["title"].strip()
        if data.get("content") is not None:
            item.content = data["content"].strip()
        if "remark" in data:
            item.remark = data["remark"]
        if "database_id" in data:
            item.database_id = self._check_database(data["database_id"])

        self._db.commit()
        self._db.refresh(item)
        return item

    def delete(self, favorite_id: int) -> None:
        item = self.get(favorite_id)
        self._db.delete(item)
        self._db.commit()

    def _check_database(self, database_id: Optional[int]) -> Optional[int]:
        """绑定连接时校验它属于当前用户；None（通用收藏）直接放行。"""
        if database_id is None:
            return None
        OpsDatabaseService(self._db, self._owner_id).get(database_id)
        return database_id


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

    started_at = datetime.now(timezone.utc)
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

    finished_at = datetime.now(timezone.utc)
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


# ---- 浏览（Navicat 式界面的数据源） -------------------------------------------
#
# 这一节的 SQL 全部在服务端拼，用户能控制的只有标识符（库名 / 表名）和分页参数。
# 标识符走反引号转义，值走参数化，会话本身仍是 READ ONLY——三层各管各的。

# MySQL 自带的系统库，排序时沉底。
_SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")

# 浏览类查询的行数上限。schema/table 列表一般不大，但不给上限就是给自己挖坑。
_BROWSE_CAP = 1000

# 补全元数据按「列」计数，大库一个 schema 几百张表乘几十列，上限得放宽些。
_COMPLETION_CAP = 20000

# Redis 集合类值最多带回多少个元素。
_COLLECTION_LIMIT = 100

# 数据浏览的页大小上限。
MAX_PAGE_SIZE = 500


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
    started_at = datetime.now(timezone.utc)
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
            finished_at=datetime.now(timezone.utc),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        ),
        any_forbidden,
    )


async def _list_redis_dbs(item: OpsDatabase) -> List[DbSchemaItem]:
    """Redis 的「schema」是有 key 的逻辑库，外加台账里配置的默认库。"""
    from redis.exceptions import RedisError

    password = _password_of(item)
    client = _connect_redis(item, password)
    try:
        info = await client.info("keyspace")
    except RedisError as exc:
        raise OpsDbError(f"读取库列表失败：{exc}") from exc
    finally:
        await client.aclose()

    found = {
        int(name[2:]): int(stats.get("keys", 0))
        for name, stats in info.items()
        if name.startswith("db") and name[2:].isdigit()
    }
    found.setdefault(_default_redis_db(item), 0)
    return [
        DbSchemaItem(name=f"db{n}", kind="redisdb", object_count=found[n])
        for n in sorted(found)
    ]


async def scan_keys(
    item: OpsDatabase, db: int, pattern: str, cursor: str, count: int
) -> RedisScanResult:
    """一页 key。SCAN 是游标式的，``cursor`` 为 "0" 表示没有更多了。"""
    from redis.exceptions import RedisError

    count = min(max(1, int(count)), MAX_PAGE_SIZE)
    password = _password_of(item)
    client = _connect_redis(item, password, db)
    try:
        next_cursor, keys = await client.scan(
            cursor=int(cursor or 0), match=pattern or None, count=count
        )
        types: List[Any] = []
        if keys:
            pipe = client.pipeline(transaction=False)
            for key in keys:
                pipe.type(key)
            types = await pipe.execute()
    except (RedisError, ValueError) as exc:
        raise OpsDbError(f"读取 key 列表失败：{exc}") from exc
    finally:
        await client.aclose()

    return RedisScanResult(
        cursor=str(next_cursor),
        keys=[
            RedisKeyItem(key=str(k), key_type=str(t))
            for k, t in zip(keys, types or ["unknown"] * len(keys))
        ],
    )


async def key_detail(item: OpsDatabase, db: int, key: str) -> RedisKeyDetail:
    """一个 key 的只读详情。集合类只带回前 ``_COLLECTION_LIMIT`` 个元素。"""
    from redis.exceptions import RedisError

    password = _password_of(item)
    client = _connect_redis(item, password, db)
    try:
        key_type = str(await client.type(key))
        if key_type == "none":
            return RedisKeyDetail(key=key, key_type="none", ttl=-2)
        ttl = int(await client.ttl(key))
        value, truncated = await _read_redis_value(client, key, key_type)
    except RedisError as exc:
        raise OpsDbError(f"读取 key 失败：{exc}") from exc
    finally:
        await client.aclose()

    return RedisKeyDetail(key=key, key_type=key_type, ttl=ttl, value=value, truncated=truncated)


async def _read_redis_value(client: Any, key: str, key_type: str) -> Tuple[Any, bool]:
    limit = _COLLECTION_LIMIT

    if key_type == "string":
        value = await client.get(key)
        text = "" if value is None else str(value)
        cap = get_settings().OPS_OUTPUT_LIMIT
        return (text[:cap], True) if len(text) > cap else (text, False)

    if key_type == "list":
        length = int(await client.llen(key))
        values = await client.lrange(key, 0, limit - 1)
        return [str(v) for v in values], length > limit

    if key_type == "set":
        cardinality = int(await client.scard(key))
        if cardinality <= limit:
            members = await client.smembers(key)
            return sorted(str(m) for m in members), False
        members = await client.srandmember(key, limit)
        if isinstance(members, str):
            members = [members]
        return sorted(str(m) for m in members), True

    if key_type == "zset":
        cardinality = int(await client.zcard(key))
        pairs = await client.zrange(key, 0, limit - 1, withscores=True)
        return [[str(m), str(s)] for m, s in pairs], cardinality > limit

    if key_type == "hash":
        length = int(await client.hlen(key))
        if length <= limit:
            data = await client.hgetall(key)
            return [[str(f), str(v)] for f, v in data.items()], False
        pairs = await client.hscan(key, count=limit)
        fields = list(pairs[1].items())[:limit]
        return [[str(f), str(v)] for f, v in fields], True

    if key_type == "stream":
        length = int(await client.xlen(key))
        entries = await client.xrange(key, count=limit)
        return [[str(entry_id), str(fields)] for entry_id, fields in entries], length > limit

    # module 类型（JSON / Bloom 等）没有通用的只读取值方式，如实告诉用户。
    return None, False


# ---- Redis 结构化写（值 / 元素 / TTL / 批量删除） ---------------------------------
#
# 和 MySQL 的行内改值同一个思路：界面上交上来的是坐标（key、field、member、
# 下标），命令由服务端用 redis-py 的类型化方法拼出来，值作为独立参数进协议层，
# 从不参与命令文本的构造——所以这一路不需要 classify_redis 的关键字网关。
# 闸门是路由层的「连接 writable × 用户 ops_write」，加上 Redis 自己会报的
# WRONGTYPE、下标越界。


# 审计文本里单个参数的长度上限：值可以是几百 KB，历史列表要看得清是哪条。
_REDIS_ARG_LEN = 2000

# 多参数命令（RPUSH / UNLINK 等）在文本里列出的参数个数，其余用总数代替。
_REDIS_ARG_DISPLAY = 10


def _redis_arg(value: str) -> str:
    """命令文本里的一个参数。只做展示（审计、提示），产出的文本从不回放执行。"""
    text = value
    if len(text) > _REDIS_ARG_LEN:
        text = text[:_REDIS_ARG_LEN] + f"…（共 {len(value)} 字符）"
    # 换行压成可见字符：审计列表是一行一条记录。
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")
    return shlex.quote(text)


def _redis_command(name: str, key: str, args: Sequence[str] = ()) -> str:
    """``NAME key arg…`` 形态的命令文本，超长参数列表按个数截。"""
    parts = [name, _redis_arg(key)]
    shown = [_redis_arg(a) for a in args[:_REDIS_ARG_DISPLAY]]
    rest = len(args) - len(shown)
    if rest > 0:
        shown.append(f"…（另有 {rest} 个）")
    return " ".join(parts + shown)


async def _redis_write(
    item: OpsDatabase, db: int, action: Callable[[Any], Awaitable[Any]]
) -> Tuple[Any, int]:
    """开一条连接跑一个写动作，返回 (回复, 耗时 ms)。

    只把 RedisError 转成 OpsDbError：动作里主动抛的语义错误（类型不对、下标
    越界）原样往上走，别在里面套两层「失败」。
    """
    from redis.exceptions import RedisError

    password = _password_of(item)
    started = time.perf_counter()
    client = _connect_redis(item, password, db)
    try:
        raw = await action(client)
    except RedisError as exc:
        raise OpsDbError(f"写入失败：{exc}") from exc
    finally:
        await client.aclose()
    return raw, int((time.perf_counter() - started) * 1000)


def _as_members(value: Any, label: str) -> List[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise OpsDbError(f"{label}需要是一个非空数组")
    return [str(v) for v in value]


def _as_pairs(value: Any, label: str) -> List[Tuple[str, str]]:
    if not isinstance(value, (list, tuple)) or not value:
        raise OpsDbError(f"{label}需要是非空的 [值, 值] 数组")
    pairs: List[Tuple[str, str]] = []
    for raw in value:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise OpsDbError(f"{label}的每一项都必须是两元素数组")
        pairs.append((str(raw[0]), str(raw[1])))
    return pairs


async def create_key(item: OpsDatabase, req: RedisKeyCreateRequest) -> RedisWriteResult:
    """新建一个 key。已存在直接拒绝——「新建」不该悄悄覆盖别人正在用的数据。"""
    key, key_type = req.key, req.key_type

    if key_type == "string":
        text = "" if req.value is None else str(req.value)
        display = _redis_command("SET", key, [text])

        async def action(client: Any) -> Any:
            return await client.set(key, text)

    elif key_type == "list":
        members = _as_members(req.value, "列表元素")
        display = _redis_command("RPUSH", key, members)

        async def action(client: Any) -> Any:
            return await client.rpush(key, *members)

    elif key_type == "set":
        members = _as_members(req.value, "集合成员")
        display = _redis_command("SADD", key, members)

        async def action(client: Any) -> Any:
            return await client.sadd(key, *members)

    elif key_type == "zset":
        pairs = _as_pairs(req.value, "有序集合元素")
        scored: List[Tuple[str, float]] = []
        for member, raw_score in pairs:
            try:
                scored.append((member, float(raw_score)))
            except ValueError:
                raise OpsDbError("有序集合的 score 需要是数字") from None
        display = _redis_command("ZADD", key, [f"{m} {s}" for m, s in scored])

        async def action(client: Any) -> Any:
            return await client.zadd(key, {m: s for m, s in scored})

    else:  # hash
        pairs = _as_pairs(req.value, "哈希字段")
        display = _redis_command("HSET", key, [f"{f} {v}" for f, v in pairs])

        async def action(client: Any) -> Any:
            return await client.hset(key, mapping=dict(pairs))

    async def run(client: Any) -> Any:
        # 存在性检查与写入在同一条连接里连着做：界面上的「新建」不该覆盖已有数据。
        if await client.exists(key):
            raise OpsDbError("key 已存在，请直接编辑它的值")
        await action(client)
        # TTL 也在这一条连接上补完，新建和设过期中间不断线。
        if req.ttl:
            await client.expire(key, req.ttl)

    _, elapsed = await _redis_write(item, req.db, run)
    if req.ttl:
        display = f"{display}; EXPIRE {_redis_arg(key)} {req.ttl}"
    return RedisWriteResult(command=display, elapsed_ms=elapsed)


async def set_string(item: OpsDatabase, req: RedisStringUpdateRequest) -> RedisWriteResult:
    """改 string 的值。"""
    key = req.key

    async def action(client: Any) -> Any:
        current = str(await client.type(key))
        if current not in ("none", "string"):
            raise OpsDbError(f"该 key 是 {current} 类型，不能按 string 改值")
        # 先记 pttl 再补回：SET 会清掉过期时间，而 KEEPTTL 只有 6.0 以上才有，
        # 「改个值把缓存的 TTL 弄没了」是这里最不该发生的副作用。
        pttl = int(await client.pttl(key))
        await client.set(key, req.value)
        if pttl > 0:
            await client.pexpire(key, pttl)

    _, elapsed = await _redis_write(item, req.db, action)
    return RedisWriteResult(
        command=_redis_command("SET", key, [req.value]), elapsed_ms=elapsed
    )


async def add_element(item: OpsDatabase, req: RedisElementAddRequest) -> RedisWriteResult:
    """给集合加一个元素（hash 的 field 已存在则按 HSET 语义覆盖值）。"""
    key = req.key
    if req.key_type == "hash":
        if not req.field:
            raise OpsDbError("哈希字段需要同时给出 field 和 value")
        value = req.value or ""
        display = _redis_command("HSET", key, [req.field, value])
        action = lambda client: client.hset(key, req.field, value)
    elif req.key_type == "list":
        if req.value is None:
            raise OpsDbError("列表元素需要给出 value")
        head = req.position == "head"
        display = _redis_command(
            "LPUSH" if head else "RPUSH", key, [req.value]
        )
        action = lambda client: (client.lpush if head else client.rpush)(key, req.value)
    elif req.key_type == "set":
        if req.value is None:
            raise OpsDbError("集合成员需要给出 value")
        display = _redis_command("SADD", key, [req.value])
        action = lambda client: client.sadd(key, req.value)
    else:  # zset
        if req.value is None or req.score is None:
            raise OpsDbError("有序集合元素需要同时给出 member 和 score")
        display = _redis_command("ZADD", key, [f"{req.score} {req.value}"])
        action = lambda client: client.zadd(key, {req.value: req.score})

    raw, elapsed = await _redis_write(item, req.db, action)
    return RedisWriteResult(command=display, elapsed_ms=elapsed, deleted=int(raw or 0))


async def remove_element(item: OpsDatabase, req: RedisElementDeleteRequest) -> RedisWriteResult:
    """删掉集合里的一个元素。``target`` 的含义随类型变，见请求模型注释。"""
    key, key_type, target = req.key, req.key_type, req.target

    if key_type == "hash":
        display = _redis_command("HDEL", key, [target])
        action = lambda client: client.hdel(key, target)
    elif key_type == "set":
        display = _redis_command("SREM", key, [target])
        action = lambda client: client.srem(key, target)
    elif key_type == "zset":
        display = _redis_command("ZREM", key, [target])
        action = lambda client: client.zrem(key, target)
    elif key_type == "stream":
        display = _redis_command("XDEL", key, [target])
        action = lambda client: client.xdel(key, target)
    elif key_type == "list":
        try:
            index = int(target)
        except ValueError:
            raise OpsDbError("列表元素按下标删除，下标需要是整数") from None
        if index < 0:
            raise OpsDbError("列表下标不能为负")
        # 按位置精确删一个元素没有单条命令：LREM 按值删，同值会一起没掉。
        # 先把这一格换成随机哨兵，再按哨兵删一条——LSET 越界本身会报错，
        # 等于顺带替我们校验了下标。
        sentinel = f"__yangvis_del_{uuid.uuid4().hex}__"

        async def action(client: Any) -> Any:
            await client.lset(key, index, sentinel)
            return await client.lrem(key, 1, sentinel)

        display = f"LREM {_redis_arg(key)} 1 <第 {index} 个元素>"
    else:
        raise OpsDbError("string 没有元素可删，请改值或删除整个 key")

    raw, elapsed = await _redis_write(item, req.db, action)
    return RedisWriteResult(command=display, elapsed_ms=elapsed, deleted=int(raw or 0))


async def set_ttl(item: OpsDatabase, req: RedisTtlRequest) -> RedisWriteResult:
    """EXPIRE / PERSIST。"""
    key = req.key
    if req.action == "expire":
        if not req.seconds:
            raise OpsDbError("设置过期需要给出秒数")

        async def action(client: Any) -> Any:
            expired = await client.expire(key, req.seconds)
            # EXPIRE 对不存在的 key 返回 0：此时用户以为设上了，其实什么都没发生。
            if not expired:
                raise OpsDbError("key 不存在或已过期")
            return expired

        display = f"EXPIRE {_redis_arg(key)} {req.seconds}"
    else:
        action = lambda client: client.persist(key)
        display = f"PERSIST {_redis_arg(key)}"

    raw, elapsed = await _redis_write(item, req.db, action)
    return RedisWriteResult(command=display, elapsed_ms=elapsed, deleted=int(bool(raw)))


async def delete_keys(item: OpsDatabase, req: RedisKeyDeleteRequest) -> RedisWriteResult:
    """批量删除 key。用 UNLINK 而不是 DEL：大集合的回收交给后台线程，不卡主线程。"""
    keys = [k for k in req.keys if k]
    if not keys:
        raise OpsDbError("没有选中任何 key")
    display = _redis_command("UNLINK", keys[0], keys[1:])

    raw, elapsed = await _redis_write(item, req.db, lambda client: client.unlink(*keys))
    return RedisWriteResult(
        command=display, elapsed_ms=elapsed, deleted=int(raw or 0)
    )


async def test_connection(item: OpsDatabase) -> str:
    """连通性测试。用最无害的命令探活。"""
    probe = "SELECT VERSION()" if item.db_type == DatabaseType.MYSQL.value else "PING"
    result = await execute(item, probe)
    if result.rows:
        return str(result.rows[0][0])
    return result.text or "连接成功"
