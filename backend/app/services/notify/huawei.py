"""华为 HMS Push Kit 的服务端发送：OAuth2 取 token + messages:send。

与 :mod:`app.services.notify.senders` 的 webhook 机器人并列的另一种发送器，
协议完全不同：先取一个 access_token（进程内缓存、临期自动刷新），再带 Bearer
调 Push Kit 的 REST 接口下发通知。凭据来自 ``notify_huawei_config``，Fernet
解密后只活在内存里，绝不进日志或错误信息。

支持两种 AGC 凭据，二选一即可：

1. **client_credentials**：AGC 应用常规页的 Client ID / Client Secret，表单
   提交 ``grant_type=client_credentials`` 换 token；
2. **服务账号私钥（JWT bearer）**：AGC 下发的服务账号 JSON（含 key_id、
   sub_account 与 PEM 私钥），本地签一个一小时有效的 JWT，再以
   ``grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`` 换 token。
   算法用 **RS256** 而非官方示例写的 PS256，原因见 :data:`JWT_ALGORITHM`。

端点契约依据 HMS Core Push Kit 服务端 API。接入时华为开发者文档站对本环境
限流（HTTP 503）无法在线复核，因此把 URL、字段名、成功码全部收敛成本模块
常量，单测用 ``httpx.MockTransport`` 钉死请求形状，并有真机端到端验收兜底；
官方若调整，只改这里的常量即可。注意两个易踩的类型契约：成功码是字符串
``"80000000"``；``message.token`` 是**字符串数组**而 ``message.topic`` 是字符串。

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
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.services.notify.senders import NotifySendError, SendResult, _http_error

logger = logging.getLogger("yangvis.notify")

# ---- 协议常量（单点维护，见模块 docstring 的复核说明）-------------------------

OAUTH_TOKEN_URL = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
PUSH_SEND_URL = "https://push-api.cloud.huawei.com/v1/{app_id}/messages:send"

# 成功响应的业务码，注意是字符串 "80000000"，不是数字 0。
SUCCESS_CODE = "80000000"
# access_token 过期/无效：刷新 token 后重试一次的依据。
TOKEN_INVALID_CODES = frozenset({"80200001"})

# 两种 OAuth 授权方式的 grant_type。
GRANT_CLIENT_CREDENTIALS = "client_credentials"
GRANT_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# 服务账号 JWT 的签名算法。官方示例与文档写的是 PS256（RSA-PSS），但 2026-11-06
# 真机联调实测：同一把 4096 位私钥、同一套 header/claims，PS256 在三种 PSS salt
# 长度（32/digest、0、MAX）下全部被 OAuth 端点拒（1101 / sub_error 20504
# "jwt verify error"，kid 本身存在——换错 kid 报的是 20502 "kid is not exist"），
# 而 RS256（RSASSA-PKCS1-v1_5 + SHA-256）当场换到 access_token。判定该端点的
# JWT 验签实际只接受 PKCS#1 v1.5，故常量取 RS256；若华为日后修复 PSS 验签，
# 只需把这里改回 "PS256"（测试均引用本常量，无需联动修改）。
JWT_ALGORITHM = "RS256"
# AGC 服务账号 JWT 固定有效期 1 小时；过期时间由签发方声明，本地时钟要准。
JWT_TTL_SECONDS = 3600

# HMS 颁发的 token 有效期通常是 3600s；响应缺 expires_in 时的兜底。
DEFAULT_TOKEN_TTL = 3600
# 提前刷新的安全余量，避免在临界点发出带过期 token 的请求。
TOKEN_SAFETY_MARGIN = 300

TARGET_TOPIC = "topic"
TARGET_TOKEN = "token"

# 服务账号 JSON 必须具备的三个字段。
_SERVICE_ACCOUNT_REQUIRED_KEYS = ("key_id", "sub_account", "private_key")

# 错误信息长度上限，与 senders 的 _MAX_ERROR_BODY 对齐，避免撑爆 512 的 error 列。
_MAX_DETAIL = 200

# 服务账号能换到 token、却被 push 端点拒（80200001）时的排查引导。2026-11-06 实测：
# 同一 app_id 下 client_credentials 令牌可正常推送，服务账号 JWT 令牌在 v1/v2 端点、
# 带不带 client_id 均报 Authentication Error；官方 push-serverdemo-python 也只实现
# client_credentials。判定为账号侧项目角色/推送权限缺失，非报文问题。
_SA_PUSH_PERMISSION_HINT = (
    "服务账号令牌已签发但无 Push Kit 调用权限：请到 AGC 控制台「用户与权限-服务账号」"
    "确认该账号已加入本项目并授予含推送权限的角色，或改用 Client ID/Secret 凭证"
)


# ---- 凭据载体 ----------------------------------------------------------------


@dataclass(frozen=True)
class HuaweiAuth:
    """一次发送所需的 OAuth 凭据，两种模式二选一。

    用不透明的单一对象穿过 dispatch / service，调用方不用到处传三个散参数；
    ``cache_key`` 让 token 缓存按凭据身份隔离，两套凭据不会互相顶掉。
    """

    mode: str
    client_id: str = ""
    client_secret: str = ""
    # JWT 模式：已校验过结构的服务账号 JSON（key_id/sub_account/private_key）。
    service_account: Optional[dict[str, Any]] = None

    @classmethod
    def client_credentials(cls, client_id: str, client_secret: str) -> "HuaweiAuth":
        return cls(mode=GRANT_CLIENT_CREDENTIALS, client_id=client_id, client_secret=client_secret)

    @classmethod
    def service_account(cls, service_account: dict[str, Any]) -> "HuaweiAuth":
        return cls(mode=GRANT_JWT_BEARER, service_account=dict(service_account))

    @property
    def cache_key(self) -> str:
        if self.mode == GRANT_JWT_BEARER:
            # sub_account 是服务账号身份（iss），天然适合做缓存键。
            return f"jwt:{(self.service_account or {}).get('sub_account', '')}"
        return f"cc:{self.client_id}"


def parse_service_account(raw_text: str) -> dict[str, Any]:
    """校验并解析 AGC 服务账号 JSON，写入配置前调用，把坏输入挡在库外。

    只验结构与 PEM 合法性，不在这里签 JWT。任何问题都抛 :class:`ValueError`
    （service 层翻译成业务错误码 -1），调用方负责不要把原文打进日志。
    """
    try:
        data = json.loads(raw_text)
    except (ValueError, TypeError) as exc:
        raise ValueError("服务账号私钥 JSON 格式不合法，请粘贴完整文件内容") from exc
    if not isinstance(data, dict):
        raise ValueError("服务账号私钥 JSON 顶层必须是对象")

    for key in _SERVICE_ACCOUNT_REQUIRED_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"服务账号私钥 JSON 缺少必要字段：{key}")

    try:
        # 立刻验一次 PEM：私钥被截断/换行损坏时，保存阶段就报错，而不是等发送时 401。
        load_pem_private_key(data["private_key"].encode("utf-8"), password=None)
    except ValueError as exc:
        raise ValueError("服务账号私钥不是合法的 PEM 私钥，请重新下载 JSON") from exc
    return data


def _build_service_account_jwt(service_account: dict[str, Any]) -> str:
    """用服务账号私钥签一个换取 access_token 的断言 JWT（算法见 JWT_ALGORITHM）。"""
    now = int(time.time())
    return jwt.encode(
        payload={
            "aud": OAUTH_TOKEN_URL,
            "iss": service_account["sub_account"],
            "exp": now + JWT_TTL_SECONDS,
            "iat": now,
        },
        key=service_account["private_key"],
        algorithm=JWT_ALGORITHM,
        headers={"kid": service_account["key_id"], "typ": "JWT", "alg": JWT_ALGORITHM},
    )


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    # 用 monotonic 记过期时刻：系统时钟回拨/NTP 校时不会污染缓存判断。
    expires_at: float


class HuaweiTokenCache:
    """按凭据身份缓存 access_token，临期或被强制时刷新。

    进程内缓存：gunicorn 多 worker 各持一份（worker 间不共享内存，与 WebSocket
    票据同一前提），每个 worker 首次发送各取一次 token，量级可接受。
    每个 cache_key 一把 :class:`asyncio.Lock`，防止 token 缺失时被多个并发
    协程重复刷新（double-checked locking）。
    """

    def __init__(self) -> None:
        self._tokens: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, cache_key: str) -> asyncio.Lock:
        # asyncio 单线程，dict 的读改写之间没有 await，这里无需额外加锁。
        lock = self._locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[cache_key] = lock
        return lock

    async def get(
        self,
        client: httpx.AsyncClient,
        auth: HuaweiAuth,
        *,
        force_refresh: bool = False,
    ) -> str:
        cache_key = auth.cache_key
        now = time.monotonic()
        cached = self._tokens.get(cache_key)
        if not force_refresh and cached and cached.expires_at - TOKEN_SAFETY_MARGIN > now:
            return cached.access_token

        async with self._lock_for(cache_key):
            now = time.monotonic()
            cached = self._tokens.get(cache_key)
            if not force_refresh and cached and cached.expires_at - TOKEN_SAFETY_MARGIN > now:
                return cached.access_token
            token, ttl = await self._fetch_token(client, auth)
            self._tokens[cache_key] = _CachedToken(
                access_token=token, expires_at=now + ttl
            )
            return token

    async def _fetch_token(
        self, client: httpx.AsyncClient, auth: HuaweiAuth
    ) -> tuple[str, int]:
        if auth.mode == GRANT_JWT_BEARER:
            if not auth.service_account:
                raise NotifySendError("华为服务账号凭据缺失")
            # 断言 JWT 每次刷新现签：它本身一小时过期，缓存的是换回来的 access_token。
            # 私钥理论上在保存时已验过 PEM；这里再兜一层，签名失败压成终态错误，
            # 而不是让 ValueError 顺着 send 逃出（dispatch 外层虽兜底，测试按钮会 500）。
            try:
                assertion = _build_service_account_jwt(auth.service_account)
            except (KeyError, TypeError, ValueError, jwt.PyJWTError) as exc:
                raise NotifySendError(
                    "华为服务账号私钥无法签名 JWT，请重新粘贴服务账号 JSON"
                ) from exc
            form = {
                "grant_type": GRANT_JWT_BEARER,
                "assertion": assertion,
            }
        else:
            # 表单提交（application/x-www-form-urlencoded），不是 JSON。
            form = {
                "grant_type": GRANT_CLIENT_CREDENTIALS,
                "client_id": auth.client_id,
                "client_secret": auth.client_secret,
            }

        resp = await client.post(OAUTH_TOKEN_URL, data=form)
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

    目标三选一本模块只用 topic / token。类型契约两者不同，踩过 80100003：
    ``topic`` 是单个字符串，``token`` 是**字符串数组**（可一次最多 1000 台）。
    通知栏展示要写两处：顶层 ``notification`` 是全平台默认结构，而 HMS 对
    Android 投递还要求 ``android.notification``——只给顶层会被拒
    （80100003 "No valid android notification payload"）。
    ``data`` 是 HMS 规定的**字符串**，结构化数据需自行 JSON 编码，App 存活时
    经透传通道收到。
    """
    notification = {"title": title, "body": body}
    message: dict[str, Any] = {
        "notification": notification,
        "android": {
            "notification": {
                **notification,
                # click_action 必传（实测缺失即 80100003 "click action is required"）。
                # type 语义实测：1 开应用内页（要 intent/action）、2 开 URL（要 url）、
                # 3 打开 App 本体（无需附加字段）、4 要 rich_resource；0 非法。
                "click_action": {"type": 3},
            }
        },
    }
    if target_type == TARGET_TOPIC:
        message["topic"] = target
    elif target_type == TARGET_TOKEN:
        message["token"] = [target]
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
    auth: HuaweiAuth,
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
                client, auth, force_refresh=force_refresh
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
        detail = _business_error(result)
        # 刷新后仍 80200001：令牌本身有效，是服务账号身份没有推送权限（见常量注释）。
        if code in TOKEN_INVALID_CODES and auth.mode == GRANT_JWT_BEARER:
            detail = f"{detail}；{_SA_PUSH_PERMISSION_HINT}"
        return SendResult(ok=False, error=detail, elapsed_ms=elapsed)

    return SendResult(
        ok=False, error=last_error or "华为推送发送失败", elapsed_ms=_elapsed(started)
    )
