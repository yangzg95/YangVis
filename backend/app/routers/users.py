"""仅限管理员的账号管理。

系统不提供公开注册：账号要么由管理员在这里创建，要么在用户表为空时
由 ``main`` 里的初始化流程创建。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_admin
from app.models.entities import SysUser
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    PasswordResetRequest,
    UserCreateRequest,
    UserItem,
    UserStatusRequest,
    UserUpdateRequest,
)
from app.security import hash_password

logger = logging.getLogger("yangvis.users")

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_admin)])


@router.get("", response_model=APIResponse[ListResponse[UserItem]])
async def list_users(db: Session = Depends(get_db)) -> APIResponse[ListResponse[UserItem]]:
    """列出所有账号，最新创建的排在前面。"""
    records = db.scalars(select(SysUser).order_by(SysUser.id.desc())).all()
    items = [UserItem.model_validate(record) for record in records]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("", response_model=APIResponse[UserItem])
async def create_user(
    payload: UserCreateRequest,
    db: Session = Depends(get_db),
) -> APIResponse[UserItem]:
    """创建一个账号。"""
    existing = db.scalar(select(SysUser).where(SysUser.username == payload.username))
    if existing is not None:
        return APIResponse(code=-1, message="用户名已存在")

    user = SysUser(
        username=payload.username,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname or payload.username,
        email=payload.email,
        status=True,
        is_admin=payload.is_admin,
        ops_write=payload.ops_write,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("created user %s (admin=%s)", user.username, user.is_admin)
    return APIResponse(data=UserItem.model_validate(user))


@router.put("/{user_id}", response_model=APIResponse[UserItem])
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> APIResponse[UserItem]:
    """修改账号资料。"""
    user = db.get(SysUser, user_id)
    if user is None:
        return APIResponse(code=-1, message="用户不存在")

    # 只写回显式传过来的字段，这样改昵称不会顺手把邮箱清掉。
    fields = payload.model_dump(exclude_unset=True)

    # 和「不能禁用自己」同理：把自己降级会留下一个可能没有管理员的实例。
    if fields.get("is_admin") is False and user.id == admin.user_id:
        return APIResponse(code=-1, message="不能取消当前登录账号的管理员身份")

    for name, value in fields.items():
        setattr(user, name, value)

    db.commit()
    db.refresh(user)
    logger.info("updated user %s (%s)", user.username, ", ".join(fields) or "no change")
    return APIResponse(data=UserItem.model_validate(user))


@router.put("/{user_id}/status", response_model=APIResponse[UserItem])
async def update_user_status(
    user_id: int,
    payload: UserStatusRequest,
    admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> APIResponse[UserItem]:
    """启用或禁用一个账号。

    禁用会在下一次请求时生效：``require_user`` 每次都会重新从数据库读取
    ``status``。
    """
    user = db.get(SysUser, user_id)
    if user is None:
        return APIResponse(code=-1, message="用户不存在")

    # Locking yourself out would leave the instance with no way back in.
    if user.id == admin.user_id and not payload.status:
        return APIResponse(code=-1, message="不能禁用当前登录的账号")

    user.status = payload.status
    db.commit()
    db.refresh(user)
    logger.info("user %s status set to %s", user.username, user.status)
    return APIResponse(data=UserItem.model_validate(user))


@router.post("/{user_id}/password", response_model=APIResponse[None])
async def reset_user_password(
    user_id: int,
    payload: PasswordResetRequest,
    db: Session = Depends(get_db),
) -> APIResponse[None]:
    """Reset someone else's password without knowing the old one."""
    user = db.get(SysUser, user_id)
    if user is None:
        return APIResponse(code=-1, message="用户不存在")

    user.password_hash = hash_password(payload.new_password)
    db.commit()
    logger.info("password reset for user %s", user.username)
    return APIResponse(data=None, message="密码已重置")
