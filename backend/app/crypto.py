"""用于存储型凭据的对称加密。

模型 API key 以 Fernet（AES-128-CBC + HMAC）加密后落盘存储。密钥保存在
``ENCRYPTION_KEY`` 中，永远不会写入数据库。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger("yangvis.crypto")


class DecryptionError(RuntimeError):
    """已存储的密文无法用当前密钥解密。"""


@lru_cache
def _cipher() -> Fernet:
    return Fernet(get_settings().ENCRYPTION_KEY.encode("utf-8"))


def encrypt(plaintext: str) -> bytes:
    """加密一个待存储的密钥。输入为空时返回值也为空。"""
    if not plaintext:
        return b""
    return _cipher().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes | None) -> str:
    """解密一个已存储的密钥。

    当密文与当前密钥不匹配时抛出 :class:`DecryptionError`。如果改为返回空字符串，
    看起来会和「用户从来没设置过 key」一模一样，而这正是本设计要极力避免的静默
    清空式故障：调用方必须抛出一个真正的错误，提示用户重新填写该凭据。
    """
    if not ciphertext:
        return ""
    try:
        return _cipher().decrypt(ciphertext).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        logger.error("failed to decrypt a stored api_key; ENCRYPTION_KEY may have changed")
        raise DecryptionError(
            "无法解密已保存的 api_key（加密密钥可能已变更），请重新填写"
        ) from exc


# ---- 掩码 -------------------------------------------------------------------

MASK_PREFIX = "sk-****"


def mask(secret: str) -> str:
    """渲染一个用于展示的密钥：绝不把明文返回给客户端。"""
    if not secret:
        return ""
    tail = secret[-4:] if len(secret) >= 4 else ""
    return f"{MASK_PREFIX}{tail}"


def is_masked(value: str) -> bool:
    """当客户端回传的是掩码值而非新密钥时返回 True。

    调用方据此保持已存储的密钥不变。没有这个判断，只修改标题的用户会把掩码提交
    上来，从而悄无声息地清掉自己的密钥——故障要等很久之后才会以「昨天还好好的」
    这种形式暴露出来。
    """
    return value.startswith(MASK_PREFIX)
