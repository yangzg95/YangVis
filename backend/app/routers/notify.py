"""通知渠道与发送记录的 HTTP 端点。

权限与其他 owner 资源一致：``require_user`` + service 内的 owner 收敛。
测试端点直接返回发送器的结论（成功或失败原因），并把它写回渠道行——前端
据此显示「上次测试 3 分钟前 · 通过」。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    HuaweiChannelCreate,
    HuaweiChannelItem,
    HuaweiChannelUpdate,
    ListResponse,
    NotifyChannelCreate,
    NotifyChannelItem,
    NotifyChannelUpdate,
    NotifyEventInfo,
    NotifyKind,
    NotifyLogItem,
    TestResult,
)
from app.services.notify.events import event_infos
from app.services.notify.service import NotifyChannelService

router = APIRouter(prefix="/notify", tags=["notify"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> NotifyChannelService:
    """把 service 绑到调用方名下，handler 不直接接触 session。"""
    return NotifyChannelService(db, user.user_id)


@router.get("/events", response_model=APIResponse[ListResponse[NotifyEventInfo]])
def list_events(
    _: CurrentUser = Depends(require_user),
) -> APIResponse[ListResponse[NotifyEventInfo]]:
    """可订阅的事件词表：key + 中文说明 + 新建时的默认勾选。"""
    infos = [NotifyEventInfo(**info) for info in event_infos()]
    return APIResponse(data=ListResponse(items=infos, total=len(infos)))


@router.get("/channels", response_model=APIResponse[ListResponse[NotifyChannelItem]])
def list_channels(
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[ListResponse[NotifyChannelItem]]:
    items = [service.to_item(channel) for channel in service.list_channels()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/channels", response_model=APIResponse[NotifyChannelItem])
def create_channel(
    payload: NotifyChannelCreate,
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[NotifyChannelItem]:
    try:
        channel = service.create_channel(payload)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_item(channel))


@router.put("/channels/{channel_id}", response_model=APIResponse[NotifyChannelItem])
def update_channel(
    channel_id: int,
    payload: NotifyChannelUpdate,
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[NotifyChannelItem]:
    try:
        channel = service.update_channel(channel_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_item(channel))


@router.delete("/channels/{channel_id}", response_model=APIResponse[None])
def delete_channel(
    channel_id: int,
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_channel(channel_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")


# -- 华为 HMS Push 渠道 -------------------------------------------------------
#
# 字段与 webhook 渠道完全不同（OAuth 凭据 + 主题/设备目标，没有 URL），单列
# 一组端点；连通测试仍复用上面的 /channels/{id}/test，由 service 内部分流。


@router.post("/channels/huawei", response_model=APIResponse[HuaweiChannelItem])
def create_huawei_channel(
    payload: HuaweiChannelCreate,
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[HuaweiChannelItem]:
    try:
        channel = service.create_huawei_channel(payload)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    cfg = service.get_huawei_config(channel)
    return APIResponse(data=service.to_huawei_item(channel, cfg))


@router.put("/channels/{channel_id}/huawei", response_model=APIResponse[HuaweiChannelItem])
def update_huawei_channel(
    channel_id: int,
    payload: HuaweiChannelUpdate,
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[HuaweiChannelItem]:
    try:
        channel = service.update_huawei_channel(channel_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    cfg = service.get_huawei_config(channel)
    return APIResponse(data=service.to_huawei_item(channel, cfg))


@router.get("/channels/{channel_id}/huawei", response_model=APIResponse[HuaweiChannelItem])
def get_huawei_channel(
    channel_id: int,
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[HuaweiChannelItem]:
    try:
        channel = service.get_channel(channel_id)
        if channel.kind != NotifyKind.HUAWEI.value:
            raise ValueError("该渠道不是华为推送渠道")
        cfg = service.get_huawei_config(channel)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_huawei_item(channel, cfg))


@router.post("/channels/{channel_id}/test", response_model=APIResponse[TestResult])
async def test_channel(
    channel_id: int,
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[TestResult]:
    try:
        channel = service.get_channel(channel_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    result = await service.send_test_message(channel)
    service.record_test(channel, ok=result.ok, error=result.error)
    return APIResponse(
        data=TestResult(success=result.ok, message=result.error or "发送成功，请到群里确认收到")
    )


@router.get("/logs", response_model=APIResponse[ListResponse[NotifyLogItem]])
def list_logs(
    limit: int = Query(default=50, ge=1, le=200),
    service: NotifyChannelService = Depends(get_service),
) -> APIResponse[ListResponse[NotifyLogItem]]:
    """最近的发送记录（含失败），排障「为什么没收到」的第一站。"""
    logs = service.list_logs(limit=limit)
    return APIResponse(data=ListResponse(items=logs, total=len(logs)))
