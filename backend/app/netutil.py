"""请求来源地址的取法。

线上跑在 nginx 反代后面（nanwa-gateway 仓库），``request.client.host`` 拿到的永远是
nginx 容器的地址：拿它当限流键，全站访客在防爆破计数器里就是同一个人，30 次失败上限
由第一个撞错密码的人替所有人买单。真实来源按 nginx 惯例在 ``X-Forwarded-For`` 的第一个
地址上。本地直跑（``scripts/dev.sh``、裸容器）没有这一层，自然回落到 ``request.client.host``。

这一维只是补充，账号维度（``AUTH_LOGIN_MAX_FAILURES``）才是防爆破的主力：调用方自己也能
写一份 ``X-Forwarded-For``，nginx 是追加不是覆盖，所以伪造头就能拿到一个新的 IP 键。
换用最后一跳（只有 nginx 能写的那一段）可以堵死伪造，但会把所有访客收敛到 nginx 的出口
地址、限掉所有人，对本机 1 worker + 家庭宽带的部署反而更糟。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request


def client_ip(request: Request) -> Optional[str]:
    """请求来源地址：优先 ``X-Forwarded-For`` 的第一个地址。

    截到 64 字符：``ai_call_log.client_ip`` 那一列就是这个长度。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None
