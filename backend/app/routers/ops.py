"""智能运维接口：服务器 / 数据库台账、控制台执行与 PTY 终端。

运维问答（AI 对话）已并入主对话的 SSE 体系（``/api/chat``，见 routers/chat.py）：
会话与待确认写命令都落库，写确认走「propose 落库即返回 → confirm POST 在自己
的 worker 上执行并续答」的两阶段协议，不再依赖任何跨进程状态。本模块只剩下
台账 CRUD、数据库控制台与一条 WebSocket。

``/ops/terminal`` 是一条真 PTY：xterm.js 敲下的每个字节原样转发给 SSH，
反过来也一样。它不做任何命令过滤——那是用户自己的手，和他 ssh 上去没有区别。
但台账全员共用之后，这只手能摸到的是共享资产，所以握手要求 ops_write
（见 ``CurrentUser.can_ops_write``）：只读账号能看台账、跑只读命令，
却拿不到一个绕过一切约束的 root shell。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.deps import require_admin, require_user, resolve_ws_user
from app.errors import CODE_OPS_EXEC_FAILED, CODE_OPS_FORBIDDEN, BusinessError
from app.models.entities import OpsAuditLog
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    DatabaseType,
    DbColumnItem,
    DbCompletionTable,
    DbBatchRequest,
    DbBatchResult,
    DbExecuteRequest,
    DbExecuteResult,
    DbRowDeleteRequest,
    DbRowFilter,
    DbRowSort,
    DbRowsResult,
    DbRowUpdateRequest,
    DbRowWriteResult,
    DbSchemaItem,
    DbTableItem,
    ListResponse,
    OpsAuditItem,
    OpsDatabaseCreate,
    OpsDatabaseItem,
    OpsDatabaseUpdate,
    OpsServerCreate,
    OpsServerItem,
    OpsServerUpdate,
    RedisKeyDetail,
    RedisScanResult,
    SqlFavoriteCreate,
    SqlFavoriteItem,
    SqlFavoriteUpdate,
    TestResult,
    WsTicket,
)
from app.security import create_ws_ticket
from app.services import ops_database as db_ops
from app.services import ops_server as server_ops
from app.services.chat_ops import Auditor

logger = logging.getLogger("yangvis.ops")

router = APIRouter(prefix="/ops", tags=["ops"])

settings = get_settings()

# WebSocket 关闭码。1008 是「策略违规」，用来表达鉴权失败最贴切。
WS_UNAUTHORIZED = status.WS_1008_POLICY_VIOLATION


def get_server_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> server_ops.OpsServerService:
    return server_ops.OpsServerService(db, user.user_id)


def get_database_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> db_ops.OpsDatabaseService:
    return db_ops.OpsDatabaseService(db, user.user_id)


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ---- 入场票 -----------------------------------------------------------------


@router.post("/ws-ticket", response_model=APIResponse[WsTicket])
async def issue_ws_ticket(user: CurrentUser = Depends(require_user)) -> APIResponse[WsTicket]:
    """签发一张短时效的 WebSocket 入场票。

    浏览器无法给 WebSocket 握手加 Authorization 头，只能走 query string，
    而 query string 会进访问日志。所以这里不发那把 12 小时的 access token，
    只发一张 60 秒、且被 ``require_user`` 拒绝用于 REST 的票。
    """
    ticket = create_ws_ticket(
        user_id=user.user_id, username=user.username, is_admin=user.is_admin
    )
    return APIResponse(data=WsTicket(ticket=ticket, expires_in=settings.OPS_WS_TICKET_TTL))


# ---- 服务器 -----------------------------------------------------------------


@router.get("/servers", response_model=APIResponse[ListResponse[OpsServerItem]])
async def list_servers(
    keyword: Optional[str] = Query(default=None, max_length=128),
    service: server_ops.OpsServerService = Depends(get_server_service),
) -> APIResponse[ListResponse[OpsServerItem]]:
    items = [service.to_item(row) for row in service.list(keyword)]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/servers", response_model=APIResponse[OpsServerItem])
async def create_server(
    payload: OpsServerCreate,
    _admin: CurrentUser = Depends(require_admin),
    service: server_ops.OpsServerService = Depends(get_server_service),
) -> APIResponse[OpsServerItem]:
    try:
        row = service.create(payload)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return APIResponse(data=service.to_item(row))


@router.put("/servers/{server_id}", response_model=APIResponse[OpsServerItem])
async def update_server(
    server_id: int,
    payload: OpsServerUpdate,
    _admin: CurrentUser = Depends(require_admin),
    service: server_ops.OpsServerService = Depends(get_server_service),
) -> APIResponse[OpsServerItem]:
    try:
        row = service.update(server_id, payload)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return APIResponse(data=service.to_item(row))


@router.delete("/servers/{server_id}", response_model=APIResponse[None])
async def delete_server(
    server_id: int,
    _admin: CurrentUser = Depends(require_admin),
    service: server_ops.OpsServerService = Depends(get_server_service),
) -> APIResponse[None]:
    try:
        service.delete(server_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=None, message="deleted")


@router.post("/servers/{server_id}/test", response_model=APIResponse[TestResult])
async def test_server(
    server_id: int,
    service: server_ops.OpsServerService = Depends(get_server_service),
) -> APIResponse[TestResult]:
    try:
        server = service.get(server_id)
    except LookupError as exc:
        raise _not_found(exc) from exc

    try:
        banner = await server_ops.test_connection(server, service)
    except server_ops.OpsConnectError as exc:
        logger.warning("connectivity test of server %s failed: %s", server_id, exc)
        service.record_check(server, ok=False, message=str(exc))
        return APIResponse(data=TestResult(success=False, message=str(exc)))

    service.record_check(server, ok=True)
    return APIResponse(data=TestResult(success=True, message=banner))


# ---- 数据库 -----------------------------------------------------------------


@router.get("/databases", response_model=APIResponse[ListResponse[OpsDatabaseItem]])
async def list_databases(
    keyword: Optional[str] = Query(default=None, max_length=128),
    db_type: Optional[DatabaseType] = Query(default=None),
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[ListResponse[OpsDatabaseItem]]:
    items = [service.to_item(row) for row in service.list(keyword, db_type)]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/databases", response_model=APIResponse[OpsDatabaseItem])
async def create_database(
    payload: OpsDatabaseCreate,
    _admin: CurrentUser = Depends(require_admin),
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[OpsDatabaseItem]:
    try:
        row = service.create(payload)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return APIResponse(data=service.to_item(row))


@router.put("/databases/{database_id}", response_model=APIResponse[OpsDatabaseItem])
async def update_database(
    database_id: int,
    payload: OpsDatabaseUpdate,
    _admin: CurrentUser = Depends(require_admin),
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[OpsDatabaseItem]:
    try:
        row = service.update(database_id, payload)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return APIResponse(data=service.to_item(row))


@router.delete("/databases/{database_id}", response_model=APIResponse[None])
async def delete_database(
    database_id: int,
    _admin: CurrentUser = Depends(require_admin),
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[None]:
    try:
        service.delete(database_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=None, message="deleted")


@router.post("/databases/{database_id}/test", response_model=APIResponse[TestResult])
async def test_database(
    database_id: int,
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[TestResult]:
    try:
        row = service.get(database_id)
    except LookupError as exc:
        raise _not_found(exc) from exc

    try:
        banner = await db_ops.test_connection(row)
    except (db_ops.OpsDbError, db_ops.OpsDbForbidden) as exc:
        logger.warning("connectivity test of database %s failed: %s", database_id, exc)
        service.record_check(row, ok=False, message=str(exc))
        return APIResponse(data=TestResult(success=False, message=str(exc)))

    service.record_check(row, ok=True)
    return APIResponse(data=TestResult(success=True, message=banner))


@router.post("/databases/{database_id}/execute", response_model=APIResponse[DbExecuteResult])
async def execute_database(
    database_id: int,
    payload: DbExecuteRequest,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[DbExecuteResult]:
    """数据库控制台：用户手敲的一条命令。

    默认只读；连接在台账里开了「允许写入」、且用户持有 ops_write 通行证时，
    这条通道可以执行 DML/DDL，审计 verdict 记为 ``write`` 以便和查询区分开。
    """
    service = db_ops.OpsDatabaseService(db, user.user_id)
    try:
        row = service.get(database_id)
    except LookupError as exc:
        raise _not_found(exc) from exc

    command = payload.command.strip()
    # 连接开关与用户通行证叠加；可写通道里成功与执行失败都记 write，
    # 被网关拦下仍是 forbidden。
    writable = row.writable and user.can_ops_write
    verdict = "write" if writable else "readonly"
    try:
        result = await db_ops.execute(row, command, payload.schema_, allow_write=user.can_ops_write)
    except db_ops.OpsDbForbidden as exc:
        _audit(db, user.user_id, row, command, "forbidden", False, str(exc))
        raise BusinessError(CODE_OPS_FORBIDDEN, str(exc)) from exc
    except db_ops.OpsDbError as exc:
        _audit(db, user.user_id, row, command, verdict, False, str(exc))
        raise BusinessError(CODE_OPS_EXEC_FAILED, str(exc)) from exc

    _audit(db, user.user_id, row, command, verdict, True)
    _note_browse_success(service, row)
    return APIResponse(data=result)


@router.post(
    "/databases/{database_id}/execute/batch", response_model=APIResponse[DbBatchResult]
)
async def execute_database_batch(
    database_id: int,
    payload: DbBatchRequest,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[DbBatchResult]:
    """数据库控制台：逐句执行一段脚本（Navicat 式批量执行）。

    与单语句端点同一套安全网关（只读 / 可写随连接开关与用户通行证叠加）。
    审计每批只留一条——历史弹层按「次」展示才有意义，每句一条会把 50 条
    历史一次刷光。
    """
    service = db_ops.OpsDatabaseService(db, user.user_id)
    try:
        row = service.get(database_id)
    except LookupError as exc:
        raise _not_found(exc) from exc

    command = payload.command.strip()
    writable = row.writable and user.can_ops_write
    verdict = "write" if writable else "readonly"
    try:
        result, any_forbidden = await db_ops.execute_batch(
            row, command, payload.schema_, allow_write=user.can_ops_write
        )
    except db_ops.OpsDbError as exc:
        _audit(db, user.user_id, row, command, verdict, False, str(exc))
        raise BusinessError(CODE_OPS_EXEC_FAILED, str(exc)) from exc

    first_error = next((s.message for s in result.statements if s.status == "error"), None)
    _audit(
        db, user.user_id, row, command,
        "forbidden" if any_forbidden else verdict,
        result.failed == 0,
        first_error,
    )
    if result.succeeded:
        _note_browse_success(service, row)
    return APIResponse(data=result)


# ---- SQL 收藏 ---------------------------------------------------------------

def get_favorite_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> db_ops.OpsSqlFavoriteService:
    return db_ops.OpsSqlFavoriteService(db, user.user_id)


@router.get("/sql-favorites", response_model=APIResponse[ListResponse[SqlFavoriteItem]])
async def list_sql_favorites(
    database_id: Optional[int] = Query(default=None),
    service: db_ops.OpsSqlFavoriteService = Depends(get_favorite_service),
) -> APIResponse[ListResponse[SqlFavoriteItem]]:
    """查询页的收藏列表：指定连接时返回它的收藏加上所有连接通用的收藏。"""
    rows = service.list(database_id)
    items = [SqlFavoriteItem.model_validate(row) for row in rows]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/sql-favorites", response_model=APIResponse[SqlFavoriteItem])
async def create_sql_favorite(
    payload: SqlFavoriteCreate,
    service: db_ops.OpsSqlFavoriteService = Depends(get_favorite_service),
) -> APIResponse[SqlFavoriteItem]:
    try:
        row = service.create(payload)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=SqlFavoriteItem.model_validate(row))


@router.put("/sql-favorites/{favorite_id}", response_model=APIResponse[SqlFavoriteItem])
async def update_sql_favorite(
    favorite_id: int,
    payload: SqlFavoriteUpdate,
    service: db_ops.OpsSqlFavoriteService = Depends(get_favorite_service),
) -> APIResponse[SqlFavoriteItem]:
    try:
        row = service.update(favorite_id, payload)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=SqlFavoriteItem.model_validate(row))


@router.delete("/sql-favorites/{favorite_id}", response_model=APIResponse[None])
async def delete_sql_favorite(
    favorite_id: int,
    service: db_ops.OpsSqlFavoriteService = Depends(get_favorite_service),
) -> APIResponse[None]:
    try:
        service.delete(favorite_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=None, message="deleted")


# ---- 数据库浏览（Navicat 式界面的数据源，全部只读） -----------------------------


def _get_typed_database(
    database_id: int,
    expect: DatabaseType,
    service: db_ops.OpsDatabaseService,
) -> Any:
    """取台账并校验类型。浏览端点按 MySQL / Redis 分成两套，调错了给 400。"""
    try:
        row = service.get(database_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    if row.db_type != expect.value:
        label = "MySQL" if expect is DatabaseType.MYSQL else "Redis"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"该连接不是 {label}，无法执行此操作",
        )
    return row


def _browse_error(exc: db_ops.OpsDbError) -> BusinessError:
    return BusinessError(CODE_OPS_EXEC_FAILED, str(exc))


def _parse_json_list(raw: Optional[str], model: Any, name: str) -> list:
    """把 JSON 数组字符串解析成 pydantic 模型列表，非法输入落成业务错误。"""
    if not raw:
        return []
    try:
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise ValueError
        return [model.model_validate(item) for item in payload]
    except (ValueError, TypeError):
        raise ValueError(f"{name} 参数不是合法的 JSON 数组") from None


def _note_browse_success(service: db_ops.OpsDatabaseService, row: Any) -> None:
    """浏览成功本身就是一次连通性证明，把「未测试」翻成正常。

    只在状态会变化时落库——浏览是高频操作，不能每展开一个节点都写一次。
    反方向（浏览失败）不记：失败多半是查询本身的问题（权限、表不存在），
    记成连接故障会误导。
    """
    if not row.last_check_ok:
        service.record_check(row, ok=True)


@router.get("/databases/{database_id}/schemas", response_model=APIResponse[ListResponse[DbSchemaItem]])
async def list_database_schemas(
    database_id: int,
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[ListResponse[DbSchemaItem]]:
    """连接展开的第一层：MySQL 的 schema 列表，或 Redis 的逻辑库列表。"""
    try:
        row = service.get(database_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    try:
        items = await db_ops.list_schemas(row)
    except db_ops.OpsDbError as exc:
        raise _browse_error(exc) from exc
    _note_browse_success(service, row)
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get(
    "/databases/{database_id}/schemas/{schema}/tables",
    response_model=APIResponse[ListResponse[DbTableItem]],
)
async def list_database_tables(
    database_id: int,
    schema: str,
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[ListResponse[DbTableItem]]:
    row = _get_typed_database(database_id, DatabaseType.MYSQL, service)
    try:
        items = await db_ops.list_tables(row, schema)
    except db_ops.OpsDbError as exc:
        raise _browse_error(exc) from exc
    _note_browse_success(service, row)
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get(
    "/databases/{database_id}/schemas/{schema}/tables/{table}/columns",
    response_model=APIResponse[ListResponse[DbColumnItem]],
)
async def list_database_columns(
    database_id: int,
    schema: str,
    table: str,
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[ListResponse[DbColumnItem]]:
    row = _get_typed_database(database_id, DatabaseType.MYSQL, service)
    try:
        items = await db_ops.list_columns(row, schema, table)
    except db_ops.OpsDbError as exc:
        raise _browse_error(exc) from exc
    _note_browse_success(service, row)
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get(
    "/databases/{database_id}/schemas/{schema}/completion",
    response_model=APIResponse[ListResponse[DbCompletionTable]],
)
async def database_completion_meta(
    database_id: int,
    schema: str,
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[ListResponse[DbCompletionTable]]:
    """查询编辑器的补全元数据：该库下所有表和列，一次拉齐。"""
    row = _get_typed_database(database_id, DatabaseType.MYSQL, service)
    try:
        items = await db_ops.list_completion_meta(row, schema)
    except db_ops.OpsDbError as exc:
        raise _browse_error(exc) from exc
    _note_browse_success(service, row)
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get(
    "/databases/{database_id}/schemas/{schema}/tables/{table}/rows",
    response_model=APIResponse[DbRowsResult],
)
async def browse_database_rows(
    database_id: int,
    schema: str,
    table: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=db_ops.MAX_PAGE_SIZE),
    filters: Optional[str] = Query(default=None),
    order_by: Optional[str] = Query(default=None),
    where: Optional[str] = Query(default=None, max_length=1024),
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[DbRowsResult]:
    """数据浏览。``filters`` / ``order_by`` 是 JSON 数组字符串，由前端筛选/排序菜单生成；

    ``where`` 是用户手输的原始条件片段，服务端会过只读网关校验。
    """
    row = _get_typed_database(database_id, DatabaseType.MYSQL, service)
    try:
        filter_list = _parse_json_list(filters, DbRowFilter, "filters")
        sort_list = _parse_json_list(order_by, DbRowSort, "order_by")
    except ValueError as exc:
        raise BusinessError(CODE_OPS_EXEC_FAILED, str(exc)) from exc
    try:
        result = await db_ops.browse_rows(
            row, schema, table, page, page_size, filter_list, sort_list, where or ""
        )
    except db_ops.OpsDbError as exc:
        raise _browse_error(exc) from exc
    _note_browse_success(service, row)
    return APIResponse(data=result)


def _row_write_fallback(verb: str, schema: str, table: str) -> str:
    """审计兜底文本：展示 SQL 还没生成出来就失败时（表不存在等），至少留下动作。"""
    label = "行内改值" if verb == "update" else "删除记录"
    return f"[{label}] {schema}.{table}"


@router.put(
    "/databases/{database_id}/schemas/{schema}/tables/{table}/rows",
    response_model=APIResponse[DbRowWriteResult],
)
async def update_database_row(
    database_id: int,
    schema: str,
    table: str,
    payload: DbRowUpdateRequest,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[DbRowWriteResult]:
    """数据浏览页的行内改值：服务端按主键（没主键按全列原值）生成单条 UPDATE。

    与手敲控制台同一套双闸门（连接 writable × 用户 ops_write）与审计，
    差别只是 SQL 由服务端生成、值参数化，用户接触不到 SQL 原文。
    """
    service = db_ops.OpsDatabaseService(db, user.user_id)
    row = _get_typed_database(database_id, DatabaseType.MYSQL, service)

    if not (row.writable and user.can_ops_write):
        command = _row_write_fallback("update", schema, table)
        _audit(db, user.user_id, row, command, "forbidden", False, "连接未开启写入或用户无写权限")
        raise BusinessError(CODE_OPS_FORBIDDEN, "该连接未开启写入，或当前账号没有写权限")

    try:
        display, result = await db_ops.update_row(row, schema, table, payload.key, payload.sets)
    except db_ops.OpsDbForbidden as exc:
        command = getattr(exc, "display", "") or _row_write_fallback("update", schema, table)
        _audit(db, user.user_id, row, command, "forbidden", False, str(exc))
        raise BusinessError(CODE_OPS_FORBIDDEN, str(exc)) from exc
    except db_ops.OpsDbError as exc:
        command = getattr(exc, "display", "") or _row_write_fallback("update", schema, table)
        _audit(db, user.user_id, row, command, "write", False, str(exc))
        raise _browse_error(exc) from exc

    _audit(db, user.user_id, row, display, "write", True)
    return APIResponse(data=result)


@router.delete(
    "/databases/{database_id}/schemas/{schema}/tables/{table}/rows",
    response_model=APIResponse[DbRowWriteResult],
)
async def delete_database_row(
    database_id: int,
    schema: str,
    table: str,
    payload: DbRowDeleteRequest,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[DbRowWriteResult]:
    """数据浏览页的删除记录：按主键（没主键按全列原值）生成单条 DELETE。

    闸门与审计同行内改值；前端在调用前已做过一次删除确认。
    """
    service = db_ops.OpsDatabaseService(db, user.user_id)
    row = _get_typed_database(database_id, DatabaseType.MYSQL, service)

    if not (row.writable and user.can_ops_write):
        command = _row_write_fallback("delete", schema, table)
        _audit(db, user.user_id, row, command, "forbidden", False, "连接未开启写入或用户无写权限")
        raise BusinessError(CODE_OPS_FORBIDDEN, "该连接未开启写入，或当前账号没有写权限")

    try:
        display, result = await db_ops.delete_row(row, schema, table, payload.key)
    except db_ops.OpsDbForbidden as exc:
        command = getattr(exc, "display", "") or _row_write_fallback("delete", schema, table)
        _audit(db, user.user_id, row, command, "forbidden", False, str(exc))
        raise BusinessError(CODE_OPS_FORBIDDEN, str(exc)) from exc
    except db_ops.OpsDbError as exc:
        command = getattr(exc, "display", "") or _row_write_fallback("delete", schema, table)
        _audit(db, user.user_id, row, command, "write", False, str(exc))
        raise _browse_error(exc) from exc

    _audit(db, user.user_id, row, display, "write", True)
    return APIResponse(data=result)


@router.get("/databases/{database_id}/keys", response_model=APIResponse[RedisScanResult])
async def scan_database_keys(
    database_id: int,
    db: int = Query(default=0, ge=0, le=15),
    pattern: str = Query(default="*", max_length=256),
    cursor: str = Query(default="0", max_length=32),
    count: int = Query(default=100, ge=1, le=db_ops.MAX_PAGE_SIZE),
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[RedisScanResult]:
    row = _get_typed_database(database_id, DatabaseType.REDIS, service)
    try:
        result = await db_ops.scan_keys(row, db, pattern, cursor, count)
    except db_ops.OpsDbError as exc:
        raise _browse_error(exc) from exc
    _note_browse_success(service, row)
    return APIResponse(data=result)


@router.get("/databases/{database_id}/key", response_model=APIResponse[RedisKeyDetail])
async def read_database_key(
    database_id: int,
    key: str = Query(..., min_length=1, max_length=4096),
    db: int = Query(default=0, ge=0, le=15),
    service: db_ops.OpsDatabaseService = Depends(get_database_service),
) -> APIResponse[RedisKeyDetail]:
    row = _get_typed_database(database_id, DatabaseType.REDIS, service)
    try:
        result = await db_ops.key_detail(row, db, key)
    except db_ops.OpsDbError as exc:
        raise _browse_error(exc) from exc
    _note_browse_success(service, row)
    return APIResponse(data=result)


def _audit(
    db: Session,
    owner_id: int,
    target,
    command: str,
    verdict: str,
    success: bool,
    error: Optional[str] = None,
) -> None:
    db.add(
        OpsAuditLog(
            owner_id=owner_id,
            target_type="database",
            target_id=target.id,
            target_name=target.name,
            actor="user",
            command=command[:8000],
            verdict=verdict,
            success=success,
            error=error[:512] if error else None,
        )
    )
    db.commit()


# ---- 审计 -------------------------------------------------------------------


@router.get("/audit", response_model=APIResponse[ListResponse[OpsAuditItem]])
async def list_audit(
    target_type: Optional[str] = Query(default=None, pattern="^(server|database)$"),
    target_id: Optional[int] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[ListResponse[OpsAuditItem]]:
    """审计列表。limit + offset 翻页；total 是符合过滤条件的真实总数，
    不是本页条数——否则分页器永远只有一页。

    可见性：普通用户只看自己的操作；台账全员共用后，管理员能看到所有人
    对共享资产的操作——这正是共享资产需要的团队可见性。"""
    stmt = select(OpsAuditLog)
    if not user.is_admin:
        stmt = stmt.where(OpsAuditLog.owner_id == user.user_id)
    if target_type:
        stmt = stmt.where(OpsAuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(OpsAuditLog.target_id == target_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(
            stmt.order_by(OpsAuditLog.id.desc()).offset(offset).limit(limit)
        ).all()
    )
    items = [OpsAuditItem.model_validate(row) for row in rows]
    return APIResponse(data=ListResponse(items=items, total=total))


# ---- WebSocket：PTY 终端 -----------------------------------------------------


async def _authorize(websocket: WebSocket, ticket: Optional[str]) -> Optional[CurrentUser]:
    """握手鉴权。失败时 accept 之后立刻带码关闭——WebSocket 没有 401。"""
    db = SessionLocal()
    try:
        user = resolve_ws_user(ticket, db)
    finally:
        db.close()

    if user is None:
        await websocket.accept()
        await websocket.close(code=WS_UNAUTHORIZED, reason="未登录或入场票已过期")
        return None

    await websocket.accept()
    return user


# 终端输出里出现这类提示，说明下一行键盘输入是密码而不是命令——不记。
# 真实提示形态不一（"[sudo] password for ops: "、"Enter passphrase for key ...: "、
# "Password: "），关键词不一定贴着结尾，所以允许关键词后隔一段再以冒号收尾。
_PASSWORD_PROMPT = re.compile(
    r"(password|passphrase|密码|口令)[^\n\r]{0,40}[:：]\s*$", re.IGNORECASE
)


class _CommandLineTracker:
    """从 PTY 输入流里尽力重建用户提交的命令行。

    真 PTY 不做命令过滤（那是用户自己的手），但「他敲过什么」要留痕。难处在
    PTY 是原始字节流，带着行编辑的全部复杂性：退格、Ctrl+U、转义序列、上下
    翻历史。完美重建需要一台完整的终端模拟器，那是堡垒机级别的活；这里走
    务实路线——可打印字符入缓冲，常见编辑键尽力处理，回车提交。翻历史翻出
    来的命令会重建不全，接受这个偏差：审计要的是「看得到大概干过什么」。

    一条硬规则：输出侧刚问过密码（sudo、mysql -p 之类），下一行输入不记——
    把密码写进审计库比漏记一条命令糟得多。
    """

    def __init__(self) -> None:
        self._buf: List[str] = []
        self._escape = False  # 正在吞一个 ANSI 转义序列
        self._output_tail = ""  # 终端输出的滚动尾部，用来认密码提示
        self._muted = False  # 下一行是密码输入，跳过

    def feed_output(self, chunk: str) -> None:
        """SSH → 浏览器方向的输出，只留一小段尾巴认提示符。"""
        self._output_tail = (self._output_tail + chunk)[-200:]
        if _PASSWORD_PROMPT.search(self._output_tail):
            self._muted = True

    def feed(self, text: str) -> List[str]:
        """喂入一段键盘输入，返回本次提交的命令行（粘贴多行时可能多条）。"""
        submitted: List[str] = []
        for ch in text:
            if self._escape:
                # CSI/SS3 序列以字母或 ~ 收尾，整个吞掉不进缓冲。
                if ch.isalpha() or ch == "~":
                    self._escape = False
                continue
            if ch == "\x1b":
                self._escape = True
            elif ch in ("\r", "\n"):
                line = "".join(self._buf).strip()
                self._buf.clear()
                if self._muted:
                    # 密码行：跳过并恢复记录。
                    self._muted = False
                elif line:
                    submitted.append(line)
            elif ch in ("\x7f", "\x08"):  # 退格
                if self._buf:
                    self._buf.pop()
            elif ch == "\x15":  # Ctrl+U 清行
                self._buf.clear()
            elif ch == "\x17":  # Ctrl+W 删一个词
                while self._buf and self._buf[-1] == " ":
                    self._buf.pop()
                while self._buf and self._buf[-1] != " ":
                    self._buf.pop()
            elif ch == "\x03":  # Ctrl+C：当前行作废
                self._buf.clear()
            elif ch >= " ":  # 可打印字符（含中文等多字节字符）
                self._buf.append(ch)
            # \t（补全请求，不产生字符）与其余控制字符忽略。
        return submitted


def _record_manual_command(user_id: int, server_id: int, server_name: str, command: str) -> None:
    """把终端里手敲的一行命令写进审计表。

    终端 WS 的数据库会话早就关了（长连接不该占连接池），这里用短会话写一条
    就关。verdict 记 ``manual``：人工输入没有「白名单判定」这回事，success
    也只是「已送达」，退出码在这条通道上拿不到。
    """
    with SessionLocal() as db:
        Auditor(db, user_id, "server").record(
            target_id=server_id,
            target_name=server_name,
            command=command,
            verdict="manual",
            success=True,
            actor="user",
        )


@router.websocket("/terminal")
async def terminal(
    websocket: WebSocket,
    server_id: int = Query(...),
    ticket: Optional[str] = Query(default=None),
    cols: int = Query(default=80, ge=20, le=500),
    rows: int = Query(default=24, ge=5, le=200),
) -> None:
    """一条 xterm.js ↔ SSH PTY 的双向字节通道。

    客户端发文本帧：普通字符串就是键盘输入，``{"type":"resize",...}`` 是窗口
    变化。服务端只回纯文本，也就是终端输出本身。
    """
    user = await _authorize(websocket, ticket)
    if user is None:
        return

    # PTY 不做命令过滤，开一个就等于把共享资产的 shell 交了出去，
    # 所以它是 ops_write 通行证的第一道闸门。
    if not user.can_ops_write:
        await websocket.close(code=WS_UNAUTHORIZED, reason="没有运维写权限，无法打开终端")
        return

    db = SessionLocal()
    try:
        service = server_ops.OpsServerService(db, user.user_id)
        try:
            server = service.get(server_id)
        except LookupError:
            await websocket.close(code=WS_UNAUTHORIZED, reason="服务器不存在")
            return

        try:
            conn = await server_ops.connect(server, service)
        except server_ops.OpsConnectError as exc:
            logger.warning(
                "terminal handshake failed: user %s, server %s: %s",
                user.user_id,
                server_id,
                exc,
            )
            service.record_check(server, ok=False, message=str(exc))
            await websocket.send_text(f"\r\n\x1b[31m{exc}\x1b[0m\r\n")
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="连接失败")
            return

        service.record_check(server, ok=True)
        # 会话关在后面对象就过期了，先把审计要用的标量取出来。
        audit_target = (server.id, server.name or server.host)
    finally:
        # PTY 会话可能挂上几个小时，不该一直占着连接池里的一个连接。
        db.close()

    try:
        process = await conn.create_process(term_type="xterm-256color", term_size=(cols, rows))
    except Exception as exc:  # pragma: no cover - 依赖真实 SSH 服务端
        logger.warning("failed to open a pty on server %s: %s", server_id, exc)
        await websocket.send_text(f"\r\n\x1b[31m无法打开终端：{exc}\x1b[0m\r\n")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        conn.close()
        return

    tracker = _CommandLineTracker()

    async def pump_out() -> None:
        """SSH → 浏览器。"""
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                break
            tracker.feed_output(chunk)
            await websocket.send_text(chunk)

    async def pump_in() -> None:
        """浏览器 → SSH。"""
        while True:
            message = await websocket.receive_text()
            if message.startswith("{"):
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("type") == "resize":
                    process.change_terminal_size(
                        int(payload.get("cols", cols)), int(payload.get("rows", rows))
                    )
                    continue
            for command in tracker.feed(message):
                _record_manual_command(user.user_id, audit_target[0], audit_target[1], command)
            process.stdin.write(message)

    logger.info("terminal tunnel opened: user %s, server %s", user.user_id, server_id)
    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    try:
        # 任意一边断掉，整条隧道就结束——另一半单独留着没有意义。
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        logger.debug("terminal websocket disconnected: user %s, server %s", user.user_id, server_id)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is None:
                continue
            if isinstance(exc, WebSocketDisconnect):
                logger.debug(
                    "terminal websocket disconnected: user %s, server %s",
                    user.user_id,
                    server_id,
                )
            else:
                logger.warning(
                    "terminal pump task died: user %s, server %s: %s",
                    user.user_id,
                    server_id,
                    exc,
                )
        conn.close()
        logger.info("terminal tunnel closed: user %s, server %s", user.user_id, server_id)
        try:
            await websocket.close()
        except RuntimeError:
            pass
