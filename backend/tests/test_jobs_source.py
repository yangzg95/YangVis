"""BOSS 来源适配器：响应解析、发布时间/薪资边界与失败映射。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

from app.models.schemas import JobFilter
from app.services.job_sources.boss import (
    BossJobSource,
    BossSourceError,
    build_apply_url,
    parse_salary,
)

NOW = datetime(2026, 11, 18, 12, 0, 0)


def raw_job(**overrides):
    item = {
        "encryptJobId": "abc_DEF-123",
        "jobName": "Python 后端工程师",
        "brandName": "示例科技",
        "cityName": "北京",
        "areaDistrict": "海淀区",
        "salaryDesc": "15-30K·14薪",
        "jobExperience": "3-5年",
        "jobDegree": "本科",
        "skills": ["Python", "FastAPI"],
        "bossName": "张经理",
        "bossTitle": "招聘负责人",
        "publishTime": (NOW - timedelta(days=1)).isoformat(),
    }
    item.update(overrides)
    return item


def response_for(items, *, status=200):
    return httpx.Response(
        status,
        json={"code": 0, "message": "Success", "zpData": {"jobList": items}},
    )


def run_search(items, filters=None, *, handler=None):
    def default_handler(request):
        return response_for(items)

    transport = httpx.MockTransport(handler or default_handler)
    source = BossJobSource(transport=transport)
    return asyncio.run(source.search(filters or JobFilter(), now=NOW))


def test_search_parses_fields_and_sends_whitelisted_filters():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return response_for([raw_job()])

    result = run_search(
        [],
        JobFilter(keyword=" Python ", city="北京", salary_min=10, salary_max=20),
        handler=handler,
    )
    assert seen["query"] == "Python"
    assert seen["city"] == "101010100"
    assert seen["salary"] == "405"
    assert seen["pageSize"] == "30"
    assert result.fetched == 1
    job = result.jobs[0]
    assert job.title == "Python 后端工程师"
    assert job.company == "示例科技"
    assert (job.salary_min, job.salary_max) == (15, 30)
    assert job.skills == ["Python", "FastAPI"]
    assert job.recruiter == "张经理 · 招聘负责人"
    assert job.published_at == NOW - timedelta(days=1)
    assert job.apply_url == "https://www.zhipin.com/job_detail/abc_DEF-123.html"


def test_publish_filter_ignores_last_time_and_missing_publish_time():
    items = [
        raw_job(encryptJobId="fresh", jobName="明确发布时间"),
        raw_job(
            encryptJobId="active",
            jobName="只有活跃时间",
            publishTime=None,
            lastTime=NOW.isoformat(),
        ),
        raw_job(
            encryptJobId="old",
            jobName="过期岗位",
            publishTime=(NOW - timedelta(days=8)).isoformat(),
        ),
    ]
    result = run_search(items, JobFilter(posted_days=7))
    assert result.fetched == 3
    assert [job.external_id for job in result.jobs] == ["fresh"]


def test_custom_salary_range_uses_intersection_and_excludes_unknown():
    items = [
        raw_job(encryptJobId="overlap", salaryDesc="10-20K"),
        raw_job(encryptJobId="high", salaryDesc="40-50K"),
        raw_job(encryptJobId="negotiable", salaryDesc="面议"),
        raw_job(encryptJobId="daily", salaryDesc="500元/天"),
    ]
    result = run_search(items, JobFilter(salary_min=15, salary_max=35))
    assert [job.external_id for job in result.jobs] == ["overlap"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10-20K·13薪", (10, 20)),
        ("50K以上", (50, None)),
        ("3K及以下", (None, 3)),
        ("面议", (None, None)),
        ("500元/天", (None, None)),
        ("20-10K", (None, None)),
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_invalid_external_id_is_skipped_and_url_is_whitelisted():
    result = run_search([raw_job(encryptJobId="bad/id", jobName="坏数据")])
    assert result.jobs == []
    with pytest.raises(ValueError):
        build_apply_url("bad/id")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="forbidden"),
        httpx.Response(200, text="<html>安全验证</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, text="not-json", headers={"content-type": "application/json"}),
        httpx.Response(200, json={"code": 37, "message": "请完成安全验证"}),
        httpx.Response(200, json={"code": 0, "zpData": {"unexpected": []}}),
    ],
)
def test_rejected_or_changed_response_has_clear_error(response):
    with pytest.raises(BossSourceError):
        run_search([], handler=lambda _: response)


def test_unsupported_city_fails_before_network():
    with pytest.raises(BossSourceError, match="暂不支持城市"):
        run_search([], JobFilter(city="不存在"))


def test_network_error_is_mapped():
    def handler(_):
        raise httpx.ConnectTimeout("timeout")

    with pytest.raises(BossSourceError, match="连接 BOSS 直聘失败"):
        run_search([], handler=handler)


def test_json_response_must_be_object():
    response = httpx.Response(200, content=json.dumps([]), headers={"content-type": "application/json"})
    with pytest.raises(BossSourceError):
        run_search([], handler=lambda _: response)
