"""自建认证所用的密码哈希与 JWT 辅助函数（见 §7）。

不引入 passlib/bcrypt：标准库里的 ``hashlib.pbkdf2_hmac`` 已经够用，也能让依赖
列表保持精简。迭代次数被写在哈希字符串内部，因此以后调高它不会让已有密码失效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt

from app.config import get_settings

settings = get_settings()

_ALGORITHM = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 260_000
_SALT_BYTES = 16


# ---- 密码哈希 ----------------------------------------------------------------

def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """把明文密码哈希成 ``pbkdf2_sha256$iterations$salt$hash`` 格式。"""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """以常数时间把明文密码与已存储的哈希做比对。

    遇到格式损坏的哈希时返回 ``False`` 而不是抛异常：一条损坏的记录应该被当作
    「密码错误」，而不是变成 500。
    """
    if not password or not encoded:
        return False
    try:
        algorithm, iterations_raw, salt_raw, hash_raw = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_raw)
        salt = _b64decode(salt_raw)
        expected = _b64decode(hash_raw)
    except (ValueError, TypeError):
        return False

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


# ---- JWT -------------------------------------------------------------------

class TokenError(Exception):
    """提交上来的 token 缺失、格式错误、已过期或签名不正确。"""


def create_access_token(
    *,
    user_id: int,
    username: str,
    is_admin: bool,
    ttl_seconds: Optional[int] = None,
) -> str:
    """签发一个 access token。不做 refresh token：这是一个内部工具。"""
    ttl = ttl_seconds if ttl_seconds is not None else settings.AUTH_TOKEN_TTL
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "is_admin": is_admin,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, settings.AUTH_JWT_SECRET, algorithm=settings.AUTH_JWT_ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """校验签名与有效期，并返回 payload。"""
    try:
        return jwt.decode(
            token,
            settings.AUTH_JWT_SECRET,
            algorithms=[settings.AUTH_JWT_ALGORITHM],
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc


def extract_bearer_token(authorization: Optional[str]) -> str:
    """从 Authorization 请求头里取出原始 token。

    SPA 目前发送的是裸 token（见 ``frontend/src/api/index.ts``），因此
    ``Bearer <token>`` 和 ``<token>`` 两种形式都接受。
    """
    if not authorization:
        raise TokenError("missing Authorization header")
    value = authorization.strip()
    if not value:
        raise TokenError("empty Authorization header")
    prefix, _, remainder = value.partition(" ")
    if prefix.lower() == "bearer":
        token = remainder.strip()
        if not token:
            raise TokenError("empty bearer token")
        return token
    return value


# ---- WebSocket 入场票 --------------------------------------------------------

_WS_SCOPE = "ws"


def create_ws_ticket(*, user_id: int, username: str, is_admin: bool) -> str:
    """签发一张只能用来建立 WebSocket 连接的短时票。

    浏览器的 ``WebSocket`` 构造函数没法设请求头，token 只能走 query string，
    而 query string 会进 nginx 访问日志。所以这里不复用那张 12 小时的
    access token，而是单签一张 60 秒、且带 ``scope=ws`` 的票：即使日志被人
    翻到，它也早就过期了，何况拿它去调 REST 接口会被 scope 挡掉。

    用签名票而不是服务端一次性 token，是因为线上跑的是 ``gunicorn -w 4``，
    四个 worker 之间不共享内存；签名票任何一个 worker 都能独立验。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "is_admin": is_admin,
        "scope": _WS_SCOPE,
        "iat": now,
        "exp": now + timedelta(seconds=settings.OPS_WS_TICKET_TTL),
    }
    return jwt.encode(payload, settings.AUTH_JWT_SECRET, algorithm=settings.AUTH_JWT_ALGORITHM)


def decode_ws_ticket(ticket: str) -> Dict[str, Any]:
    """校验一张 WebSocket 入场票，并确认它确实是票而不是普通 access token。"""
    payload = decode_access_token(ticket)
    if payload.get("scope") != _WS_SCOPE:
        raise TokenError("not a websocket ticket")
    return payload
