"""智能办公 · 外部招聘岗位抓取与缓存列表。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.errors import CODE_JOB_SOURCE_ERROR, BusinessError
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    JobFetchResult,
    JobFilter,
    JobItem,
    ListResponse,
)
from app.services.job_sources.boss import BossJobSource, BossSourceError
from app.services.jobs import JobService

router = APIRouter(prefix="/office", tags=["office"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> JobService:
    return JobService(db, user.user_id)


def get_boss_source() -> BossJobSource:
    return BossJobSource()


def get_filters(
    keyword: str = Query(default="", max_length=100),
    city: str = Query(default="全国", min_length=1, max_length=32),
    posted_days: int | None = Query(default=None),
    salary_min: float | None = Query(default=None, ge=0, le=1000),
    salary_max: float | None = Query(default=None, ge=0, le=1000),
) -> JobFilter:
    """查询模型的跨字段错误也统一走 HTTP 422，而不是漏成未处理异常。"""
    city = city.strip()
    if not city or posted_days not in (None, 1, 3, 7, 30):
        raise HTTPException(status_code=422, detail="请求参数不合法")
    if salary_min is not None and salary_max is not None and salary_min > salary_max:
        raise HTTPException(status_code=422, detail="请求参数不合法")
    return JobFilter(
        keyword=keyword,
        city=city,
        posted_days=posted_days,
        salary_min=salary_min,
        salary_max=salary_max,
    )


@router.get("/jobs", response_model=APIResponse[ListResponse[JobItem]])
def list_jobs(
    filters: JobFilter = Depends(get_filters),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: JobService = Depends(get_service),
) -> APIResponse[ListResponse[JobItem]]:
    records, total = service.list_records(filters, page=page, page_size=page_size)
    return APIResponse(
        data=ListResponse(
            items=[service.to_item(record) for record in records],
            total=total,
        )
    )


@router.post("/jobs/fetch", response_model=APIResponse[JobFetchResult])
async def fetch_jobs(
    payload: JobFilter,
    service: JobService = Depends(get_service),
    source: BossJobSource = Depends(get_boss_source),
) -> APIResponse[JobFetchResult]:
    try:
        result = await source.search(payload)
    except BossSourceError as exc:
        raise BusinessError(CODE_JOB_SOURCE_ERROR, str(exc)) from exc
    created, updated = service.save_jobs(result.jobs)
    return APIResponse(
        data=JobFetchResult(
            fetched=result.fetched,
            matched=len(result.jobs),
            created=created,
            updated=updated,
            fetched_at=datetime.now(),
        ),
        message="抓取完成",
    )
