"""移动 App 的专用通知令牌、增量消息和 SSE 端点。

这套端点只服务于后台长连接，不使用控制台的 12 小时 JWT。令牌与 owner 必须
同时配置，避免个人部署的便利变成跨用户通知泄露。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.models.schemas import APIResponse, AppMessageItem, ListResponse
from app.security import TokenError, extract_bearer_token
from app.services.notify.app_inbox import list_after, list_latest
from app.services.notify.app_stream import AppStreamConnection, app_stream
from app.sse import SSE_HEADERS, sse_event, with_heartbeat

logger = logging.getLogger("yangvis.app_messages")

router = APIRouter(prefix="/app/v1", tags=["app-messages"])

_UNAUTHORIZED = HTTPException(status_code=401, detail="未授权")
_PAGE_SIZE = 100


def require_app_owner(authorization: Optional[str] = Header(default=None)) -> int:
    settings = get_settings()
    expected = settings.NOTIFY_APP_TOKEN
    owner_id = settings.NOTIFY_APP_OWNER_ID
    if not expected or owner_id is None:
        raise HTTPException(status_code=404, detail="接口不存在")
    try:
        token = extract_bearer_token(authorization)
    except TokenError as exc:
        logger.debug("rejecting app request: %s", exc)
        raise _UNAUTHORIZED from exc
    if not hmac.compare_digest(token, expected):
        logger.debug("rejecting app request: token mismatch")
        raise _UNAUTHORIZED
    return owner_id


@router.get("/messages", response_model=APIResponse[ListResponse[AppMessageItem]])
def list_messages(
    after_id: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    owner_id: int = Depends(require_app_owner),
) -> APIResponse[ListResponse[AppMessageItem]]:
    """无游标时取最新记录；有游标时按 ID 升序补拉。"""
    items = (
        list_latest(owner_id, limit)
        if after_id is None
        else list_after(owner_id, after_id, limit)
    )
    return APIResponse(data=ListResponse(items=items, total=len(items)))


async def _message_events(
    connection: AppStreamConnection, after_id: int
) -> AsyncIterator[str]:
    cursor = after_id
    try:
        # 先 register 再读 backlog：期间提交的消息会同时进队列，重复由 cursor 去掉。
        while True:
            batch = await asyncio.to_thread(
                list_after, connection.owner_id, cursor, _PAGE_SIZE
            )
            if not batch:
                break
            for message in batch:
                if message.id <= cursor:
                    continue
                cursor = message.id
                yield sse_event(
                    "message",
                    message.model_dump(mode="json"),
                    event_id=str(message.id),
                )
            if len(batch) < _PAGE_SIZE:
                break

        yield sse_event("ready", {"after_id": cursor})

        while True:
            message = await connection.next_message()
            if message is None:
                return
            if message.id <= cursor:
                continue
            cursor = message.id
            yield sse_event(
                "message",
                message.model_dump(mode="json"),
                event_id=str(message.id),
            )
    finally:
        app_stream.unregister(connection)


@router.get("/messages/stream")
async def stream_messages(
    after_id: int = Query(default=0, ge=0),
    owner_id: int = Depends(require_app_owner),
) -> StreamingResponse:
    connection = app_stream.register(owner_id)
    return StreamingResponse(
        with_heartbeat(_message_events(connection, after_id)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
