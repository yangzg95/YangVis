"""AI 网关的管理侧：通道、模型路由、统一密钥、调用日志与统计。

对外转发在 :mod:`app.services.ai_gateway_proxy`；这里只管配置与留痕。网关是
平台级资产（不像 ``model_config`` 按用户隔离），因此所有写操作都由
``require_admin`` 把关。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import DecryptionError, decrypt, encrypt, is_masked, mask
from app.models.entities import AiApiKey, AiCallLog, AiChannel, AiModelRoute
from app.models.schemas import (
    AiApiKeyItem,
    AiCallLogItem,
    AiChannelCreate,
    AiChannelItem,
    AiChannelUpdate,
    AiGatewayOverview,
    AiModelRouteCreate,
    AiModelRouteItem,
    AiModelRouteUpdate,
    AiStats,
    AiStatsBucket,
    AiStatsTotals,
)

logger = logging.getLogger("yangvis.ai_gateway")

settings = get_settings()

# 对外密钥的可识别前缀：一眼能认出这是 yangvis 签发的网关密钥，也方便在
# 日志里做泄露扫描。
KEY_PREFIX = "sk-yv-"
# 列表里展示的前缀长度（含 KEY_PREFIX），剩下的用掩码代替。
DISPLAY_PREFIX_LENGTH = len(KEY_PREFIX) + 6

TRUNCATE_MARK = "…[已截断]"

# ai_call_log.created_at 由 MySQL 的 NOW() 写入（数据库本地时间），因此做时间
# 窗过滤时也用本地 naive 时间，与 services/ops_actions.py 同一套约定。
def _now() -> datetime:
    return datetime.now()


# ---- 密钥 -------------------------------------------------------------------


def hash_api_key(plain: str) -> str:
    """网关密钥只存摘要。

    这是一把长期有效、交给外部系统持有的凭据：存明文等于把「数据库被读到」
    直接升级成「对外接口被冒用」。
    """
    return hashlib.sha256(plain.strip().encode("utf-8")).hexdigest()


def display_prefix(plain: str) -> str:
    return plain[:DISPLAY_PREFIX_LENGTH]


def generate_api_key() -> Tuple[str, str, str]:
    """生成一把新密钥，返回（明文, 摘要, 展示前缀）。

    明文只在这一刻存在，调用方必须立刻把它返回给管理员。
    """
    plain = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return plain, hash_api_key(plain), display_prefix(plain)


def extract_bearer_key(authorization: Optional[str]) -> Optional[str]:
    """从 ``Authorization: Bearer <key>`` 里取出密钥，格式不对返回 None。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    key = parts[1].strip()
    return key or None


def truncate_text(text: Optional[str], max_chars: Optional[int] = None) -> Optional[str]:
    """按上限截断正文，并让「这条被截断过」在数据里看得出来。"""
    if text is None:
        return None
    limit = settings.AI_GATEWAY_LOG_MAX_CHARS if max_chars is None else max_chars
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + TRUNCATE_MARK


# ---- 上游地址校验 -----------------------------------------------------------

# 通道的 base_url 指回网关自己就会无限递归：请求打到 /v1，转发出去又回到 /v1，
# 一圈圈套下去直到超时，还会把调用日志刷满。能认出的「自己」有三类：管理请求
# 自己的 Host（概览页显示给管理员复制的正是这个地址，粘回来是最容易犯的错）、
# 回环地址、本机主机名。走内网别名（如 compose 服务名）指回来的认不出来，那种
# 配置只有部署者自己写得出来。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def normalize_base_url(base_url: str, own_host: Optional[str] = None) -> str:
    """校验并规范化通道的 base_url，指向本站时抛 ``ValueError``。

    ``own_host`` 传管理请求的 Host 头即可（nginx 会透传成对外域名），带不带
    端口都行。
    """
    url = base_url.strip().rstrip("/")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("base_url 必须形如 http(s)://主机名[:端口][/路径]")

    own = (own_host or "").split(":")[0].strip().lower()
    if host in _LOOPBACK_HOSTS or host == socket.gethostname().lower() or (own and host == own):
        raise ValueError("base_url 不能指向网关自己，请求会在网关里无限循环")
    return url


def public_base_url(scheme: str, host: str) -> str:
    """概览页显示、供调用方复制的接入地址（含 ``/v1``）。

    配了 ``AI_GATEWAY_PUBLIC_ORIGIN`` 就以它为准：网关单独挂一个域名（例如
    ``ai.nanwa.xyz``）时，管理员是从控制台域名打开这个页面的，按请求 origin
    推导会给出一个「也能用、但不是对外公布的那个」的地址。
    """
    origin = settings.AI_GATEWAY_PUBLIC_ORIGIN.strip().rstrip("/")
    if not origin:
        origin = f"{scheme}://{host}"
    return f"{origin}/v1"


# ---- 路由解析 ---------------------------------------------------------------


@dataclass(frozen=True)
class RouteCandidate:
    """一个对外模型名的一条可用上游。"""

    route: AiModelRoute
    channel: AiChannel
    upstream_model: str

    @property
    def upstream_key(self) -> str:
        # 挂在内网网关后面的 OpenAI 兼容服务常常不需要 key，但客户端要求非空。
        return decrypt(self.channel.api_key_enc) or "not-needed"


def resolve_candidates(db: Session, model_name: str) -> List[RouteCandidate]:
    """按优先级返回某个对外模型名当前可用的全部上游。

    通道停用、路由停用、通道被删（join 不上）都在这里一次性排除，转发侧拿到的
    就是「可以直接打」的列表，顺序即故障转移顺序。
    """
    rows = db.execute(
        select(AiModelRoute, AiChannel)
        .join(AiChannel, AiChannel.id == AiModelRoute.channel_id)
        .where(
            AiModelRoute.model_name == model_name,
            AiModelRoute.enabled.is_(True),
            AiChannel.enabled.is_(True),
        )
        .order_by(AiModelRoute.priority.asc(), AiModelRoute.id.asc())
    ).all()

    candidates: List[RouteCandidate] = []
    for route, channel in rows:
        candidates.append(
            RouteCandidate(
                route=route,
                channel=channel,
                upstream_model=(route.upstream_model or route.model_name),
            )
        )
    return candidates


def list_public_models(db: Session) -> List[str]:
    """当前对外可用的模型名（去重、按名字排序），给 ``GET /v1/models`` 用。"""
    rows = db.scalars(
        select(AiModelRoute.model_name)
        .join(AiChannel, AiChannel.id == AiModelRoute.channel_id)
        .where(AiModelRoute.enabled.is_(True), AiChannel.enabled.is_(True))
        .distinct()
        .order_by(AiModelRoute.model_name.asc())
    ).all()
    return list(rows)


def find_api_key(db: Session, plain: str) -> Optional[AiApiKey]:
    return db.scalar(select(AiApiKey).where(AiApiKey.key_hash == hash_api_key(plain)))


# ---- 管理服务 ---------------------------------------------------------------


class AiGatewayService:
    """网关配置的增删改查、日志查询与统计聚合。"""

    def __init__(self, db: Session) -> None:
        self._db = db

    # -- 通道 ---------------------------------------------------------------

    def list_channels(self) -> List[AiChannel]:
        return list(self._db.scalars(select(AiChannel).order_by(AiChannel.id.asc())).all())

    def get_channel(self, channel_id: int) -> AiChannel:
        channel = self._db.get(AiChannel, channel_id)
        if channel is None:
            raise LookupError("通道不存在")
        return channel

    def create_channel(
        self, payload: AiChannelCreate, own_host: Optional[str] = None
    ) -> AiChannel:
        channel = AiChannel(
            name=payload.name.strip(),
            base_url=normalize_base_url(payload.base_url, own_host),
            api_key_enc=encrypt(payload.api_key or ""),
            models=[name.strip() for name in (payload.models or []) if name.strip()] or None,
            remark=payload.remark,
        )
        self._db.add(channel)
        self._db.commit()
        self._db.refresh(channel)
        logger.info("created ai channel %s (%s)", channel.id, channel.name)
        return channel

    def update_channel(
        self, channel_id: int, payload: AiChannelUpdate, own_host: Optional[str] = None
    ) -> AiChannel:
        channel = self.get_channel(channel_id)
        data = payload.model_dump(exclude_unset=True)

        for field in ("name", "remark"):
            if data.get(field) is not None:
                setattr(channel, field, data[field].strip() if isinstance(data[field], str) else data[field])

        if data.get("base_url"):
            channel.base_url = normalize_base_url(data["base_url"], own_host)

        if "models" in data:
            models = data["models"]
            channel.models = (
                [name.strip() for name in models if name and name.strip()] if models else None
            )

        if data.get("enabled") is not None:
            channel.enabled = data["enabled"]

        # 与 model_config 同一约定：空串或掩码都表示「别动这把 key」。
        api_key = data.get("api_key")
        if api_key is not None and api_key != "" and not is_masked(api_key):
            channel.api_key_enc = encrypt(api_key)
            # 换过凭据，之前的测试结论就不再成立了。
            channel.last_test_ok = False
            channel.last_test_error = None

        self._db.commit()
        self._db.refresh(channel)
        logger.info("updated ai channel %s", channel.id)
        return channel

    def delete_channel(self, channel_id: int) -> None:
        channel = self.get_channel(channel_id)
        # 通道没了，指向它的路由就成了永远打不通的死配置，一起删掉；日志保留
        # channel_name 快照，历史仍然读得懂。
        routes = self._db.scalars(
            select(AiModelRoute).where(AiModelRoute.channel_id == channel_id)
        ).all()
        for route in routes:
            self._db.delete(route)
        self._db.delete(channel)
        self._db.commit()
        logger.info("deleted ai channel %s and %d route(s)", channel_id, len(routes))

    def record_channel_test(
        self, channel: AiChannel, *, ok: bool, message: str
    ) -> None:
        channel.last_tested_at = _now()
        channel.last_test_ok = ok
        channel.last_test_error = None if ok else message[:512]
        self._db.commit()

    def channel_item(self, channel: AiChannel) -> AiChannelItem:
        try:
            api_key = mask(decrypt(channel.api_key_enc))
            key_error: Optional[str] = None
        except DecryptionError as exc:
            logger.warning("failed to decrypt the api key of ai channel %s", channel.id)
            api_key = ""
            key_error = str(exc)

        return AiChannelItem(
            id=channel.id,
            name=channel.name,
            base_url=channel.base_url,
            api_key=api_key,
            api_key_error=key_error,
            protocol=channel.protocol,
            models=channel.models,
            enabled=channel.enabled,
            remark=channel.remark,
            last_tested_at=channel.last_tested_at,
            last_test_ok=channel.last_test_ok,
            last_test_error=channel.last_test_error,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )

    # -- 模型路由 -----------------------------------------------------------

    def list_routes(self, model_name: Optional[str] = None) -> List[AiModelRouteItem]:
        stmt: Select = select(AiModelRoute, AiChannel.name).outerjoin(
            AiChannel, AiChannel.id == AiModelRoute.channel_id
        )
        if model_name:
            stmt = stmt.where(AiModelRoute.model_name == model_name)
        stmt = stmt.order_by(AiModelRoute.model_name.asc(), AiModelRoute.priority.asc())

        items: List[AiModelRouteItem] = []
        for route, channel_name in self._db.execute(stmt).all():
            item = AiModelRouteItem.model_validate(route)
            item.channel_name = channel_name
            items.append(item)
        return items

    def create_route(self, payload: AiModelRouteCreate) -> AiModelRouteItem:
        # 通道不存在时让 join 悄悄丢掉这条路由，是最难排查的那种故障：配置看
        # 着在，调用却报「模型未配置」。所以创建时就把关系钉死。
        self.get_channel(payload.channel_id)
        route = AiModelRoute(
            model_name=payload.model_name.strip(),
            channel_id=payload.channel_id,
            upstream_model=(payload.upstream_model or "").strip() or None,
            priority=payload.priority,
            enabled=payload.enabled,
            remark=payload.remark,
        )
        self._db.add(route)
        self._db.commit()
        self._db.refresh(route)
        logger.info(
            "created ai route %s -> channel %s as %s",
            route.model_name,
            route.channel_id,
            route.upstream_model or route.model_name,
        )
        return self._route_item(route)

    def update_route(self, route_id: int, payload: AiModelRouteUpdate) -> AiModelRouteItem:
        route = self.get_route(route_id)
        data = payload.model_dump(exclude_unset=True)

        if data.get("channel_id") is not None and data["channel_id"] != route.channel_id:
            self.get_channel(data["channel_id"])
            route.channel_id = data["channel_id"]
        if data.get("model_name"):
            route.model_name = data["model_name"].strip()
        if "upstream_model" in data:
            upstream = (data["upstream_model"] or "").strip()
            route.upstream_model = upstream or None
        if data.get("priority") is not None:
            route.priority = data["priority"]
        if data.get("enabled") is not None:
            route.enabled = data["enabled"]
        if data.get("remark") is not None:
            route.remark = data["remark"]

        self._db.commit()
        self._db.refresh(route)
        logger.info("updated ai route %s", route.id)
        return self._route_item(route)

    def get_route(self, route_id: int) -> AiModelRoute:
        route = self._db.get(AiModelRoute, route_id)
        if route is None:
            raise LookupError("模型路由不存在")
        return route

    def delete_route(self, route_id: int) -> None:
        route = self.get_route(route_id)
        self._db.delete(route)
        self._db.commit()
        logger.info("deleted ai route %s (%s)", route_id, route.model_name)

    def _route_item(self, route: AiModelRoute) -> AiModelRouteItem:
        channel = self._db.get(AiChannel, route.channel_id)
        item = AiModelRouteItem.model_validate(route)
        item.channel_name = channel.name if channel else None
        return item

    # -- 密钥 ---------------------------------------------------------------

    def list_keys(self) -> List[AiApiKey]:
        return list(self._db.scalars(select(AiApiKey).order_by(AiApiKey.id.desc())).all())

    def get_key(self, key_id: int) -> AiApiKey:
        row = self._db.get(AiApiKey, key_id)
        if row is None:
            raise LookupError("密钥不存在")
        return row

    def create_key(self, name: str, remark: Optional[str]) -> Tuple[AiApiKey, str]:
        """建一把密钥，返回（记录, 明文）。明文此后无法再取。"""
        plain, key_hash, prefix = generate_api_key()
        row = AiApiKey(name=name.strip(), key_hash=key_hash, key_prefix=prefix, remark=remark)
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info("issued ai api key %s (%s)", row.id, row.name)
        return row, plain

    def update_key(self, key_id: int, payload: Any) -> AiApiKey:
        row = self.get_key(key_id)
        data = payload.model_dump(exclude_unset=True)
        for field in ("name", "remark"):
            if data.get(field) is not None:
                value = data[field]
                setattr(row, field, value.strip() if field == "name" else value)
        if data.get("enabled") is not None:
            row.enabled = data["enabled"]
        self._db.commit()
        self._db.refresh(row)
        logger.info("updated ai api key %s (enabled=%s)", row.id, row.enabled)
        return row

    def delete_key(self, key_id: int) -> None:
        row = self.get_key(key_id)
        self._db.delete(row)
        self._db.commit()
        # 吊销是安全动作，明确留痕：谁在什么时候让哪把钥匙失效了。
        logger.info("revoked ai api key %s (%s)", key_id, row.name)

    def key_item(self, row: AiApiKey) -> AiApiKeyItem:
        item = AiApiKeyItem.model_validate(row)
        item.key_prefix = f"{row.key_prefix}****" if row.key_prefix else ""
        return item

    def touch_key(self, key: AiApiKey) -> None:
        """回填调用计数与最近使用时间（与日志同一个事务提交）。"""
        key.call_count = (key.call_count or 0) + 1
        key.last_used_at = _now()

    # -- 日志 ---------------------------------------------------------------

    def list_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        key_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        success: Optional[bool] = None,
        keyword: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Tuple[List[AiCallLogItem], int]:
        stmt = select(AiCallLog)
        if key_id is not None:
            stmt = stmt.where(AiCallLog.key_id == key_id)
        if channel_id is not None:
            stmt = stmt.where(AiCallLog.channel_id == channel_id)
        if model:
            stmt = stmt.where(AiCallLog.model == model)
        if endpoint:
            stmt = stmt.where(AiCallLog.endpoint == endpoint)
        if success is not None:
            stmt = stmt.where(AiCallLog.success.is_(success))
        if keyword:
            pattern = f"%{keyword.strip()}%"
            stmt = stmt.where(
                or_(
                    AiCallLog.request_id.like(pattern),
                    AiCallLog.model.like(pattern),
                    AiCallLog.key_name.like(pattern),
                    AiCallLog.channel_name.like(pattern),
                    AiCallLog.error.like(pattern),
                )
            )
        if start is not None:
            stmt = stmt.where(AiCallLog.created_at >= start)
        if end is not None:
            stmt = stmt.where(AiCallLog.created_at <= end)

        total = self._db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = self._db.scalars(
            stmt.order_by(AiCallLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [AiCallLogItem.model_validate(row) for row in rows], total

    def get_log(self, log_id: int) -> AiCallLog:
        row = self._db.get(AiCallLog, log_id)
        if row is None:
            raise LookupError("调用日志不存在")
        return row

    def purge_logs(self, before_days: int) -> int:
        cutoff = _now() - timedelta(days=before_days)
        result = self._db.execute(delete(AiCallLog).where(AiCallLog.created_at < cutoff))
        self._db.commit()
        deleted = int(result.rowcount or 0)
        logger.info("purged %d ai call log row(s) older than %d day(s)", deleted, before_days)
        return deleted

    # -- 统计 ---------------------------------------------------------------

    def stats(self, days: int = 7) -> AiStats:
        since = _now() - timedelta(days=days)
        window = AiCallLog.created_at >= since

        calls = self._db.scalar(select(func.count(AiCallLog.id)).where(window)) or 0
        success = self._db.scalar(
            select(func.count(AiCallLog.id)).where(window, AiCallLog.success.is_(True))
        ) or 0
        prompt_tokens = int(self._db.scalar(select(func.sum(AiCallLog.prompt_tokens)).where(window)) or 0)
        completion_tokens = int(
            self._db.scalar(select(func.sum(AiCallLog.completion_tokens)).where(window)) or 0
        )
        total_tokens = int(self._db.scalar(select(func.sum(AiCallLog.total_tokens)).where(window)) or 0)
        avg_latency = int(self._db.scalar(select(func.avg(AiCallLog.latency_ms)).where(window)) or 0)
        max_latency = int(self._db.scalar(select(func.max(AiCallLog.latency_ms)).where(window)) or 0)

        return AiStats(
            totals=AiStatsTotals(
                calls=calls,
                success=success,
                failed=calls - success,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                avg_latency_ms=avg_latency,
                max_latency_ms=max_latency,
            ),
            daily=self._bucket(func.date(AiCallLog.created_at), window, desc=True),
            by_channel=self._bucket(func.coalesce(AiCallLog.channel_name, "未知通道"), window),
            by_model=self._bucket(func.coalesce(AiCallLog.model, "未知模型"), window),
            by_key=self._bucket(func.coalesce(AiCallLog.key_name, "未知密钥"), window),
        )

    def _bucket(self, label: Any, window: Any, *, desc: bool = False) -> List[AiStatsBucket]:
        """按某一列聚合出调用数 / 失败数 / token / 平均耗时。"""
        failed_expr = func.sum(case((AiCallLog.success.is_(True), 0), else_=1))
        stmt = (
            select(
                label.label("name"),
                func.count(AiCallLog.id),
                failed_expr,
                func.sum(AiCallLog.total_tokens),
                func.avg(AiCallLog.latency_ms),
            )
            .where(window)
            .group_by(label)
        )
        if desc:
            stmt = stmt.order_by(label.desc())
        rows = self._db.execute(stmt).all()
        return [
            AiStatsBucket(
                name=str(name),
                calls=int(calls or 0),
                failed=int(failed or 0),
                total_tokens=int(tokens or 0),
                avg_latency_ms=int(avg or 0),
            )
            for name, calls, failed, tokens, avg in rows
        ]

    # -- 概览 ---------------------------------------------------------------

    def overview(self, base_url: str) -> AiGatewayOverview:
        def count(model: Any, *where: Any) -> int:
            return int(self._db.scalar(select(func.count(model.id)).where(*where)) or 0)

        return AiGatewayOverview(
            base_url=base_url,
            channel_count=count(AiChannel),
            channel_enabled=count(AiChannel, AiChannel.enabled.is_(True)),
            route_count=count(AiModelRoute),
            route_enabled=count(AiModelRoute, AiModelRoute.enabled.is_(True)),
            key_count=count(AiApiKey),
            key_enabled=count(AiApiKey, AiApiKey.enabled.is_(True)),
            log_count=count(AiCallLog),
            payload_logging=settings.AI_GATEWAY_LOG_PAYLOAD,
        )


def write_call_log(db: Session, log: AiCallLog) -> None:
    """落一条调用日志。

    留痕失败绝不能让调用方的请求失败：网关已经在转发路径上，日志写不进去是
    运维问题，把它变成 5xx 等于让监控故障扩大成业务故障。
    """
    try:
        db.add(log)
        db.commit()
    except Exception:  # noqa: BLE001 - 任何数据库异常都只记录，不外抛
        db.rollback()
        logger.error("failed to persist ai call log for request %s", log.request_id, exc_info=True)
