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
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymysql
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import DecryptionError, decrypt, encrypt, is_masked, mask
from app.models.entities import OpsDatabase, OpsSqlFavorite
from app.models.schemas import (
    DatabaseType,
    DbBatchResult,
    DbBatchStatement,
    DbColumnItem,
    DbCompletionColumn,
    DbCompletionTable,
    DbExecuteResult,
    DbRowFilter,
    DbRowSort,
    DbRowsResult,
    DbRowWriteResult,
    DbSchemaItem,
    DbTableItem,
    OpsDatabaseCreate,
    OpsDatabaseItem,
    OpsDatabaseUpdate,
    RedisKeyDetail,
    RedisKeyItem,
    RedisScanResult,
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
        "EXTRA, COLUMN_COMMENT "
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
    structured, params = _build_where(filters)
    parts = [f"({where})"] if where else []
    if structured:
        parts.append(structured)
    where_sql = f" WHERE {' AND '.join(parts)}" if parts else ""
    order_sql = _build_order_by(sorts)
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
    """把值渲染成 MySQL 字面量，仅供审计展示与网关兜底判定（不参与执行）。"""
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


async def test_connection(item: OpsDatabase) -> str:
    """连通性测试。用最无害的命令探活。"""
    probe = "SELECT VERSION()" if item.db_type == DatabaseType.MYSQL.value else "PING"
    result = await execute(item, probe)
    if result.rows:
        return str(result.rows[0][0])
    return result.text or "连接成功"
