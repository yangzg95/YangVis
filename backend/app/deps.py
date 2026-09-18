"""共享的 FastAPI 依赖。

``require_user`` 是身份的唯一入口：每一个需要隔离的资源都从它返回的对象里
取 ``owner_id``，因此任何 handler 都不应该直接从请求里读用户 id。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import SysUser
from app.models.schemas import CurrentUser
from app.security import (
    TokenError,
    decode_access_token,
    decode_ws_ticket,
    extract_bearer_token,
)

logger = logging.getLogger("yangvis.deps")

# 所有认证失败都由 handler 抛出这个异常。提示信息刻意保持统一：区分「token 有误」
# 「用户不存在」「账号被禁用」只会方便别人探测哪些用户名是有效的。
_UNAUTHORIZED = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录已过期")


async def require_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """解析并验证调用方身份。

    数据库查询并不是多余的：无状态的 JWT 无法被撤销，因此在每次请求时重新读取
    ``status`` 字段，是让「禁用账号」在 token 自然过期之前生效的唯一途径。
    """
    try:
        token = extract_bearer_token(authorization)
        payload = decode_access_token(token)
    except TokenError as exc:
        logger.debug("rejecting request: %s", exc)
        raise _UNAUTHORIZED from exc

    # WebSocket 入场票只能用来握手。它走的是 query string、会落进访问日志，
    # 拿它调 REST 接口等于把认证降级到日志可见的强度。
    if payload.get("scope"):
        # 正常用户不会走到这里：票据是从响应体里取的，不会出现在 query 之外。
        # 看到了就是有人在试，记一笔。
        logger.warning("websocket ticket used against a rest endpoint, rejected")
        raise _UNAUTHORIZED

    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError) as exc:
        raise _UNAUTHORIZED from exc

    user = db.get(SysUser, user_id)
    if user is None or not user.status:
        raise _UNAUTHORIZED

    return CurrentUser(
        user_id=user.id,
        username=user.username,
        nickname=user.nickname,
        is_admin=user.is_admin,
        ops_write=user.ops_write,
    )


async def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    """限定某个接口只能由管理员访问。"""
    if not user.is_admin:
        # 普通用户摸到管理员端点：前端没有入口，多半是手动试探，记一笔。
        logger.warning("non-admin user %s hit an admin endpoint, rejected", user.username)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def resolve_ws_user(ticket: Optional[str], db: Session) -> Optional[CurrentUser]:
    """校验一张 WebSocket 入场票，失败返回 ``None``。

    不做成 ``Depends``：WebSocket 里没有「返回 401 响应」这回事，鉴权失败只能
    先 accept 再带着 close code 断开，这个动作得由 handler 自己控制。
    """
    if not ticket:
        return None
    try:
        payload = decode_ws_ticket(ticket)
        user_id = int(payload.get("sub", ""))
    except (TokenError, TypeError, ValueError) as exc:
        logger.debug("rejecting websocket: %s", exc)
        return None

    user = db.get(SysUser, user_id)
    if user is None or not user.status:
        return None

    return CurrentUser(
        user_id=user.id,
        username=user.username,
        nickname=user.nickname,
        is_admin=user.is_admin,
        ops_write=user.ops_write,
    )
