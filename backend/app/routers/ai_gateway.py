"""AI 网关的管理接口（仅管理员）。

网关是平台级共享资产：通道、对外密钥、调用日志都由管理员统一维护，普通用户
看不到这些端点。对外的 OpenAI 兼容端点在 :mod:`app.routers.ai_gateway_openai`。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.crypto import DecryptionError, decrypt
from app.database import get_db
from app.deps import require_admin
from app.models.schemas import (
    APIResponse,
    AiApiKeyCreate,
    AiApiKeyCreated,
    AiApiKeyItem,
    AiApiKeyUpdate,
    AiCallLogDetail,
    AiCallLogItem,
    AiChannelCreate,
    AiChannelItem,
    AiChannelTestRequest,
    AiChannelUpdate,
    AiGatewayOverview,
    AiLogPurgeRequest,
    AiLogPurgeResult,
    AiModelRouteCreate,
    AiModelRouteItem,
    AiModelRouteUpdate,
    AiStats,
    ListResponse,
    TestResult,
)
from app.services import ai_gateway_proxy as proxy
from app.services.ai_gateway import AiGatewayService

logger = logging.getLogger("yangvis.ai_gateway.admin")

router = APIRouter(
    prefix="/ai-gateway",
    tags=["ai-gateway"],
    dependencies=[Depends(require_admin)],
)


def get_service(db: Session = Depends(get_db)) -> AiGatewayService:
    return AiGatewayService(db)


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---- 概览 -------------------------------------------------------------------


@router.get("/overview", response_model=APIResponse[AiGatewayOverview])
async def overview(
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiGatewayOverview]:
    return APIResponse(data=service.overview())


# ---- 上游通道 ---------------------------------------------------------------


@router.get("/channels", response_model=APIResponse[ListResponse[AiChannelItem]])
async def list_channels(
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[ListResponse[AiChannelItem]]:
    items = [service.channel_item(channel) for channel in service.list_channels()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/channels", response_model=APIResponse[AiChannelItem])
async def create_channel(
    payload: AiChannelCreate,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiChannelItem]:
    channel = service.create_channel(payload)
    return APIResponse(data=service.channel_item(channel))


@router.put("/channels/{channel_id}", response_model=APIResponse[AiChannelItem])
async def update_channel(
    channel_id: int,
    payload: AiChannelUpdate,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiChannelItem]:
    try:
        channel = service.update_channel(channel_id, payload)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=service.channel_item(channel))


@router.delete("/channels/{channel_id}", response_model=APIResponse[None])
async def delete_channel(
    channel_id: int,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_channel(channel_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=None, message="deleted")


@router.post("/channels/{channel_id}/test", response_model=APIResponse[TestResult])
async def test_channel(
    channel_id: int,
    payload: AiChannelTestRequest,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[TestResult]:
    """真发一条最短的 prompt 验证 base_url + api_key + model 能通。"""
    try:
        channel = service.get_channel(channel_id)
    except LookupError as exc:
        raise _not_found(exc) from exc

    model = (payload.model or "").strip() or ((channel.models or [None])[0])
    if not model:
        return APIResponse(
            data=TestResult(success=False, message="请先填写该通道支持的模型名，或在此指定 model")
        )

    try:
        api_key = decrypt(channel.api_key_enc)
    except DecryptionError as exc:
        return APIResponse(data=TestResult(success=False, message=str(exc)))

    ok, message = await proxy.test_channel(channel.base_url, api_key, model)
    service.record_channel_test(channel, ok=ok, message=message)
    return APIResponse(data=TestResult(success=ok, message=message))


# ---- 模型路由 ---------------------------------------------------------------


@router.get("/routes", response_model=APIResponse[ListResponse[AiModelRouteItem]])
async def list_routes(
    model_name: Optional[str] = Query(default=None),
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[ListResponse[AiModelRouteItem]]:
    items = service.list_routes(model_name)
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/routes", response_model=APIResponse[AiModelRouteItem])
async def create_route(
    payload: AiModelRouteCreate,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiModelRouteItem]:
    try:
        return APIResponse(data=service.create_route(payload))
    except LookupError as exc:
        raise _not_found(exc) from exc


@router.put("/routes/{route_id}", response_model=APIResponse[AiModelRouteItem])
async def update_route(
    route_id: int,
    payload: AiModelRouteUpdate,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiModelRouteItem]:
    try:
        return APIResponse(data=service.update_route(route_id, payload))
    except LookupError as exc:
        raise _not_found(exc) from exc


@router.delete("/routes/{route_id}", response_model=APIResponse[None])
async def delete_route(
    route_id: int,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_route(route_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=None, message="deleted")


# ---- 统一密钥 ---------------------------------------------------------------


@router.get("/keys", response_model=APIResponse[ListResponse[AiApiKeyItem]])
async def list_keys(
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[ListResponse[AiApiKeyItem]]:
    items = [service.key_item(row) for row in service.list_keys()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/keys", response_model=APIResponse[AiApiKeyCreated])
async def create_key(
    payload: AiApiKeyCreate,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiApiKeyCreated]:
    row, plain = service.create_key(payload.name, payload.remark)
    # 明文只在这一次响应里出现；数据库里只有摘要，之后谁都取不回来。
    return APIResponse(data=AiApiKeyCreated(item=service.key_item(row), api_key=plain))


@router.put("/keys/{key_id}", response_model=APIResponse[AiApiKeyItem])
async def update_key(
    key_id: int,
    payload: AiApiKeyUpdate,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiApiKeyItem]:
    try:
        row = service.update_key(key_id, payload)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=service.key_item(row))


@router.delete("/keys/{key_id}", response_model=APIResponse[None])
async def delete_key(
    key_id: int,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_key(key_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=None, message="deleted")


# ---- 监控与审计 -------------------------------------------------------------


@router.get("/stats", response_model=APIResponse[AiStats])
async def stats(
    days: int = Query(default=7, ge=1, le=90),
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiStats]:
    return APIResponse(data=service.stats(days))


@router.get("/logs", response_model=APIResponse[ListResponse[AiCallLogItem]])
async def list_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    key_id: Optional[int] = Query(default=None),
    channel_id: Optional[int] = Query(default=None),
    model: Optional[str] = Query(default=None),
    endpoint: Optional[str] = Query(default=None),
    success: Optional[bool] = Query(default=None),
    keyword: Optional[str] = Query(default=None, max_length=128),
    start: Optional[datetime] = Query(default=None),
    end: Optional[datetime] = Query(default=None),
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[ListResponse[AiCallLogItem]]:
    items, total = service.list_logs(
        page=page,
        page_size=page_size,
        key_id=key_id,
        channel_id=channel_id,
        model=model,
        endpoint=endpoint,
        success=success,
        keyword=keyword,
        start=start,
        end=end,
    )
    return APIResponse(data=ListResponse(items=items, total=total))


@router.get("/logs/{log_id}", response_model=APIResponse[AiCallLogDetail])
async def log_detail(
    log_id: int,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiCallLogDetail]:
    try:
        row = service.get_log(log_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return APIResponse(data=AiCallLogDetail.model_validate(row))


@router.post("/logs/purge", response_model=APIResponse[AiLogPurgeResult])
async def purge_logs(
    payload: AiLogPurgeRequest,
    service: AiGatewayService = Depends(get_service),
) -> APIResponse[AiLogPurgeResult]:
    deleted = service.purge_logs(payload.before_days)
    return APIResponse(data=AiLogPurgeResult(deleted=deleted), message=f"已清理 {deleted} 条")
