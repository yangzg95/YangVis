"""移动 App（yangvis-app）拉取历史通知的只读端点。

与 :mod:`app.routers.notify` 的关键区别：这套接口不给 Web 控制台的登录用户用，
而是给手机 App 用，鉴权靠一个长期静态令牌（环境变量 ``NOTIFY_APP_TOKEN``），
不走带图形验证码的登录流，也不签发 JWT。

安全约定：

- 令牌留空时端点直接回 404 而不是 401——不对外暴露这个功能的存在；
- 令牌比对走 :func:`hmac.compare_digest`，避免时序侧信道；
- 只返回 ``status=sent`` 的留痕：发送失败的通知没到过手机，不该出现在历史里；
- 归属范围由 ``NOTIFY_APP_OWNER_ID`` 限定；留空则全站可见，仅适用单用户自用部署。
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.entities import NotifyLog
from app.models.schemas import APIResponse, AppMessageItem, ListResponse
from app.security import TokenError, extract_bearer_token
from app.services.notify.dispatch import STATUS_SENT

logger = logging.getLogger("yangvis.app_messages")

router = APIRouter(prefix="/app/v1", tags=["app-messages"])

_UNAUTHORIZED = HTTPException(status_code=401, detail="未授权")


def require_app_token(authorization: Optional[str] = Header(default=None)) -> None:
    """校验 App 的静态 Bearer 令牌。未配置令牌时把端点藏起来（404）。"""
    expected = get_settings().NOTIFY_APP_TOKEN
    if not expected:
        raise HTTPException(status_code=404, detail="接口不存在")
    try:
        token = extract_bearer_token(authorization)
    except TokenError as exc:
        logger.debug("rejecting app request: %s", exc)
        raise _UNAUTHORIZED from exc
    if not hmac.compare_digest(token, expected):
        logger.debug("rejecting app request: token mismatch")
        raise _UNAUTHORIZED


@router.get("/messages", response_model=APIResponse[ListResponse[AppMessageItem]])
def list_messages(
    limit: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_app_token),
    db: Session = Depends(get_db),
) -> APIResponse[ListResponse[AppMessageItem]]:
    """最近成功送达的通知，倒序返回，供 App 的历史消息页渲染。"""
    stmt = select(NotifyLog).where(NotifyLog.status == STATUS_SENT)
    owner_id = get_settings().NOTIFY_APP_OWNER_ID
    if owner_id is not None:
        stmt = stmt.where(NotifyLog.owner_id == owner_id)
    stmt = stmt.order_by(NotifyLog.id.desc()).limit(limit)

    rows = db.scalars(stmt).all()
    items = [AppMessageItem.model_validate(row) for row in rows]
    return APIResponse(data=ListResponse(items=items, total=len(items)))
