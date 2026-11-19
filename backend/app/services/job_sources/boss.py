"""BOSS 直聘公开岗位搜索适配器。

只请求固定官方 HTTPS 地址，不带用户 Cookie，也不处理登录或安全验证。站点拒绝
访问时把错误交给调用方，不能用招聘者活跃时间或伪造数据补齐岗位列表。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

import httpx

from app.models.schemas import JobFilter

logger = logging.getLogger("yangvis.jobs.boss")

_SEARCH_URL = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
_PAGE_SIZE = 30
_MAX_PAGES = 3
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SALARY_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)K")
_SALARY_MIN_RE = re.compile(r"^(\d+(?:\.\d+)?)K(?:及)?以上")
_SALARY_MAX_RE = re.compile(r"^(\d+(?:\.\d+)?)K(?:及)?以下")
_RELATIVE_TIME_RE = re.compile(r"^(\d+)\s*(分钟|小时|天|周)前$")

# 客户端只传城市名，BOSS 的城市代码留在来源适配器里，避免业务表依赖外部编码。
CITY_CODES = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "天津": "101030100",
    "重庆": "101040100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "南京": "101190100",
    "苏州": "101190400",
    "成都": "101270100",
    "武汉": "101200100",
    "西安": "101110100",
    "长沙": "101250100",
    "郑州": "101180100",
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
    "济南": "101120100",
    "青岛": "101120200",
    "东莞": "101281600",
    "佛山": "101280800",
    "宁波": "101210400",
    "无锡": "101190200",
    "大连": "101070200",
    "沈阳": "101070100",
}

# BOSS 只接受离散薪资档。自定义数值区间由本地数值过滤保证，不能硬塞进近档。
_SALARY_CODES = {
    (None, 3.0): "402",
    (3.0, 5.0): "403",
    (5.0, 10.0): "404",
    (10.0, 20.0): "405",
    (20.0, 50.0): "406",
    (50.0, None): "407",
}
_PUBLISH_FIELDS = (
    "publishTime",
    "publish_time",
    "jobPublishTime",
    "job_publish_time",
    "publishedAt",
    "published_at",
)


class BossSourceError(Exception):
    """外部岗位来源暂时不可用或响应结构不符合约定。"""


@dataclass(slots=True)
class BossJob:
    external_id: str
    title: str
    company: str
    city: str
    district: Optional[str]
    salary_text: str
    salary_min: Optional[float]
    salary_max: Optional[float]
    experience: Optional[str]
    education: Optional[str]
    skills: list[str]
    recruiter: Optional[str]
    published_at: Optional[datetime]
    apply_url: str


@dataclass(slots=True)
class BossSearchResult:
    jobs: list[BossJob]
    fetched: int


class BossJobSource:
    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport

    async def search(
        self, filters: JobFilter, *, now: Optional[datetime] = None
    ) -> BossSearchResult:
        city_code = CITY_CODES.get(filters.city)
        if city_code is None:
            raise BossSourceError(f"暂不支持城市「{filters.city}」，请从城市选项中选择")

        current_time = now or datetime.now()
        jobs: list[BossJob] = []
        fetched = 0
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_TIMEOUT,
                follow_redirects=False,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            ) as client:
                for page in range(1, _MAX_PAGES + 1):
                    params: dict[str, Any] = {
                        "scene": 1,
                        "query": filters.keyword,
                        "city": city_code,
                        "page": page,
                        "pageSize": _PAGE_SIZE,
                    }
                    salary_code = _SALARY_CODES.get((filters.salary_min, filters.salary_max))
                    if salary_code:
                        params["salary"] = salary_code
                    raw_jobs = await self._request_page(client, params)
                    fetched += len(raw_jobs)
                    for raw in raw_jobs:
                        job = _parse_job(raw, current_time)
                        if job is not None and _matches(job, filters, current_time):
                            jobs.append(job)
                    if len(raw_jobs) < _PAGE_SIZE:
                        break
        except httpx.HTTPError as exc:
            logger.warning("BOSS request failed: %s", type(exc).__name__)
            raise BossSourceError("连接 BOSS 直聘失败，请稍后重试") from exc
        return BossSearchResult(jobs=jobs, fetched=fetched)

    @staticmethod
    async def _request_page(
        client: httpx.AsyncClient, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        async with client.stream("GET", _SEARCH_URL, params=params) as response:
            if response.status_code in (401, 403, 412, 429):
                raise BossSourceError("BOSS 直聘暂时限制了访问，请稍后重试")
            if response.status_code != 200:
                raise BossSourceError(f"BOSS 直聘返回 HTTP {response.status_code}，请稍后重试")
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type:
                raise BossSourceError("BOSS 直聘要求登录或安全验证，本次未抓取岗位")
            chunks = bytearray()
            async for chunk in response.aiter_bytes(64 * 1024):
                chunks.extend(chunk)
                if len(chunks) > _MAX_RESPONSE_BYTES:
                    raise BossSourceError("BOSS 直聘响应过大，已中止抓取")

        try:
            body = json.loads(bytes(chunks).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BossSourceError("BOSS 直聘返回了无法解析的响应") from exc
        if not isinstance(body, dict) or body.get("code") != 0:
            message = body.get("message") if isinstance(body, dict) else None
            logger.warning("BOSS business error code=%r", body.get("code") if isinstance(body, dict) else None)
            raise BossSourceError(message or "BOSS 直聘拒绝了本次搜索")
        data = body.get("zpData")
        if not isinstance(data, dict) or not isinstance(data.get("jobList"), list):
            raise BossSourceError("BOSS 直聘岗位响应结构已变化，本次未保存岗位")
        return [item for item in data["jobList"] if isinstance(item, dict)]


def _parse_job(raw: dict[str, Any], now: datetime) -> Optional[BossJob]:
    external_id = _text(raw.get("encryptJobId"), 128)
    title = _text(raw.get("jobName"), 200)
    company = _text(raw.get("brandName"), 200)
    if not external_id or not title or not company or not _EXTERNAL_ID_RE.fullmatch(external_id):
        return None

    salary_text = _text(raw.get("salaryDesc"), 64) or "面议"
    salary_min, salary_max = parse_salary(salary_text)
    recruiter_name = _text(raw.get("bossName"), 80)
    recruiter_title = _text(raw.get("bossTitle"), 100)
    recruiter = " · ".join(part for part in (recruiter_name, recruiter_title) if part) or None
    return BossJob(
        external_id=external_id,
        title=title,
        company=company,
        city=_text(raw.get("cityName"), 32) or "全国",
        district=_text(raw.get("areaDistrict"), 64) or None,
        salary_text=salary_text,
        salary_min=salary_min,
        salary_max=salary_max,
        experience=_text(raw.get("jobExperience"), 32) or None,
        education=_text(raw.get("jobDegree"), 32) or None,
        skills=_string_list(raw.get("skills") or raw.get("jobLabels")),
        recruiter=recruiter,
        published_at=_published_at(raw, now),
        apply_url=build_apply_url(external_id),
    )


def build_apply_url(external_id: str) -> str:
    """详情地址只由合法岗位 ID 生成，不直接打开上游返回的任意 URL。"""
    if not _EXTERNAL_ID_RE.fullmatch(external_id):
        raise ValueError("非法 BOSS 岗位 ID")
    return f"https://www.zhipin.com/job_detail/{quote(external_id, safe='')}.html"


def parse_salary(value: str) -> tuple[Optional[float], Optional[float]]:
    """解析标准月薪 K；元/日、元/时和面议不做数值猜测。"""
    text = value.strip().upper().replace(" ", "")
    match = _SALARY_RANGE_RE.match(text)
    if match:
        lower, upper = float(match.group(1)), float(match.group(2))
        return (lower, upper) if lower <= upper else (None, None)
    match = _SALARY_MIN_RE.match(text) or _SALARY_MAX_RE.match(text)
    if not match:
        return None, None
    number = float(match.group(1))
    return (number, None) if "以上" in text else (None, number)


def _matches(job: BossJob, filters: JobFilter, now: datetime) -> bool:
    if filters.city != "全国" and job.city != filters.city:
        return False
    if filters.posted_days is not None:
        if job.published_at is None or job.published_at < now - timedelta(days=filters.posted_days):
            return False
    if filters.salary_min is not None or filters.salary_max is not None:
        if job.salary_min is None and job.salary_max is None:
            return False
        if filters.salary_min is not None:
            if job.salary_max is not None and job.salary_max < filters.salary_min:
                return False
        if filters.salary_max is not None:
            if job.salary_min is not None and job.salary_min > filters.salary_max:
                return False
    return True


def _published_at(raw: dict[str, Any], now: datetime) -> Optional[datetime]:
    # 故意不读取 lastTime：它通常表示招聘者活跃时间，不是岗位发布时间。
    for field in _PUBLISH_FIELDS:
        value = raw.get(field)
        if value not in (None, ""):
            parsed = _parse_datetime(value, now)
            if parsed is not None:
                return parsed
    return None


def _parse_datetime(value: Any, now: datetime) -> Optional[datetime]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(timestamp).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_datetime(int(text), now)
    if text in ("刚刚", "今天"):
        return now
    if text == "昨天":
        return now - timedelta(days=1)
    relative = _RELATIVE_TIME_RE.fullmatch(text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = {
            "分钟": timedelta(minutes=amount),
            "小时": timedelta(hours=amount),
            "天": timedelta(days=amount),
            "周": timedelta(weeks=amount),
        }[unit]
        return now - delta
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item.get("name") if isinstance(item, dict) else item, 32)
        if text and text not in result:
            result.append(text)
        if len(result) == 8:
            break
    return result


def _text(value: Any, limit: int) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()[:limit]
