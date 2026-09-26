"""AI 网关的对外转发：OpenAI 兼容协议 → 多个上游厂商。

设计上的四条硬约束：

1. **绝不把调用方的 Authorization 透传给上游**。网关密钥和上游密钥是两套凭据，
   透传等于把对外密钥泄露给每一个上游厂商。
2. **故障转移只能发生在「还没往客户端写第一个字节」之前**。SSE 一旦开始就
   没法回头，因此转移判定全部基于上游的响应状态码。
3. **留痕失败不影响调用**。日志写库异常只记 error，不让业务请求变成 5xx。
4. **绝不与自己对打**。出站请求一律盖 :data:`GATEWAY_HOP_HEADER` 戳，入站再
   看到它就按 :data:`LOOP_STATUS` 拒绝——通道 base_url 指回本站时会无限递归，
   而内网别名是保存时的 host 校验认不出来的。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models.entities import AiApiKey, AiCallLog
from app.services.ai_gateway import (
    RouteCandidate,
    resolve_candidates,
    truncate_text,
    write_call_log,
)

logger = logging.getLogger("yangvis.ai_gateway_proxy")

settings = get_settings()

CHAT_ENDPOINT = "chat/completions"
EMBEDDINGS_ENDPOINT = "embeddings"
MODELS_ENDPOINT = "models"

# 这些状态码说明「这条通道现在不行」而不是「请求本身有问题」，换一条通道再试
# 是有意义的。400 不在其中：参数非法换谁都一样，重试只是拖长失败时间。
FAILOVER_STATUS = frozenset({401, 403, 404, 408, 429, 500, 502, 503, 504})

# 转发出去的每个上游请求都盖这个戳。它再出现在入站请求里，就说明某个通道的
# base_url 指回了本站——保存时按 host 校验认不出内网别名（compose 服务名、
# 公网 IP），只能靠这个戳在打出去之前掐断，否则请求会在网关里一圈圈套下去。
GATEWAY_HOP_HEADER = "X-Yangvis-Gateway"
LOOP_STATUS = 508  # RFC 658 Loop Detected；不在 FAILOVER_STATUS 里，换通道也没用


class GatewayError(Exception):
    """网关自身拒绝了请求（鉴权失败、模型未配置等）。"""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_type: str = "invalid_request_error",
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code

    def to_openai_dict(self) -> Dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": None,
                "code": self.code,
            }
        }


@dataclass
class CallRecord:
    """一次调用的记账本，转发过程中不断往里填，结束时一次性落库。"""

    request_id: str
    endpoint: str
    key: Optional[AiApiKey]
    model: Optional[str] = None
    stream: bool = False
    client_ip: Optional[str] = None
    channel_id: Optional[int] = None
    channel_name: Optional[str] = None
    upstream_model: Optional[str] = None
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    status_code: int = 0
    success: bool = False
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    first_token_ms: Optional[int] = None
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    started_at: float = field(default_factory=time.perf_counter)

    def mark_latency(self) -> None:
        self.latency_ms = int((time.perf_counter() - self.started_at) * 1000)

    def to_entity(self) -> AiCallLog:
        store_payload = settings.AI_GATEWAY_LOG_PAYLOAD
        return AiCallLog(
            request_id=self.request_id,
            key_id=self.key.id if self.key else None,
            key_name=self.key.name if self.key else None,
            endpoint=self.endpoint,
            model=self.model,
            stream=self.stream,
            channel_id=self.channel_id,
            channel_name=self.channel_name,
            upstream_model=self.upstream_model,
            attempts=self.attempts or None,
            status_code=self.status_code,
            success=self.success,
            error=self.error[:512] if self.error else None,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            latency_ms=self.latency_ms,
            first_token_ms=self.first_token_ms,
            client_ip=self.client_ip,
            request_body=truncate_text(self.request_body) if store_payload else None,
            response_body=truncate_text(self.response_body) if store_payload else None,
        )


def persist(record: CallRecord) -> None:
    """把一次调用写进 ai_call_log，并回填密钥的调用计数。

    流式响应的日志要等流结束才写得出来，那时请求作用域的 session 早已关闭，
    所以这里自己开一个。
    """
    with SessionLocal() as db:
        if record.key is not None:
            row = db.get(AiApiKey, record.key.id)
            if row is not None:
                row.call_count = (row.call_count or 0) + 1
                row.last_used_at = datetime.now()
        write_call_log(db, record.to_entity())


def new_record(endpoint: str, key: Optional[AiApiKey], client_ip: Optional[str]) -> CallRecord:
    return CallRecord(
        request_id=uuid.uuid4().hex,
        endpoint=endpoint,
        key=key,
        client_ip=client_ip,
    )


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        settings.AI_GATEWAY_READ_TIMEOUT,
        connect=settings.AI_GATEWAY_CONNECT_TIMEOUT,
    )


def _upstream_headers(candidate: RouteCandidate, record: CallRecord) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {candidate.upstream_key}",
        "Content-Type": "application/json",
        # 带上本次调用的 request_id：万一真的绕回来了，两端日志能对上。
        GATEWAY_HOP_HEADER: record.request_id,
    }


def _upstream_url(candidate: RouteCandidate, path: str) -> str:
    return f"{candidate.channel.base_url.rstrip('/')}/{path.lstrip('/')}"


# ---- 内容提取 ---------------------------------------------------------------


def extract_chat_content(payload: Any) -> str:
    """从一份 OpenAI 风格的 chat 响应里取出助手回答的文本。"""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    # 多模态分段（[{"type":"text","text":...}]）也要能审计，拼出其中的文本。
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def extract_usage(payload: Any) -> Optional[Dict[str, int]]:
    """取出 usage 三元组；上游没给就返回 None。"""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def apply_usage(record: CallRecord, usage: Optional[Dict[str, int]]) -> None:
    if not usage:
        return
    record.prompt_tokens = usage["prompt_tokens"]
    record.completion_tokens = usage["completion_tokens"]
    # 部分厂商只给分项不给总数，这里兜一手加法。
    record.total_tokens = usage["total_tokens"] or (
        usage["prompt_tokens"] + usage["completion_tokens"]
    )


class StreamAccumulator:
    """旁路解析 SSE，把流式回答的内容与 usage 攒出来。

    转发本身是原样透传字节（不重新序列化，避免破坏各家厂商的字段差异），这里
    只是同时读一份用于统计与审计。解析失败一律静默：统计不到远比打断转发好。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self.parts: List[str] = []
        self.usage: Optional[Dict[str, int]] = None

    def feed(self, chunk: bytes) -> None:
        try:
            self._buffer += chunk.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001 - 解码不该打断转发
            return

        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._consume_line(line.strip())

    def _consume_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return

        usage = extract_usage(payload)
        if usage:
            self.usage = usage

        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.parts.append(content)

    @property
    def text(self) -> str:
        return "".join(self.parts)


# ---- 转发 -------------------------------------------------------------------


def _pick_candidates(db: Session, record: CallRecord) -> List[RouteCandidate]:
    if not record.model:
        raise GatewayError(400, "请求体缺少 model 字段", code="missing_model")
    candidates = resolve_candidates(db, record.model)[: settings.AI_GATEWAY_MAX_ATTEMPTS]
    if not candidates:
        raise GatewayError(
            404,
            f"模型 {record.model} 未在网关中配置，或对应通道已停用",
            error_type="model_not_found",
            code="model_not_found",
        )
    return candidates


async def forward_json(
    db: Session,
    record: CallRecord,
    body: Dict[str, Any],
    path: str,
) -> Tuple[int, bytes]:
    """非流式转发：按优先级依次尝试，返回（状态码, 响应字节）。

    整个响应读完才返回，因此换通道重试对调用方完全透明。命中的通道记在
    ``record`` 上，与日志一起落库。
    """
    candidates = _pick_candidates(db, record)
    last: Tuple[int, bytes] = (503, b'{"error":{"message":"no upstream available"}}')

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        for index, candidate in enumerate(candidates):
            upstream_body = dict(body)
            upstream_body["model"] = candidate.upstream_model
            started = time.perf_counter()
            try:
                response = await client.post(
                    _upstream_url(candidate, path),
                    json=upstream_body,
                    headers=_upstream_headers(candidate, record),
                )
            except httpx.HTTPError as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                message = f"{type(exc).__name__}: {exc}"
                record.attempts.append(
                    {
                        "channel_id": candidate.channel.id,
                        "channel_name": candidate.channel.name,
                        "status_code": 0,
                        "error": message[:512],
                        "latency_ms": elapsed,
                    }
                )
                logger.warning(
                    "upstream %s unreachable for model %s: %s",
                    candidate.channel.name,
                    record.model,
                    exc,
                )
                last = (502, _error_bytes(f"上游通道 {candidate.channel.name} 不可达：{exc}"))
                continue

            if response.status_code in FAILOVER_STATUS and index + 1 < len(candidates):
                record.attempts.append(
                    {
                        "channel_id": candidate.channel.id,
                        "channel_name": candidate.channel.name,
                        "status_code": response.status_code,
                        "error": response.text[:512],
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                    }
                )
                last = (response.status_code, response.content)
                continue

            record.channel_id = candidate.channel.id
            record.channel_name = candidate.channel.name
            record.upstream_model = candidate.upstream_model
            return response.status_code, response.content

    return last[0], last[1]


async def open_stream(
    db: Session,
    record: CallRecord,
    body: Dict[str, Any],
    path: str,
) -> Tuple[Optional[httpx.Response], Optional[httpx.AsyncClient], int, bytes]:
    """流式转发：返回（已打开的上游响应, 需要在流结束后关闭的 client, 状态码, 错误体）。

    成功时前两项非空，调用方负责在生成器的 finally 里关掉它们；失败时 client
    已在此处关闭，错误体可直接回给调用方。
    """
    candidates = _pick_candidates(db, record)
    # 流式默认注入 include_usage 拿 token 数；个别上游不认这个字段，撞上之后
    # 就地降级重试同一条通道（记在 tries 里而不是写死一层嵌套）。
    inject_usage = bool(body.get("stream")) and "stream_options" not in body

    tries: List[Tuple[RouteCandidate, bool]] = [
        (candidate, inject_usage) for candidate in candidates
    ]
    last_status = 503
    last_body = _error_bytes("没有可用的上游通道")

    index = 0
    while index < len(tries):
        candidate, inject = tries[index]
        index += 1

        upstream_body = dict(body)
        upstream_body["model"] = candidate.upstream_model
        if inject:
            upstream_body["stream_options"] = {"include_usage": True}

        client = httpx.AsyncClient(timeout=_timeout())
        started = time.perf_counter()
        try:
            request = client.build_request(
                "POST",
                _upstream_url(candidate, path),
                json=upstream_body,
                headers=_upstream_headers(candidate, record),
            )
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            elapsed = int((time.perf_counter() - started) * 1000)
            record.attempts.append(
                {
                    "channel_id": candidate.channel.id,
                    "channel_name": candidate.channel.name,
                    "status_code": 0,
                    "error": f"{type(exc).__name__}: {exc}"[:512],
                    "latency_ms": elapsed,
                }
            )
            last_status, last_body = 502, _error_bytes(
                f"上游通道 {candidate.channel.name} 不可达：{exc}"
            )
            continue

        if response.status_code < 400:
            record.channel_id = candidate.channel.id
            record.channel_name = candidate.channel.name
            record.upstream_model = candidate.upstream_model
            return response, client, response.status_code, b""

        error_body = await response.aread()
        await response.aclose()
        await client.aclose()
        text = error_body.decode("utf-8", errors="replace")

        # 上游不认 stream_options：同一条通道去掉它再试一次，别因此判定通道坏了。
        if inject and response.status_code in (400, 422) and "stream_options" in text:
            tries.insert(index, (candidate, False))
            continue

        record.attempts.append(
            {
                "channel_id": candidate.channel.id,
                "channel_name": candidate.channel.name,
                "status_code": response.status_code,
                "error": text[:512],
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        )
        last_status, last_body = response.status_code, error_body
        if response.status_code not in FAILOVER_STATUS:
            # 请求本身有问题（比如参数非法），换通道也是同样的结果。
            break

    return None, None, last_status, last_body


def _error_bytes(message: str) -> bytes:
    payload = {
        "error": {
            "message": message[:512],
            "type": "upstream_error",
            "param": None,
            "code": None,
        }
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


async def relay_stream(
    response: httpx.Response,
    client: httpx.AsyncClient,
    record: CallRecord,
) -> AsyncIterator[bytes]:
    """把上游的 SSE 原样透传给调用方，同时旁路攒出内容与 usage。

    这是个 async generator：日志只能在流真正结束之后写（token 数、完整回答、
    是否中途断连都要等最后才知道），所以落库放在 finally 里。客户端中途断开时
    Starlette 会往这里丢 GeneratorExit，同样走 finally，已经产生的内容不丢。
    """
    accumulator = StreamAccumulator()
    disconnected = False
    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            if record.first_token_ms is None:
                record.first_token_ms = int(
                    (time.perf_counter() - record.started_at) * 1000
                )
            accumulator.feed(chunk)
            yield chunk
    except (httpx.HTTPError, httpx.StreamError) as exc:
        record.error = f"上游流中断：{exc}"
        logger.warning("upstream stream broke for request %s: %s", record.request_id, exc)
    except (GeneratorExit, asyncio.CancelledError):
        # 客户端自己断开：不是上游的错，也不该记成网关失败。
        disconnected = True
        raise
    except Exception as exc:  # noqa: BLE001 - 转发中任何异常都要留痕
        record.error = f"流式转发异常：{type(exc).__name__}: {exc}"
        logger.exception("stream relay failed for request %s", record.request_id)
    finally:
        for closer in (response.aclose, client.aclose):
            try:
                await closer()
            except Exception:  # noqa: BLE001 - 关闭失败不该盖掉真正的错误
                logger.debug("failed to close the upstream stream", exc_info=True)

        text = accumulator.text
        apply_usage(record, accumulator.usage)
        record.response_body = text or None
        if disconnected:
            record.error = record.error or "客户端提前断开连接"
            # 已经吐出了内容就算这次调用成立；一个字没给就断开记成失败。
            record.success = bool(text)
        else:
            record.success = record.error is None
        record.mark_latency()
        persist(record)


# ---- 通道连通性测试 ---------------------------------------------------------


async def test_channel(
    base_url: str, api_key: str, model: str
) -> Tuple[bool, str]:
    """往 ``{base_url}/chat/completions`` 发一条最短的 prompt。

    不用 langchain 客户端：网关转发本身走的就是裸 HTTP，测试走同一条路径才能
    保证「测通了 = 转发也能通」。
    """
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply with the single word: ok"},
            {"role": "user", "content": "ping"},
        ],
        "stream": False,
    }
    headers = {"Content-Type": "application/json", GATEWAY_HOP_HEADER: "channel-test"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions", json=body, headers=headers
            )
    except httpx.HTTPError as exc:
        return False, f"无法连接上游：{exc}"

    if response.status_code >= 400:
        return False, f"上游返回 {response.status_code}：{response.text[:200]}"

    try:
        content = extract_chat_content(response.json())
    except json.JSONDecodeError:
        return False, "上游返回的不是合法 JSON，请确认 base_url 指向 OpenAI 兼容接口"
    return True, f"连接成功，模型返回：{(content or '(空响应)')[:64]}"
