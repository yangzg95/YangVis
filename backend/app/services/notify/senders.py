"""四种机器人方言的 payload 构造、加签与实际发送。

纯函数（构造 / 加签）与 IO（发送）分开：前者是各家文档算法的翻译，单测直接
对着已知向量钉；后者只有 ``send_to_channel`` 一个入口，dispatch 统一持有
``httpx.AsyncClient`` 复用连接。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

import httpx

# 机器人 webhook 的响应都是即答的小 JSON，10 秒足够覆盖跨地域的抖动。
TIMEOUT = httpx.Timeout(10.0)


class NotifySendError(RuntimeError):
    """发送失败的可读原因（已包含渠道侧的 errcode/errmsg）。"""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None


# ---- 飞书加签 ---------------------------------------------------------------


def feishu_sign(timestamp: int, secret: str) -> str:
    """飞书自定义机器人签名。

    算法（官方文档）：以 ``"{timestamp}\\n{secret}"`` 为 **key**、空串为消息，
    HmacSHA256 后 base64。key 与 message 的位置和钉钉正好相反，两家都写一遍
    才不至于在排查「sign 不匹配」时互相污染。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


# ---- 钉钉加签 ---------------------------------------------------------------


def dingtalk_sign(timestamp_ms: int, secret: str) -> str:
    """钉钉加签机器人的签名（URL 编码后的 base64）。

    算法（官方文档）：以 secret 为 key、``"{timestamp}\\n{secret}"`` 为消息，
    HmacSHA256 后 base64，再对整个串做 ``quote_plus``。
    """
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))


# ---- payload 构造 -------------------------------------------------------------


def build_payload(
    kind: str,
    *,
    event: str,
    title: str,
    body: str,
    text: str,
    secret: Optional[str] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """按渠道方言构造请求体。

    飞书的签名校验是**请求体字段**（``timestamp`` 秒级 + ``sign``），不是 URL
    参数；钉钉反过来，签名只能挂在 URL 上（见 :func:`signed_url`）。两家弄反的
    后果不一样：钉钉挂 body 会被验签拒绝，飞书挂 URL 会把 hook 路径带脏
    （``.../hook/abc&timestamp=…``）——飞书直接回 HTTP 404。
    """
    if kind == "feishu":
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if secret:
            ts = int(now if now is not None else time.time())
            payload["timestamp"] = str(ts)
            payload["sign"] = feishu_sign(ts, secret)
        return payload
    if kind == "wecom":
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    # generic：结构化字段，给自建接收端解析用。
    return {
        "event": event,
        "title": title,
        "body": body,
        "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def signed_url(kind: str, webhook_url: str, secret: Optional[str], *, now: Optional[int] = None) -> str:
    """钉钉加签机器人：把毫秒时间戳与签名挂到 URL query，其余渠道原样返回。

    钉钉自己的地址一定带 ``?access_token=``，但自建反代可能没有 query，这里按
    URL 现状选 ``?`` 或 ``&``——拼错分隔符同样会得到一个 404。
    """
    if kind != "dingtalk" or not secret:
        return webhook_url
    ts_ms = int((now if now is not None else time.time()) * 1000)
    sep = "&" if urllib.parse.urlparse(webhook_url).query else "?"
    return f"{webhook_url}{sep}timestamp={ts_ms}&sign={dingtalk_sign(ts_ms, secret)}"


# ---- 响应判定 -------------------------------------------------------------


def _check_response(kind: str, payload: dict[str, Any]) -> None:
    """把渠道的错误码翻译成一个可读异常；通过则静默返回。

    飞书新版返回 ``{"code": 0}``，旧版 ``{"StatusCode": 0}``；两家 IM 的
    错误码语义各自独立，只认「码为零或缺失」为成功，其余一律报错。
    """
    if kind == "feishu":
        code = payload.get("code", payload.get("StatusCode", 0))
        if code != 0:
            raise NotifySendError(
                f"飞书返回 code={code} msg={payload.get('msg') or payload.get('StatusMessage')}"
            )
        return
    if kind in ("wecom", "dingtalk"):
        code = payload.get("errcode", 0)
        if code != 0:
            raise NotifySendError(
                f"{'企业微信' if kind == 'wecom' else '钉钉'}返回 errcode={code} "
                f"errmsg={payload.get('errmsg')}"
            )
        return
    # generic：HTTP 2xx 即成功，不看正文。


# 反代、网关与自建接收端的错误正文里常整段复述请求 URL（含 access_token /
# key / sign），这些串会落进 notify_log.error 并在设置页回显，先抹掉值再截断。
_CREDENTIAL_QUERY = re.compile(r"(?P<key>access_token|token|api_key|key|secret|sign)\s*=\s*[^\s&\"']+")

# 正文片段的上限：够放下一条 JSON 错误，又不至于把 512 的列撑成一屏日志。
_MAX_ERROR_BODY = 200


def _http_error(resp: httpx.Response) -> str:
    """HTTP 状态码 + 可读的正文片段（只认 JSON）。

    非 JSON（HTML 错误页）一律丢弃：机器人侧的失败原因都在 JSON 里，而网关
    的 HTML 往往把请求 URL 原样印出来，那种正文对排查没帮助、只有泄漏风险。
    """
    base = f"HTTP {resp.status_code}"
    try:
        data = resp.json()
    except (ValueError, UnicodeDecodeError):
        return base
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    text = " ".join(_CREDENTIAL_QUERY.sub(r"\g<key>=***", text).split())
    return f"{base}：{text[:_MAX_ERROR_BODY]}" if text else base


# ---- 发送 -------------------------------------------------------------------


async def send_to_channel(
    client: httpx.AsyncClient,
    *,
    kind: str,
    webhook_url: str,
    secret: Optional[str],
    event: str,
    title: str,
    body: str,
    text: str,
) -> SendResult:
    """向一个渠道发送一条消息，带一次重试。

    重试只针对连接层错误与 5xx（可能是瞬时过载）；4xx 是终态——地址错了、
    被限流，重试同样会被拒，白等十秒。渠道侧业务错误（errcode 非零）与
    响应不是 JSON 也都是终态。任何失败都压成 :class:`SendResult` 返回，
    调用方（dispatch）只负责把结果落进 ``notify_log``。

    ``secret`` 同时喂给两条加签路径：钉钉进 URL，飞书进请求体。
    """
    url = signed_url(kind, webhook_url, secret)
    payload = build_payload(
        kind, event=event, title=title, body=body, text=text, secret=secret
    )
    started = time.monotonic()

    last_error: Optional[str] = None
    for _attempt in range(2):
        try:
            resp = await client.post(url, json=payload)
            elapsed = int((time.monotonic() - started) * 1000)
            if resp.status_code >= 500:
                last_error = _http_error(resp)
                continue
            if resp.status_code >= 400:
                return SendResult(ok=False, error=_http_error(resp), elapsed_ms=elapsed)
            # generic 是自建接收端，常见返回纯文本（"ok"），不按 JSON 解析。
            if kind != "generic":
                _check_response(kind, resp.json())
            return SendResult(ok=True, elapsed_ms=elapsed)
        except httpx.TransportError as exc:
            last_error = f"网络错误：{exc}"
            continue
        except (NotifySendError, ValueError) as exc:
            # ValueError：渠道返回了 2xx 但正文不是 JSON。
            return SendResult(
                ok=False,
                error=str(exc),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    return SendResult(
        ok=False,
        error=last_error or "发送失败",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
