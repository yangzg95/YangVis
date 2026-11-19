"""当前用户的招聘岗位缓存、去重与组合筛选。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.entities import JobPosting
from app.models.schemas import JobFilter, JobItem
from app.services.job_sources.boss import BossJob

logger = logging.getLogger("yangvis.jobs")

_LIKE_ESCAPE = str.maketrans({"/": "//", "%": "/%", "_": "/_"})


class JobService:
    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    def list_records(
        self, filters: JobFilter, *, page: int = 1, page_size: int = 20
    ) -> tuple[list[JobPosting], int]:
        stmt = self._filtered(select(JobPosting), filters)
        total = self._db.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ) or 0
        stmt = stmt.order_by(
            JobPosting.published_at.is_(None).asc(),
            JobPosting.published_at.desc(),
            JobPosting.last_seen_at.desc(),
            JobPosting.id.desc(),
        ).offset((page - 1) * page_size).limit(page_size)
        return list(self._db.scalars(stmt).all()), int(total)

    def save_jobs(self, jobs: list[BossJob]) -> tuple[int, int]:
        """按外部岗位 ID 幂等写入；重复抓取只更新快照与最近发现时间。"""
        if not jobs:
            return 0, 0
        external_ids = [job.external_id for job in jobs]
        existing = {
            record.external_id: record
            for record in self._db.scalars(
                select(JobPosting).where(
                    JobPosting.owner_id == self._owner_id,
                    JobPosting.source == "boss",
                    JobPosting.external_id.in_(external_ids),
                )
            ).all()
        }
        now = datetime.now()
        created = 0
        updated = 0
        try:
            for job in jobs:
                record = existing.get(job.external_id)
                if record is None:
                    record = JobPosting(
                        owner_id=self._owner_id,
                        source="boss",
                        external_id=job.external_id,
                        first_seen_at=now,
                    )
                    self._db.add(record)
                    existing[job.external_id] = record
                    created += 1
                else:
                    updated += 1
                self._apply_snapshot(record, job)
                record.last_seen_at = now
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        logger.info(
            "owner %s cached BOSS jobs: created=%s updated=%s",
            self._owner_id,
            created,
            updated,
        )
        return created, updated

    def _filtered(self, stmt: Select, filters: JobFilter) -> Select:
        stmt = stmt.where(JobPosting.owner_id == self._owner_id)
        keyword = filters.keyword.strip()
        if keyword:
            term = f"%{keyword.translate(_LIKE_ESCAPE)}%"
            stmt = stmt.where(
                or_(
                    JobPosting.title.ilike(term, escape="/"),
                    JobPosting.company.ilike(term, escape="/"),
                )
            )
        if filters.city != "全国":
            stmt = stmt.where(JobPosting.city == filters.city)
        if filters.posted_days is not None:
            cutoff = datetime.now() - timedelta(days=filters.posted_days)
            stmt = stmt.where(
                JobPosting.published_at.is_not(None), JobPosting.published_at >= cutoff
            )
        if filters.salary_min is not None or filters.salary_max is not None:
            # 面议和无法规范化的日薪/时薪不参与数值薪资筛选。
            stmt = stmt.where(
                or_(JobPosting.salary_min.is_not(None), JobPosting.salary_max.is_not(None))
            )
        if filters.salary_min is not None:
            stmt = stmt.where(
                or_(
                    JobPosting.salary_max.is_(None),
                    JobPosting.salary_max >= filters.salary_min,
                )
            )
        if filters.salary_max is not None:
            stmt = stmt.where(
                or_(
                    JobPosting.salary_min.is_(None),
                    JobPosting.salary_min <= filters.salary_max,
                )
            )
        return stmt

    @staticmethod
    def _apply_snapshot(record: JobPosting, job: BossJob) -> None:
        record.title = job.title
        record.company = job.company
        record.city = job.city
        record.district = job.district
        record.salary_text = job.salary_text
        record.salary_min = job.salary_min
        record.salary_max = job.salary_max
        record.experience = job.experience
        record.education = job.education
        record.skills = list(job.skills)
        record.recruiter = job.recruiter
        record.published_at = job.published_at
        record.apply_url = job.apply_url

    @staticmethod
    def to_item(record: JobPosting) -> JobItem:
        return JobItem.model_validate(record)
