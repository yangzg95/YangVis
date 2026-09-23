"""登录防爆破：失败计数与锁定（进程内存）。

按部署要求计数放内存、不落库。注意一个已知取舍：gunicorn 多 worker 部署下
每个进程各持一份计数，攻击者实际拿到的额度约为「配置上限 × worker 数」；
单 worker（开发环境、小部署）下语义是精确的。重启后计数清零，可接受——
锁定本来就是短期惩罚。

两个维度：

- (username, ip)：同一账号在同一来源上、一个窗口内的失败次数，达到
  AUTH_LOGIN_MAX_FAILURES 即锁定 AUTH_LOGIN_LOCK_SECONDS；登录成功立即清零。
- ("", ip)：整个 IP 在滑动窗口内的失败总数，达到 AUTH_LOGIN_IP_MAX_FAILURES
  同样锁定。挡的是「一个账号被锁就换下一个账号」的密码喷洒。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from app.config import get_settings

settings = get_settings()

# IP 维度计数键的 username 占位值。登录名经 schema 校验不允许为空，不会撞上。
_IP_ROW_USERNAME = ""


class _Counter:
    __slots__ = ("failures", "window_start", "locked_until")

    def __init__(self, now: datetime) -> None:
        self.failures = 0
        self.window_start = now
        self.locked_until: datetime | None = None


_counters: dict[tuple[str, str], _Counter] = {}
_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _locked_remaining(counter: _Counter | None, now: datetime) -> int:
    if counter and counter.locked_until and counter.locked_until > now:
        return int((counter.locked_until - now).total_seconds())
    return 0


def _prune(now: datetime) -> None:
    """清掉既未锁定、窗口也早已过期的条目，字典不会只涨不消。

    调用方必须已持有 ``_lock``。
    """
    horizon = now - timedelta(seconds=settings.AUTH_LOGIN_LOCK_SECONDS)
    stale = [
        key
        for key, counter in _counters.items()
        if counter.window_start < horizon
        and (counter.locked_until is None or counter.locked_until < now)
    ]
    for key in stale:
        del _counters[key]


def locked_seconds(username: str, ip: str) -> int:
    """该账号/IP 当前剩余的锁定秒数；0 表示可以放手尝试。"""
    now = _utcnow()
    with _lock:
        _prune(now)
        return max(
            _locked_remaining(_counters.get((username, ip)), now),
            _locked_remaining(_counters.get((_IP_ROW_USERNAME, ip)), now),
        )


def _bump(username: str, ip: str, max_failures: int, now: datetime) -> None:
    """给一维计数 +1，窗口过期先归零，达到上限则锁定。

    调用方必须已持有 ``_lock``。
    """
    counter = _counters.get((username, ip))
    if counter is None:
        counter = _Counter(now)
        _counters[(username, ip)] = counter
    if now - counter.window_start > timedelta(seconds=settings.AUTH_LOGIN_LOCK_SECONDS):
        counter.failures = 0
        counter.window_start = now
        counter.locked_until = None
    counter.failures += 1
    if counter.failures >= max_failures:
        counter.locked_until = now + timedelta(seconds=settings.AUTH_LOGIN_LOCK_SECONDS)
        # 锁定后计数归零：解锁时是一张干净的窗口，而不是立刻又触发锁定。
        counter.failures = 0
        counter.window_start = now


def record_failure(username: str, ip: str) -> None:
    now = _utcnow()
    with _lock:
        _bump(username, ip, settings.AUTH_LOGIN_MAX_FAILURES, now)
        _bump(_IP_ROW_USERNAME, ip, settings.AUTH_LOGIN_IP_MAX_FAILURES, now)


def reset_failures(username: str, ip: str) -> None:
    """登录成功后清掉 (username, ip) 的计数。

    IP 维度的窗口计数不清：NAT 出口下一个正常用户的成功登录，不该替同源
    的攻击流量销账；它反正会随窗口过期自然归零。
    """
    with _lock:
        _counters.pop((username, ip), None)
