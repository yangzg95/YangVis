"""华为 HMS Push Kit 的服务端发送：OAuth2 取 token + messages:send。

与 :mod:`app.services.notify.senders` 的 webhook 机器人并列的另一种发送器，
协议完全不同：先用 client_id / client_secret 走 client_credentials 换一个
access_token（进程内缓存、临期自动刷新），再带 Bearer 调 Push Kit 的 REST
接口下发通知。凭据来自 ``notify_huawei_config``，Fernet 解密后只活在内存里，
绝不进日志或错误信息。

端点契约依据 HMS Core Push Kit 服务端 API。接入时华为开发者文档站对本环境
限流（HTTP 503）无法在线复核，因此把 URL、字段名、成功码全部收敛成本模块
常量，单测用 ``httpx.MockTransport`` 钉死请求形状，并有真机端到端验收兜底；
官方若调整，只改这里的常量即可。

重试口径与 senders 一致：连接层错误与 5xx 重试一次；4xx 是终态。额外处理
鉴权失效——HTTP 401/403 或 token 失效业务码 80200001 时，强制刷新 token 后
最多重试一次。任何失败都压成 :class:`SendResult`，不向 dispatch 抛异常。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.services.notify.senders import NotifySendError, SendResult, _http_error

logger = logging.getLogger("yangvis.notify")

# ---- 协议常量（单点维护，见模块 docstring 的复核说明）-------------------------

OAUTH_TOKEN_URL = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
PUSH_SEND_URL = "https://push-api.cloud.huawei.com/v1/{app_id}/messages:send"

# 成功响应的业务码，注意是字符串 "80000000"，不是数字 0。
SUCCESS_CODE = "80000000"
# access_token 过期/无效：刷新 token 后重试一次的依据。
TOKEN_INVALID_CODES = frozenset({"80200001"})

# HMS 颁发的 token 有效期通常是 3600s；响应缺 expires_in 时的兜底。
DEFAULT_TOKEN_TTL = 3600
# 提前刷新的安全余量，避免在临界点发出带过期 token 的请求。
TOKEN_SAFETY_MARGIN = 300

TARGET_TOPIC = "topic"
TARGET_TOKEN = "token"

# 错误信息长度上限，与 senders 的 _MAX_ERROR_BODY 对齐，避免撑爆 512 的 error 列。
_MAX_DETAIL = 200


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    # 用 monotonic 记过期时刻：系统时钟回拨/NTP 校时不会污染缓存判断。
    expires_at: float


class HuaweiTokenCache:
    """按 client_id 缓存 access_token，临期或被强制时刷新。

    进程内缓存：gunicorn 多 worker 各持一份（worker 间不共享内存，与 WebSocket
    票据同一前提），每个 worker 首次发送各取一次 token，量级可接受。
    每个 client_id 一把 :class:`asyncio.Lock`，防止 token 缺失时被多个并发
    协程重复刷新（double-checked locking）。
    """

    def __init__(self) -> None:
        self._tokens: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, client_id: str) -> asyncio.Lock:
        # asyncio 单线程，dict 的读改写之间没有 await，这里无需额外加锁。
        lock = self._locks.get(client_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[client_id] = lock
        return lock

    async def get(
        self,
        client: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        *,
        force_refresh: bool = False,
    ) -> str:
        now = time.monotonic()
        cached = self._tokens.get(client_id)
        if not force_refresh and cached and cached.expires_at - TOKEN_SAFETY_MARGIN > now:
            return cached.access_token

        async with self._lock_for(client_id):
            now = time.monotonic()
            cached = self._tokens.get(client_id)
            if not force_refresh and cached and cached.expires_at - TOKEN_SAFETY_MARGIN > now:
                return cached.access_token
            token, ttl = await self._fetch_token(client, client_id, client_secret)
            self._tokens[client_id] = _CachedToken(
                access_token=token, expires_at=now + ttl
            )
            return token

    async def _fetch_token(
        self, client: httpx.AsyncClient, client_id: str, client_secret: str
    ) -> tuple[str, int]:
        # 表单提交（application/x-www-form-urlencoded），不是 JSON。
        resp = await client.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if resp.status_code >= 400:
            # 凭据错、应用未开通等都是终态，靠重试翻不了盘。
            raise NotifySendError(f"华为 OAuth 鉴权失败：{_http_error(resp)}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise NotifySendError("华为 OAuth 返回了无法解析的响应") from exc

        token = payload.get("access_token")
        if not token:
            # 走到这里说明响应里本就没有有效 token，dump 正文不会泄密，仅截断。
            detail = json.dumps(payload, ensure_ascii=False)[:_MAX_DETAIL]
            raise NotifySendError(f"华为 OAuth 响应缺少 access_token：{detail}")
        ttl = int(payload.get("expires_in") or DEFAULT_TOKEN_TTL)
        return token, max(ttl, 60)

    def clear(self) -> None:
        """供测试与潜在的手动失效使用。"""
        self._tokens.clear()


# 模块级单例：进程内复用 token，避免每条消息都换一次。
token_cache = HuaweiTokenCache()


# ---- 请求体构造（纯函数，单测直接钉形状）-------------------------------------


def build_huawei_message(
    *,
    target_type: str,
    target: str,
    title: str,
    body: str,
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """组装 messages:send 的请求体。

    目标三选一本模块只用 topic / token：topic 走主题订阅，token 定向单设备。
    ``notification`` 决定系统通知栏展示；``data`` 是 HMS 规定的**字符串**，
    结构化数据需自行 JSON 编码，App 存活时经透传通道收到。
    """
    message: dict[str, Any] = {"notification": {"title": title, "body": body}}
    if target_type == TARGET_TOPIC:
        message["topic"] = target
    elif target_type == TARGET_TOKEN:
        message["token"] = target
    else:
        raise NotifySendError(f"未知华为推送目标类型：{target_type!r}")

    if data:
        message["data"] = json.dumps(data, ensure_ascii=False)
    return {"validate_only": False, "message": message}


def _business_error(payload: Any) -> str:
    """把 HMS 业务错误码翻译成可读字符串；响应体不含密钥，可安全留痕。"""
    if isinstance(payload, dict):
        code = payload.get("code")
        msg = payload.get("msg") or payload.get("error_description") or "未知错误"
        return f"华为返回 code={code} msg={msg}"[:_MAX_DETAIL]
    return f"华为返回异常响应：{str(payload)[:_MAX_DETAIL]}"


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ---- 发送 -------------------------------------------------------------------


async def send_huawei_message(
    client: httpx.AsyncClient,
    *,
    app_id: str,
    client_id: str,
    client_secret: str,
    target_type: str,
    target: str,
    title: str,
    body: str,
    data: Optional[dict[str, Any]] = None,
) -> SendResult:
    """向 HMS Push Kit 发送一条通知，带一次重试，失败不外抛。

    成功判定：HTTP 2xx 且响应业务码为 :data:`SUCCESS_CODE`。鉴权失效时强制
    刷新 token 重试一次；连接错误 / 5xx 也重试一次；其余 4xx 与业务错误码是
    终态，直接返回失败结果。
    """
    url = PUSH_SEND_URL.format(app_id=app_id)
    payload = build_huawei_message(
        target_type=target_type,
        target=target,
        title=title,
        body=body,
        data=data,
    )
    started = time.monotonic()
    force_refresh = False
    last_error: Optional[str] = None

    for attempt in range(2):
        # 取 token 失败要区分：网络抖动可重试，OAuth 明确拒绝是终态。
        try:
            access_token = await token_cache.get(
                client, client_id, client_secret, force_refresh=force_refresh
            )
        except httpx.TransportError as exc:
            last_error = f"网络错误：{exc}"
            force_refresh = False
            continue
        except NotifySendError as exc:
            return SendResult(ok=False, error=str(exc), elapsed_ms=_elapsed(started))

        try:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )
        except httpx.TransportError as exc:
            last_error = f"网络错误：{exc}"
            continue

        elapsed = _elapsed(started)
        if resp.status_code >= 500:
            last_error = _http_error(resp)
            continue
        if resp.status_code in (401, 403):
            last_error = _http_error(resp)
            if attempt == 0:
                # token 可能已在别处失效：强制刷新后给一次机会。
                force_refresh = True
                continue
            return SendResult(ok=False, error=last_error, elapsed_ms=elapsed)
        if resp.status_code >= 400:
            return SendResult(ok=False, error=_http_error(resp), elapsed_ms=elapsed)

        try:
            result = resp.json()
        except ValueError:
            return SendResult(
                ok=False,
                error=f"华为返回了无法解析的响应（HTTP {resp.status_code}）",
                elapsed_ms=elapsed,
            )

        code = str(result.get("code", ""))
        if code == SUCCESS_CODE:
            return SendResult(ok=True, elapsed_ms=elapsed)
        if code in TOKEN_INVALID_CODES and attempt == 0:
            last_error = _business_error(result)
            force_refresh = True
            continue
        return SendResult(ok=False, error=_business_error(result), elapsed_ms=elapsed)

    return SendResult(
        ok=False, error=last_error or "华为推送发送失败", elapsed_ms=_elapsed(started)
    )
