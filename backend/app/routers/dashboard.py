"""首页工作台接口：各模块数量的聚合概览。"""
from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import APIResponse, CurrentUser, DashboardSummary
from app.services.dashboard import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# 概览要串行跑十来条 count，而首页每次刷新都会调它。数字本身没有「实时」要求，
# 缓存一小段时间，把轮询与反复回首页的开销压成一次查询。
_SUMMARY_TTL = 20.0
_summary_cache: Dict[Tuple[int, bool], Tuple[float, DashboardSummary]] = {}
_cache_lock = threading.Lock()


@router.get("/summary", response_model=APIResponse[DashboardSummary])
def summary(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[DashboardSummary]:
    """调用方名下的资源数量；管理员额外带上 AI 网关的全局口径。"""
    key = (user.user_id, user.is_admin)
    now = time.monotonic()
    with _cache_lock:
        cached = _summary_cache.get(key)
        if cached is not None and now - cached[0] < _SUMMARY_TTL:
            return APIResponse(data=cached[1])

    data = DashboardService(db, user.user_id).summary(is_admin=user.is_admin)
    with _cache_lock:
        _summary_cache[key] = (now, data)
    return APIResponse(data=data)
