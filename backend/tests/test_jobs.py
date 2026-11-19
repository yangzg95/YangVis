"""招聘岗位缓存：用户隔离、幂等 upsert、组合筛选与 HTTP 契约。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import JobPosting
from app.models.schemas import CurrentUser, JobFilter
from app.routers.jobs import get_boss_source
from app.services.job_sources.boss import BossJob, BossSearchResult, BossSourceError, build_apply_url
from app.services.jobs import JobService

OWNER = 1
OTHER = 2


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client_factory(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db

    def make_client(owner_id=OWNER, source=None):
        app.dependency_overrides[require_user] = lambda: CurrentUser(
            user_id=owner_id, username=f"user{owner_id}"
        )
        if source is not None:
            app.dependency_overrides[get_boss_source] = lambda: source
        return TestClient(app)

    try:
        yield make_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def service(db):
    return JobService(db, OWNER)


def make_job(external_id="job-1", **overrides):
    values = {
        "external_id": external_id,
        "title": "Python 后端工程师",
        "company": "示例科技",
        "city": "北京",
        "district": "海淀区",
        "salary_text": "10-20K",
        "salary_min": 10.0,
        "salary_max": 20.0,
        "experience": "3-5年",
        "education": "本科",
        "skills": ["Python"],
        "recruiter": "张经理",
        "published_at": datetime.now() - timedelta(days=1),
        "apply_url": build_apply_url(external_id),
    }
    values.update(overrides)
    return BossJob(**values)


def test_save_jobs_is_idempotent_and_updates_snapshot(service, db):
    created, updated = service.save_jobs([make_job()])
    assert (created, updated) == (1, 0)
    first = db.scalar(select(JobPosting))

    created, updated = service.save_jobs([make_job(company="新公司", salary_text="20-30K", salary_min=20, salary_max=30)])
    assert (created, updated) == (0, 1)
    records = list(db.scalars(select(JobPosting)).all())
    assert len(records) == 1
    assert records[0].company == "新公司"
    assert records[0].salary_text == "20-30K"
    assert records[0].first_seen_at == first.first_seen_at
    assert records[0].last_seen_at >= first.last_seen_at


def test_combined_filters_and_owner_isolation(service, db):
    service.save_jobs(
        [
            make_job("match"),
            make_job("other-city", city="上海"),
            make_job("old", published_at=datetime.now() - timedelta(days=10)),
            make_job("unknown-time", published_at=None),
            make_job("low-salary", salary_min=5, salary_max=8, salary_text="5-8K"),
            make_job("negotiable", salary_min=None, salary_max=None, salary_text="面议"),
        ]
    )
    JobService(db, OTHER).save_jobs([make_job("other-owner")])

    filters = JobFilter(
        keyword="python", city="北京", posted_days=7, salary_min=15, salary_max=25
    )
    records, total = service.list_records(filters)
    assert total == 1
    assert [record.external_id for record in records] == ["match"]
    other_records, other_total = JobService(db, OTHER).list_records(filters)
    assert other_total == 1
    assert [record.external_id for record in other_records] == ["other-owner"]


def test_list_paginates_and_keeps_unknown_publish_time_last(service):
    service.save_jobs(
        [
            make_job("known", published_at=datetime.now()),
            make_job("unknown", published_at=None),
        ]
    )
    records, total = service.list_records(JobFilter(), page=1, page_size=1)
    assert total == 2
    assert [record.external_id for record in records] == ["known"]
    records, _ = service.list_records(JobFilter(), page=2, page_size=1)
    assert [record.external_id for record in records] == ["unknown"]


def test_list_http_uses_same_filters(client_factory, service):
    service.save_jobs([make_job(), make_job("shanghai", city="上海")])
    client = client_factory()
    body = client.get(
        "/api/office/jobs",
        params={"keyword": "Python", "city": "北京", "posted_days": 7, "salary_min": 15},
    ).json()
    assert body["code"] == 0, body
    assert body["data"]["total"] == 1
    item = body["data"]["items"][0]
    assert item["title"] == "Python 后端工程师"
    assert item["apply_url"] == "https://www.zhipin.com/job_detail/job-1.html"
    assert item["skills"] == ["Python"]


class FakeSource:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.payload = None

    async def search(self, payload):
        self.payload = payload
        if self.error:
            raise self.error
        return self.result


def test_fetch_http_saves_and_reports_counts(client_factory):
    source = FakeSource(BossSearchResult(jobs=[make_job()], fetched=3))
    client = client_factory(source=source)
    response = client.post(
        "/api/office/jobs/fetch",
        json={"keyword": "Python", "city": "北京", "posted_days": 7, "salary_min": 10},
    )
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "抓取完成"
    assert body["data"]["fetched"] == 3
    assert body["data"]["matched"] == 1
    assert body["data"]["created"] == 1
    assert body["data"]["updated"] == 0
    assert source.payload.keyword == "Python"
    assert client.get("/api/office/jobs").json()["data"]["total"] == 1


def test_fetch_failure_preserves_cache(client_factory, service):
    service.save_jobs([make_job()])
    source = FakeSource(error=BossSourceError("BOSS 直聘暂时限制了访问，请稍后重试"))
    client = client_factory(source=source)
    body = client.post("/api/office/jobs/fetch", json={"city": "北京"}).json()
    assert body["code"] == 4301
    assert "暂时限制" in body["message"]
    assert client.get("/api/office/jobs").json()["data"]["total"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"city": "   "},
        {"salary_min": 30, "salary_max": 20},
        {"posted_days": 2},
        {"salary_min": -1},
    ],
)
def test_invalid_filters_are_422(client_factory, payload):
    client = client_factory()
    assert client.post("/api/office/jobs/fetch", json=payload).status_code == 422
    assert client.get("/api/office/jobs", params=payload).status_code == 422
