"""Chat endpoints: conversations, history and the streaming answer."""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.errors import BusinessError
from app.models.schemas import (
    APIResponse,
    CompletionRequest,
    ConversationCreate,
    ConversationItem,
    ConversationUpdate,
    CurrentUser,
    ListResponse,
    MessageItem,
    MessageUpdate,
    OpsActionItem,
    OpsConversationRequest,
)
from app.services.chat import ROLE_USER, ChatService, derive_title

logger = logging.getLogger("yangvis.chat")

router = APIRouter(prefix="/chat", tags=["chat"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> ChatService:
    """Bind the service to the caller so handlers never see a bare session."""
    return ChatService(db, user.user_id)


# ---- Conversations ----------------------------------------------------------

@router.get("/conversations", response_model=APIResponse[ListResponse[ConversationItem]])
async def list_conversations(
    service: ChatService = Depends(get_service),
) -> APIResponse[ListResponse[ConversationItem]]:
    items = service.list_conversations()
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/conversations", response_model=APIResponse[ConversationItem])
async def create_conversation(
    # Defaulted so a bare POST works: "start a new chat" carries no data.
    payload: ConversationCreate = ConversationCreate(),
    service: ChatService = Depends(get_service),
) -> APIResponse[ConversationItem]:
    try:
        row = service.create_conversation(
            title=payload.title,
            agent_id=payload.agent_id,
            project_id=payload.project_id,
            type_ids=payload.type_ids,
            model_config_id=payload.model_config_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(
        data=ConversationItem(
            id=row.id,
            title=row.title,
            agent_id=row.agent_id,
            project_id=row.project_id,
            type_ids=row.type_ids,
            model_config_id=row.model_config_id,
            message_count=0,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
    )


@router.put("/conversations/{conversation_id}", response_model=APIResponse[ConversationItem])
async def update_conversation(
    conversation_id: int,
    payload: ConversationUpdate,
    service: ChatService = Depends(get_service),
) -> APIResponse[ConversationItem]:
    try:
        row = service.update_conversation(
            conversation_id,
            title=payload.title,
            agent_id=payload.agent_id,
            project_id=payload.project_id,
            type_ids=payload.type_ids,
            model_config_id=payload.model_config_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(
        data=ConversationItem(
            id=row.id,
            title=row.title,
            agent_id=row.agent_id,
            project_id=row.project_id,
            type_ids=row.type_ids,
            model_config_id=row.model_config_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
    )


@router.delete("/conversations/{conversation_id}", response_model=APIResponse[None])
async def delete_conversation(
    conversation_id: int,
    service: ChatService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_conversation(conversation_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=APIResponse[ListResponse[MessageItem]],
)
async def list_messages(
    conversation_id: int,
    service: ChatService = Depends(get_service),
) -> APIResponse[ListResponse[MessageItem]]:
    try:
        items = service.list_messages(conversation_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get(
    "/conversations/{conversation_id}/actions",
    response_model=APIResponse[ListResponse[OpsActionItem]],
)
async def list_actions(
    conversation_id: int,
    service: ChatService = Depends(get_service),
) -> APIResponse[ListResponse[OpsActionItem]]:
    """一个会话的全部运维待确认项：历史回放时确认卡片靠它恢复。"""
    try:
        items = service.list_actions(conversation_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=ListResponse(items=items, total=len(items)))


# ---- 运维问答会话与写命令确认 -------------------------------------------------


@router.post("/ops-conversation", response_model=APIResponse[ConversationItem])
async def find_ops_conversation(
    payload: OpsConversationRequest,
    service: ChatService = Depends(get_service),
) -> APIResponse[ConversationItem]:
    """运维页面嵌入面板的入口：每个目标一个会话，找到复用、没有就建。"""
    try:
        row = service.find_or_create_ops_conversation(payload.target_type, payload.target_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(
        data=ConversationItem(
            id=row.id,
            title=row.title,
            agent_id=row.agent_id,
            project_id=row.project_id,
            type_ids=row.type_ids,
            model_config_id=row.model_config_id,
            message_count=service.count_messages(row.id),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
    )


@router.post("/actions/{action_id}/reject", response_model=APIResponse[OpsActionItem])
async def reject_action(
    action_id: int,
    service: ChatService = Depends(get_service),
) -> APIResponse[OpsActionItem]:
    # BusinessError（已处理/已超时）交给全局处理器：HTTP 200 + 响应体非零 code。
    try:
        item = service.reject_action(action_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=item)


@router.post("/actions/{action_id}/confirm")
async def confirm_action(
    action_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    service: ChatService = Depends(get_service),
) -> StreamingResponse:
    """确认一条待执行命令：认领（CAS）→ 执行 → 基于结果续答，全程 SSE。

    与 ``/completions`` 同样的「恒 200 + error 事件」约定。执行发生在本请求
    自己的 worker 上——这是两阶段落库的关键性质，不依赖任何跨进程状态。
    """

    async def stream() -> AsyncIterator[str]:
        # ops_write 闸门必须在 CAS 之前：无权限用户不能把 pending 认领成
        # approved。拒绝（reject）不设限——拒绝是安全方向。
        if not user.can_ops_write:
            yield _sse("error", {"code": 403, "message": "没有运维写权限，请联系管理员"})
            return
        try:
            async for event, data in service.confirm_action_stream(
                action_id=action_id,
                is_disconnected=request.is_disconnected,
            ):
                yield _sse(event, data)
        except BusinessError as exc:
            yield _sse("error", {"code": exc.code, "message": exc.msg})
        except LookupError as exc:
            yield _sse("error", {"code": 404, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the stream must not just stop
            logger.exception("ops action confirm failed")
            yield _sse("error", {"code": -1, "message": f"命令执行失败：{exc}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )


@router.put("/messages/{message_id}", response_model=APIResponse[MessageItem])
async def update_message(
    message_id: int,
    payload: MessageUpdate,
    service: ChatService = Depends(get_service),
) -> APIResponse[MessageItem]:
    """改写一条助手消息的正文（用户手动改图的落库通道）。

    改的是历史本身：下一轮提问回放给模型的就是改后的版本。
    """
    try:
        row = service.update_message_content(message_id, payload.content)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=ChatService.to_message(row))


# ---- Streaming completion ---------------------------------------------------

def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx buffers proxied responses by default, which holds the whole
    # stream until completion and defeats the point of streaming.
    "X-Accel-Buffering": "no",
}


@router.post("/completions")
async def completions(
    payload: CompletionRequest,
    request: Request,
    service: ChatService = Depends(get_service),
) -> StreamingResponse:
    """Answer a question, streaming tokens over SSE.

    Documented exception to the ``APIResponse`` envelope: the response is a
    token stream, so there is no single JSON body to wrap. Errors are delivered
    as an ``error`` event instead of an HTTP status, because by the time one
    occurs the 200 and its headers have usually already been sent.
    """
    question = payload.message.strip()

    async def stream() -> AsyncIterator[str]:
        try:
            agent = service.resolve_agent(payload.agent_id)

            if payload.conversation_id is None:
                conversation = service.create_conversation(
                    title=derive_title(question),
                    agent_id=agent.id,
                    project_id=payload.project_id,
                    type_ids=payload.type_ids,
                    model_config_id=payload.model_config_id,
                )
            else:
                conversation = service.get_conversation(payload.conversation_id)
                # An omitted scope on a follow-up means "keep using what this
                # thread was already grounded in", not "search everything".
                # 模型同理：没点名就沿用会话记住的那份。
                if payload.project_id is not None or payload.model_config_id is not None:
                    conversation = service.update_conversation(
                        conversation.id,
                        project_id=payload.project_id,
                        type_ids=payload.type_ids,
                        model_config_id=payload.model_config_id,
                    )

            # 模型在会话落库之后才确定：这次点名了就用点名的，否则沿用会话
            # 记住的那份，都没有才退回「设置」里的默认配置。
            config = service.require_chat_config(conversation.model_config_id)

            project_id = conversation.project_id
            type_ids = conversation.type_ids

            # Tell the client which conversation this belongs to before any
            # tokens arrive, so a first message can update the URL / sidebar
            # even if the answer later fails.
            yield _sse(
                "meta",
                {
                    "conversation_id": conversation.id,
                    "agent_id": agent.id,
                    "project_id": project_id,
                    "type_ids": type_ids,
                    "model_config_id": conversation.model_config_id,
                },
            )

            service.add_message(conversation.id, ROLE_USER, question)

            async for event, data in service.stream_answer(
                conversation_id=conversation.id,
                agent=agent,
                question=question,
                config=config,
                project_id=project_id,
                type_ids=type_ids,
                # Lets generation stop when the user hits stop, instead of
                # running to completion and saving an answer they never saw.
                is_disconnected=request.is_disconnected,
            ):
                yield _sse(event, data)

        except BusinessError as exc:
            yield _sse("error", {"code": exc.code, "message": exc.msg})
        except LookupError as exc:
            yield _sse("error", {"code": 404, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the stream must not just stop
            logger.exception("chat completion failed")
            yield _sse("error", {"code": -1, "message": f"回答生成失败：{exc}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )


@router.post("/conversations/{conversation_id}/retry")
async def retry_last_answer(
    conversation_id: int,
    request: Request,
    service: ChatService = Depends(get_service),
) -> StreamingResponse:
    """Re-answer the last question of a conversation, streaming over SSE.

    Failure recovery for the streaming endpoint above: when generation
    failed, the user message was already persisted but no answer exists,
    and a plain resend would duplicate the question in history. Here the
    service drops that dangling user message first, then the normal flow
    re-records it. Same event shapes as ``/completions``.
    """

    async def stream() -> AsyncIterator[str]:
        try:
            conversation = service.get_conversation(conversation_id)
            # 先删后问：拿不到悬空提问（比如上次其实成功了）直接报错事件。
            question = service.pop_dangling_user_message(conversation.id)

            agent = service.resolve_agent(conversation.agent_id)
            config = service.require_chat_config(conversation.model_config_id)

            yield _sse(
                "meta",
                {
                    "conversation_id": conversation.id,
                    "agent_id": agent.id,
                    "project_id": conversation.project_id,
                    "type_ids": conversation.type_ids,
                    "model_config_id": conversation.model_config_id,
                },
            )

            service.add_message(conversation.id, ROLE_USER, question)

            async for event, data in service.stream_answer(
                conversation_id=conversation.id,
                agent=agent,
                question=question,
                config=config,
                project_id=conversation.project_id,
                type_ids=conversation.type_ids,
                is_disconnected=request.is_disconnected,
            ):
                yield _sse(event, data)

        except BusinessError as exc:
            yield _sse("error", {"code": exc.code, "message": exc.msg})
        except LookupError as exc:
            yield _sse("error", {"code": 404, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the stream must not just stop
            logger.exception("chat retry failed")
            yield _sse("error", {"code": -1, "message": f"重试失败：{exc}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )
