"""对外的 OpenAI 兼容端点。

刻意挂在站点根目录的 ``/v1`` 下而不是 ``/api/v1``：调用方填的 base_url 就是
``http://<host>:<port>/v1``，与所有 OpenAI SDK 的默认约定一致，不用为 yangvis
特殊处理。

鉴权用的是网关自己签发的密钥（:class:`AiApiKey`），不是控制台登录的 JWT——
两套凭据互不通用。错误响应也刻意不套项目的统一信封，而是 OpenAI 的
``{"error": {...}}`` 结构，否则标准客户端解析不出来。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import AiApiKey
from app.services import ai_gateway_proxy as proxy
from app.services.ai_gateway import extract_bearer_key, find_api_key, list_public_models

logger = logging.getLogger("yangvis.ai_gateway.openai")

router = APIRouter(prefix="/v1", tags=["ai-gateway"])

REQUEST_ID_HEADER = "X-Yangvis-Request-Id"


def _error(exc: proxy.GatewayError, request_id: Optional[str] = None) -> JSONResponse:
    # 认证/请求体阶段就被拒的调用没有 request_id，也没有日志行可查。
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return JSONResponse(status_code=exc.status_code, content=exc.to_openai_dict(), headers=headers)


def _upstream_error(status_code: int, content: bytes, request_id: str) -> Response:
    """把上游的错误原样回给调用方。

    原样透传而不是重写：上游的错误体里有调用方排障需要的细节（哪个参数非法、
    额度还剩多少），网关没有资格替它总结。
    """
    return Response(
        content=content,
        status_code=status_code,
        media_type="application/json",
        headers={REQUEST_ID_HEADER: request_id},
    )


def _reject_loop(request: Request) -> None:
    """入站请求带着网关自己的戳，说明某个通道的 base_url 指回了本站。

    放它进去就是无限递归：一圈圈套下去把 worker 占满，还会把调用日志刷爆。
    这一圈不留日志行也没关系——触发它的那次外层调用会把这个错误原样记下。
    """
    hop = request.headers.get(proxy.GATEWAY_HOP_HEADER)
    if not hop:
        return
    logger.warning("refusing a looped gateway request (marker %s)", hop)
    raise proxy.GatewayError(
        proxy.LOOP_STATUS,
        "检测到网关回环：某个上游通道的 base_url 指向了网关自己，请改成真实的上游地址",
        error_type="server_error",
        code="gateway_loop_detected",
    )


async def _authenticate(db: Session, authorization: Optional[str]) -> AiApiKey:
    plain = extract_bearer_key(authorization)
    if plain is None:
        raise proxy.GatewayError(
            401,
            "缺少 API 密钥：请在 Authorization 头里携带 Bearer <key>",
            error_type="authentication_error",
            code="invalid_api_key",
        )
    row = find_api_key(db, plain)
    # 无效与不存在给同一句话：区分它们只会让人拿着密钥列表来试探。
    if row is None:
        raise proxy.GatewayError(
            401,
            "API 密钥无效",
            error_type="authentication_error",
            code="invalid_api_key",
        )
    if not row.enabled:
        raise proxy.GatewayError(
            403,
            "API 密钥已停用",
            error_type="permission_error",
            code="key_disabled",
        )
    return row


async def _read_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise proxy.GatewayError(400, f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise proxy.GatewayError(400, "请求体必须是一个 JSON 对象")
    return body


def _client_ip(request: Request) -> Optional[str]:
    # 反代后面的真实来源：nginx 惯例是 X-Forwarded-For 的第一个地址。
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _prepare(
    request: Request, key: AiApiKey, body: Dict[str, Any], endpoint: str
) -> proxy.CallRecord:
    record = proxy.new_record(endpoint, key, _client_ip(request))
    record.model = body.get("model") if isinstance(body.get("model"), str) else None
    record.stream = bool(body.get("stream"))
    record.request_body = json.dumps(body, ensure_ascii=False)
    return record


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Response:
    try:
        _reject_loop(request)
        key = await _authenticate(db, authorization)
        body = await _read_body(request)
    except proxy.GatewayError as exc:
        return _error(exc)

    record = _prepare(request, key, body, proxy.CHAT_ENDPOINT)

    try:
        if record.stream:
            upstream, client, status, error_body = await proxy.open_stream(
                db, record, body, "/chat/completions"
            )
            if upstream is None or client is None:
                text = error_body.decode("utf-8", errors="replace")
                record.status_code = status
                record.success = False
                record.error = text
                record.response_body = text
                record.mark_latency()
                proxy.persist(record)
                return _upstream_error(status, error_body, record.request_id)

            record.status_code = status
            return StreamingResponse(
                proxy.relay_stream(upstream, client, record),
                status_code=status,
                media_type="text/event-stream",
                headers={
                    REQUEST_ID_HEADER: record.request_id,
                    "Cache-Control": "no-cache",
                    # nginx 默认会缓冲上游响应，不关掉就没有「打字机」效果。
                    "X-Accel-Buffering": "no",
                },
            )

        status, content = await proxy.forward_json(db, record, body, "/chat/completions")
    except proxy.GatewayError as exc:
        # 走到这里只可能是「模型没配」这类网关自身的拒绝。
        record.status_code = exc.status_code
        record.error = exc.message
        record.mark_latency()
        proxy.persist(record)
        logger.info(
            "rejected %s for key %s: %s", record.model, key.name, exc.message
        )
        return _error(exc, record.request_id)

    record.status_code = status
    record.success = status < 400
    text = content.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if record.success:
        proxy.apply_usage(record, proxy.extract_usage(payload))
        record.response_body = proxy.extract_chat_content(payload)
    else:
        record.error = text
        record.response_body = text
    record.mark_latency()
    proxy.persist(record)

    if not record.success:
        logger.info(
            "upstream %s returned %s for request %s", record.channel_name, status, record.request_id
        )
    return Response(
        content=content,
        status_code=status,
        media_type="application/json",
        headers={REQUEST_ID_HEADER: record.request_id},
    )


@router.post("/embeddings")
async def embeddings(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Response:
    try:
        _reject_loop(request)
        key = await _authenticate(db, authorization)
        body = await _read_body(request)
    except proxy.GatewayError as exc:
        return _error(exc)

    record = _prepare(request, key, body, proxy.EMBEDDINGS_ENDPOINT)
    # embeddings 没有流式形态，客户端传了也不该影响转发方式。
    record.stream = False
    body.pop("stream", None)

    try:
        status, content = await proxy.forward_json(db, record, body, "/embeddings")
    except proxy.GatewayError as exc:
        record.status_code = exc.status_code
        record.error = exc.message
        record.mark_latency()
        proxy.persist(record)
        return _error(exc, record.request_id)

    record.status_code = status
    record.success = status < 400
    text = content.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    if record.success and isinstance(payload, dict):
        proxy.apply_usage(record, proxy.extract_usage(payload))
        vectors = payload.get("data") or []
        # 向量本身没有审计价值（几万个浮点数），记条数与维度就够还原这次调用。
        first = vectors[0] if vectors and isinstance(vectors[0], dict) else {}
        dimensions = len(first.get("embedding") or first.get("vector") or [])
        record.response_body = json.dumps(
            {"count": len(vectors), "dimensions": dimensions, "model": payload.get("model")},
            ensure_ascii=False,
        )
    else:
        record.error = text
        record.response_body = text
    record.mark_latency()
    proxy.persist(record)

    return Response(
        content=content,
        status_code=status,
        media_type="application/json",
        headers={REQUEST_ID_HEADER: record.request_id},
    )


@router.get("/models")
async def models(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Response:
    """列出当前对外可用的模型名。

    刻意不写调用日志：它没有内容也不产生费用，而很多 SDK 每次请求前都会先拉一
    遍模型列表，记下来只会把真正需要审计的调用淹掉。
    """
    try:
        _reject_loop(request)
        await _authenticate(db, authorization)
    except proxy.GatewayError as exc:
        return _error(exc)

    data = [
        {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "yangvis",
        }
        for name in list_public_models(db)
    ]
    return JSONResponse(content={"object": "list", "data": data})
