"""Authentication endpoints backed by the local ``sys_user`` table.

This router used to proxy the upstream noetix platform. Identity is now owned
by this service so that ``owner_id`` — the isolation key for every knowledge
base row — is a value we can vouch for ourselves.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.models.entities import SysUser
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    LoginRequest,
    LoginResult,
    PasswordChangeRequest,
    UserInfo,
)
from app.security import create_access_token, hash_password, verify_password

logger = logging.getLogger("yangvis.auth")
settings = get_settings()

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_user_info(user: SysUser) -> UserInfo:
    return UserInfo(
        user_id=user.id,
        username=user.username,
        nickname=user.nickname,
        email=user.email,
        roles=["admin"] if user.is_admin else ["user"],
        permissions=["ops:write"] if (user.is_admin or user.ops_write) else [],
        ops_write=user.ops_write,
    )


# ---- Endpoints -------------------------------------------------------------

@router.post("/login", response_model=APIResponse[LoginResult])
async def login(payload: LoginRequest, db: Session = Depends(get_db)) -> APIResponse[LoginResult]:
    """Verify credentials against ``sys_user`` and issue an access token."""
    user = db.scalar(select(SysUser).where(SysUser.username == payload.username))

    # Same response for "no such user", "wrong password" and "disabled": a
    # distinct message would turn this endpoint into a username oracle. The
    # hash is still verified when the user is missing so that the timing of a
    # failed login does not reveal whether the account exists.
    if user is None:
        verify_password(payload.password, hash_password("placeholder"))
        return APIResponse(code=401, message="用户名或密码错误")

    if not user.status or not verify_password(payload.password, user.password_hash):
        return APIResponse(code=401, message="用户名或密码错误")

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    token = create_access_token(
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
    )
    logger.info("user %s (%s) logged in", user.username, user.id)
    return APIResponse(data=LoginResult(access_token=token, user_info=_to_user_info(user)))


@router.get("/me", response_model=APIResponse[UserInfo])
async def current_user(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[UserInfo]:
    """Return the caller's profile, resolved from the token."""
    record = db.get(SysUser, user.user_id)
    if record is None:
        return APIResponse(code=401, message="未登录")
    return APIResponse(data=_to_user_info(record))


@router.post("/logout", response_model=APIResponse[None])
async def logout() -> APIResponse[None]:
    """Log out.

    Tokens are stateless, so this only exists for the client to call before it
    drops its local copy. Immediate revocation would need a deny list, which is
    not worth the moving parts at this scale.
    """
    return APIResponse(data=None, message="已退出登录")


@router.post("/password", response_model=APIResponse[None])
async def change_password(
    payload: PasswordChangeRequest,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[None]:
    """Change your own password."""
    record = db.get(SysUser, user.user_id)
    if record is None:
        return APIResponse(code=401, message="未登录")

    if not verify_password(payload.old_password, record.password_hash):
        return APIResponse(code=-1, message="原密码不正确")

    if payload.old_password == payload.new_password:
        return APIResponse(code=-1, message="新密码不能与原密码相同")

    record.password_hash = hash_password(payload.new_password)
    db.commit()
    logger.info("user %s changed their password", record.username)

    # Existing tokens stay valid until they expire; forcing a re-login would
    # need the deny list mentioned in logout().
    return APIResponse(data=None, message="密码已更新")
