"""智能办公 · 百度网盘绑定接口。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.errors import BusinessError
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    NetdiskAuthUrl,
    NetdiskBindRequest,
    NetdiskSaveTextRequest,
    NetdiskSaveTextResult,
    NetdiskStatus,
)
from app.services.netdisk import NetdiskService

logger = logging.getLogger("yangvis.netdisk")

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
    user: CurrentUser = Depends(require_user),
) -> APIResponse[NetdiskStatus]:
    """用授权码完成绑定。已绑定过时相当于换新令牌重新绑定。"""
    try:
        account = await service.bind(payload.code)
    except BusinessError:
        # 失败原因已在服务层记过（errno/HTTP）；这里补一笔「谁的绑定没成」。
        logger.warning("netdisk bind failed for user %s", user.user_id)
        raise
    # 绑定关系是安全相关事件，始终留痕。
    logger.info(
        "user %s bound netdisk account %s", user.user_id, account.baidu_name or account.baidu_uid
    )
    return APIResponse(data=service.status_view(), message="绑定成功")


@router.post("/save-text", response_model=APIResponse[NetdiskSaveTextResult])
async def save_text_to_netdisk(
    payload: NetdiskSaveTextRequest,
    service: NetdiskService = Depends(get_service),
    user: CurrentUser = Depends(require_user),
) -> APIResponse[NetdiskSaveTextResult]:
    """把一段文本（分析报告等）存进网盘的 ``reports/`` 子目录。

    内容本身由调用方组好（前端报告抽屉的 markdown 全文）；路径段清洗
    在 ``_remote_path`` 里做，filename 里的 ``/`` 之类进不了最终路径。
    """
    _, path = await service.upload_file(
        f"reports/{payload.filename}", payload.content.encode("utf-8")
    )
    logger.info("user %s saved a text file to netdisk: %s", user.user_id, path)
    return APIResponse(data=NetdiskSaveTextResult(path=path), message="已保存到网盘")


@router.post("/unbind", response_model=APIResponse[None])
async def unbind_netdisk(
    service: NetdiskService = Depends(get_service),
    user: CurrentUser = Depends(require_user),
) -> APIResponse[None]:
    """解除绑定。只删本地凭据，网盘里已同步的文件原样保留。"""
    service.unbind()
    logger.info("user %s unbound their netdisk account", user.user_id)
    return APIResponse(data=None, message="已解绑")
