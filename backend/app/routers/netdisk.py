"""智能办公 · 百度网盘绑定接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    NetdiskAuthUrl,
    NetdiskBindRequest,
    NetdiskStatus,
)
from app.services.netdisk import NetdiskService

router = APIRouter(prefix="/office/netdisk", tags=["office"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> NetdiskService:
    return NetdiskService(db, user.user_id)


@router.get("/status", response_model=APIResponse[NetdiskStatus])
async def netdisk_status(
    service: NetdiskService = Depends(get_service),
) -> APIResponse[NetdiskStatus]:
    """绑定状态。configured=false 表示后端未配置 AppKey，前端隐藏整个入口。"""
    return APIResponse(data=service.status_view())


@router.get("/auth-url", response_model=APIResponse[NetdiskAuthUrl])
async def netdisk_auth_url(
    service: NetdiskService = Depends(get_service),
) -> APIResponse[NetdiskAuthUrl]:
    """百度 OAuth 授权页地址，前端在新窗口打开。"""
    return APIResponse(data=NetdiskAuthUrl(url=service.auth_url()))


@router.post("/bind", response_model=APIResponse[NetdiskStatus])
async def bind_netdisk(
    payload: NetdiskBindRequest,
    service: NetdiskService = Depends(get_service),
) -> APIResponse[NetdiskStatus]:
    """用授权码完成绑定。已绑定过时相当于换新令牌重新绑定。"""
    await service.bind(payload.code)
    return APIResponse(data=service.status_view(), message="绑定成功")


@router.post("/unbind", response_model=APIResponse[None])
async def unbind_netdisk(
    service: NetdiskService = Depends(get_service),
) -> APIResponse[None]:
    """解除绑定。只删本地凭据，网盘里已同步的文件原样保留。"""
    service.unbind()
    return APIResponse(data=None, message="已解绑")
