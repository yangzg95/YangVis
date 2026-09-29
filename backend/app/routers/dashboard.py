"""首页工作台接口：各模块数量的聚合概览。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import APIResponse, CurrentUser, DashboardSummary
from app.services.dashboard import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=APIResponse[DashboardSummary])
async def summary(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[DashboardSummary]:
    """调用方名下的资源数量；管理员额外带上 AI 网关的全局口径。"""
    data = DashboardService(db, user.user_id).summary(is_admin=user.is_admin)
    return APIResponse(data=data)
