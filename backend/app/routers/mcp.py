"""MCP 服务接口：管理可挂载到工作流上的工具服务。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    McpExportResponse,
    McpImportReport,
    McpImportRequest,
    McpServerCreate,
    McpServerItem,
    McpServerUpdate,
    TestResult,
)
from app.services.mcp import McpServerService, export_document
from app.services.mcp.tools import test_connection

logger = logging.getLogger("yangvis.mcp")

router = APIRouter(prefix="/mcp", tags=["mcp"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> McpServerService:
    return McpServerService(db, user.user_id)


@router.get("", response_model=APIResponse[ListResponse[McpServerItem]])
def list_mcp_servers(
    service: McpServerService = Depends(get_service),
) -> APIResponse[ListResponse[McpServerItem]]:
    """当前用户配置的全部 MCP 服务。"""
    items = [service.to_item(server) for server in service.list()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("", response_model=APIResponse[McpServerItem])
def create_mcp_server(
    payload: McpServerCreate,
    service: McpServerService = Depends(get_service),
) -> APIResponse[McpServerItem]:
    try:
        server = service.create(payload)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_item(server))


@router.get("/export", response_model=APIResponse[McpExportResponse])
def export_mcp_config(
    ids: Optional[List[int]] = Query(default=None),
    service: McpServerService = Depends(get_service),
) -> APIResponse[McpExportResponse]:
    """导出 mcpServers 标准文档：不传 ids 导出全部，传了只导选中条。

    文档内含明文环境变量/请求头，前端会先警告。
    """
    try:
        servers = service.export_rows(ids)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    document, warnings = export_document(servers)
    return APIResponse(data=McpExportResponse(**document, warnings=warnings))


@router.post("/import", response_model=APIResponse[McpImportReport])
def import_mcp_config(
    payload: McpImportRequest,
    service: McpServerService = Depends(get_service),
) -> APIResponse[McpImportReport]:
    """导入 mcpServers 文档批量创建；重名按 conflict 策略处理。"""
    try:
        report = service.import_config(payload.config, conflict=payload.conflict)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=report)


@router.get("/{server_id}", response_model=APIResponse[McpServerItem])
def get_mcp_server(
    server_id: int,
    service: McpServerService = Depends(get_service),
) -> APIResponse[McpServerItem]:
    try:
        server = service.require(server_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_item(server))


@router.put("/{server_id}", response_model=APIResponse[McpServerItem])
def update_mcp_server(
    server_id: int,
    payload: McpServerUpdate,
    service: McpServerService = Depends(get_service),
) -> APIResponse[McpServerItem]:
    try:
        server = service.update(server_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_item(server))


@router.delete("/{server_id}", response_model=APIResponse[None])
def delete_mcp_server(
    server_id: int,
    service: McpServerService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete(server_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")


@router.post("/{server_id}/test", response_model=APIResponse[TestResult])
async def test_mcp_server(
    server_id: int,
    service: McpServerService = Depends(get_service),
) -> APIResponse[TestResult]:
    """连通性测试：真正建立连接并 initialize，回传发现的工具数或失败原因。"""
    try:
        server = service.require(server_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    ok, message, _count = await test_connection(server)
    service.record_test(server, ok=ok, message=message)
    return APIResponse(data=TestResult(success=ok, message=message))
