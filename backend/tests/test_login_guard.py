"""登录防爆破（login_guard）与图形验证码（captcha）的单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi import Request

from app.config import get_settings
from app.netutil import client_ip
from app.services import captcha, login_guard
from app.services.captcha import issue_captcha, verify_captcha

settings = get_settings()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    # 验证码令牌用 ENCRYPTION_KEY 加密：测试进程里它没有默认值，
    # 每次测试发一把新密钥并清掉 Fernet 实例缓存。
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    captcha._cipher.cache_clear()
    login_guard._counters.clear()
    yield
    login_guard._counters.clear()


# ---- 验证码（无状态令牌） ---------------------------------------------------


def test_issue_captcha_returns_png():
    captcha_id, png = issue_captcha()
    assert captcha_id
    assert png.startswith(b"\x89PNG")


def test_verify_captcha_roundtrip():
    captcha_id, _ = issue_captcha()
    # 答案只有 4 个字符且在字母表内：穷举出正确答案来验证正常路径。
    from app.services.captcha import _ALPHABET, _cipher

    expected = _cipher().decrypt(captcha_id.encode("ascii"))
    code = expected.decode("utf-8")
    assert len(code) == settings.AUTH_CAPTCHA_LENGTH
    assert all(ch.upper() in _ALPHABET for ch in code)
    # 大小写不敏感。
    assert verify_captcha(captcha_id, code.upper())
    assert verify_captcha(captcha_id, code)


def test_verify_captcha_wrong_code():
    captcha_id, _ = issue_captcha()
    # 字母表不含 '0'，所以这个答案必然错误。
    assert not verify_captcha(captcha_id, "0000")


def test_verify_captcha_tampered_token():
    captcha_id, _ = issue_captcha()
    tampered = captcha_id[:-4] + ("AAAA" if not captcha_id.endswith("AAAA") else "BBBB")
    assert not verify_captcha(tampered, "AB23")


def test_verify_captcha_expired(monkeypatch):
    captcha_id, _ = issue_captcha()
    from app.services.captcha import _cipher

    code = _cipher().decrypt(captcha_id.encode("ascii")).decode("utf-8")
    # TTL 设为 -1：任何令牌在解密时都立即视为过期。
    monkeypatch.setattr(settings, "AUTH_CAPTCHA_TTL", -1)
    assert not verify_captcha(captcha_id, code)


# ---- 防爆破计数 ------------------------------------------------------------


def test_lock_after_max_failures():
    for _ in range(settings.AUTH_LOGIN_MAX_FAILURES - 1):
        login_guard.record_failure("alice", "1.2.3.4")
    assert login_guard.locked_seconds("alice", "1.2.3.4") == 0

    login_guard.record_failure("alice", "1.2.3.4")
    assert login_guard.locked_seconds("alice", "1.2.3.4") > 0


def test_lock_is_scoped_to_username_and_ip():
    for _ in range(settings.AUTH_LOGIN_MAX_FAILURES):
        login_guard.record_failure("alice", "1.2.3.4")

    # 同 IP 换一个账号不受影响；同账号换一个 IP 也不受影响。
    assert login_guard.locked_seconds("bob", "1.2.3.4") == 0
    assert login_guard.locked_seconds("alice", "9.9.9.9") == 0


def test_ip_spray_locks_the_whole_ip():
    # 每个账号只失败一次（单账号维度不会锁），但累计到 IP 上限后整个 IP 被锁。
    for i in range(settings.AUTH_LOGIN_IP_MAX_FAILURES):
        login_guard.record_failure(f"user{i}", "5.6.7.8")

    assert login_guard.locked_seconds("never-tried", "5.6.7.8") > 0


def test_reset_after_success_clears_user_counter():
    for _ in range(settings.AUTH_LOGIN_MAX_FAILURES - 1):
        login_guard.record_failure("alice", "1.2.3.4")
    login_guard.reset_failures("alice", "1.2.3.4")

    # 计数已清零：再失败 上限-1 次也不该锁。
    for _ in range(settings.AUTH_LOGIN_MAX_FAILURES - 1):
        login_guard.record_failure("alice", "1.2.3.4")
    assert login_guard.locked_seconds("alice", "1.2.3.4") == 0


def test_window_expiry_resets_counter():
    login_guard.record_failure("alice", "1.2.3.4")
    counter = login_guard._counters[("alice", "1.2.3.4")]
    counter.window_start -= timedelta(seconds=settings.AUTH_LOGIN_LOCK_SECONDS + 1)

    login_guard.record_failure("alice", "1.2.3.4")
    assert login_guard._counters[("alice", "1.2.3.4")].failures == 1


def test_lock_expires():
    for _ in range(settings.AUTH_LOGIN_MAX_FAILURES):
        login_guard.record_failure("alice", "1.2.3.4")
    assert login_guard.locked_seconds("alice", "1.2.3.4") > 0

    counter = login_guard._counters[("alice", "1.2.3.4")]
    counter.locked_until = _utcnow() - timedelta(seconds=1)
    assert login_guard.locked_seconds("alice", "1.2.3.4") == 0


# ---- 反代后的来源地址 --------------------------------------------------------


def _request(forwarded: str | None = None, peer: str | None = "10.0.0.1") -> Request:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request({"type": "http", "headers": headers, "client": (peer, 54321) if peer else None})


def test_client_ip_takes_the_first_forwarded_hop():
    # 线上 nginx 后面 request.client.host 恒为网关容器：拿它当限流键，全站访客在
    # 计数器里就是同一个人，IP 上限由第一个撞错密码的人替所有人触发。
    assert client_ip(_request("203.0.113.7, 10.0.0.1")) == "203.0.113.7"


def test_client_ip_falls_back_to_the_peer():
    # 本地直跑没有反代，这一维回到对端地址。
    assert client_ip(_request()) == "10.0.0.1"
    assert client_ip(_request(peer=None)) is None


def test_client_ip_truncates_to_the_log_column_width():
    # ai_call_log.client_ip 是 String(64)，超了会当场炸在写日志上。
    assert len(client_ip(_request("x" * 200))) == 64
