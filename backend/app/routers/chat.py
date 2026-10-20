"""Chat endpoints: conversations, history and the streaming answer."""
from __future__ import annotations

import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.errors import BusinessError
from app.models.entities import ChatConversation
from app.models.schemas import (
    APIResponse,
    ChatAttachmentItem,
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
from app.services.chat.attachments import (
    AttachmentError,
    extract_file_text,
    store_image,
)
from app.services.uploads import (
    content_length_exceeds,
    oversize_message,
    read_upload_limited,
)
from app.sse import SSE_HEADERS, sse_event, with_heartbeat

logger = logging.getLogger("yangvis.chat")

settings = get_settings()

router = APIRouter(prefix="/chat", tags=["chat"])

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _display_filename(raw: str | None) -> str:
    """只保留文件名本身：展示用，绝不参与任何路径拼接。"""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (name or "attachment")[:255]


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> ChatService:
    """Bind the service to the caller so handlers never see a bare session."""
    return ChatService(db, user.user_id)


# ---- Conversations ----------------------------------------------------------

@router.get("/conversations", response_model=APIResponse[ListResponse[ConversationItem]])
def list_conversations(
    service: ChatService = Depends(get_service),
) -> APIResponse[ListResponse[ConversationItem]]:
    items = service.list_conversations()
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/conversations", response_model=APIResponse[ConversationItem])
def create_conversation(
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
def update_conversation(
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
def delete_conversation(
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
def list_messages(
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
def list_actions(
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


def _conversation_item(
    row: ChatConversation, message_count: int = 0
) -> ConversationItem:
    return ConversationItem(
        id=row.id,
        title=row.title,
        agent_id=row.agent_id,
        project_id=row.project_id,
        type_ids=row.type_ids,
        model_config_id=row.model_config_id,
        message_count=message_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/ops-conversations",
    response_model=APIResponse[ListResponse[ConversationItem]],
)
def list_ops_conversations(
    target_type: str,
    target_id: int,
    service: ChatService = Depends(get_service),
) -> APIResponse[ListResponse[ConversationItem]]:
    """某个运维目标的全部历史问答会话（最近活跃的在前）：面板的历史下拉用。"""
    try:
        rows = service.list_ops_conversations(target_type, target_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    items = [_conversation_item(row, count) for row, count in rows]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/ops-conversations", response_model=APIResponse[ConversationItem])
def create_ops_conversation(
    payload: OpsConversationRequest,
    service: ChatService = Depends(get_service),
) -> APIResponse[ConversationItem]:
    """运维嵌入面板开新会话的入口：每个新窗口一个会话，旧会话走列表翻回去。"""
    try:
        row = service.create_ops_conversation(
            payload.target_type, payload.target_id, payload.title
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=_conversation_item(row))


@router.post("/actions/{action_id}/reject", response_model=APIResponse[OpsActionItem])
def reject_action(
    action_id: int,
    user: CurrentUser = Depends(require_user),
    service: ChatService = Depends(get_service),
) -> APIResponse[OpsActionItem]:
    # BusinessError（已处理/已超时）交给全局处理器：HTTP 200 + 响应体非零 code。
    try:
        item = service.reject_action(action_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # 用户拒绝一条 AI 提议的写命令是审计事件，留一条 info。
    logger.info("user %s rejected ops action %s", user.user_id, action_id)
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
            yield sse_event("error", {"code": 403, "message": "没有运维写权限，请联系管理员"})
            return
        try:
            async for event, data in service.confirm_action_stream(
                action_id=action_id,
                is_disconnected=request.is_disconnected,
            ):
                yield sse_event(event, data)
        except BusinessError as exc:
            yield sse_event("error", {"code": exc.code, "message": exc.msg})
        except LookupError as exc:
            yield sse_event("error", {"code": 404, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the stream must not just stop
            logger.exception("ops action %s confirm failed", action_id)
            yield sse_event("error", {"code": -1, "message": f"命令执行失败：{exc}"})

    return StreamingResponse(
        with_heartbeat(stream()),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )


@router.put("/messages/{message_id}", response_model=APIResponse[MessageItem])
def update_message(
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
    # 手动编辑改的是历史本身，下一轮回放给模型的就是这个版本，留痕。
    logger.info("message %s was manually edited", message_id)
    return APIResponse(data=ChatService.to_message(row))


# ---- Attachments -------------------------------------------------------------

@router.post("/attachments", response_model=APIResponse[ChatAttachmentItem])
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    service: ChatService = Depends(get_service),
) -> APIResponse[ChatAttachmentItem]:
    """上传一条对话附件：图片保留原文件，文档当场抽成文本。

    上传与发送分两次请求：前端先拿到附件 id 做预览，发送时只带 id 列表。
    没有挂到消息上的附件可以直接删除；发送成功的会随消息留下。
    """
    limit = settings.CHAT_MAX_ATTACHMENT_BYTES
    if content_length_exceeds(request.headers.get("content-length"), limit):
        return APIResponse(code=-1, message=oversize_message(limit))
    raw = await read_upload_limited(file, limit)
    if raw is None:
        return APIResponse(code=-1, message=oversize_message(limit))
    if not raw:
        return APIResponse(code=-1, message="文件内容为空")

    filename = _display_filename(file.filename)
    content_type = (file.content_type or "").lower()
    is_image = content_type.startswith("image/") or filename.lower().endswith(
        _IMAGE_EXTENSIONS
    )
    try:
        if is_image:
            stored = store_image(service.owner_id, filename, raw)
            row = service.create_attachment(
                kind="image",
                filename=filename,
                mime=stored.mime,
                size=stored.size,
                rel_path=stored.rel_path,
            )
        else:
            text = extract_file_text(filename, raw)
            row = service.create_attachment(
                kind="file",
                filename=filename,
                mime=content_type or None,
                size=len(raw),
                text=text,
            )
    except AttachmentError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=ChatService.to_attachment(row))


@router.delete("/attachments/{attachment_id}", response_model=APIResponse[None])
def delete_attachment(
    attachment_id: int,
    service: ChatService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_attachment(attachment_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")


# ---- Streaming completion ---------------------------------------------------

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
        conversation = None  # 异常可能发生在会话建出来之前，错误日志里要容忍它
        try:
            # 附件校验先行：批次不对（已发过、不属于本用户）时连会话都不该建，
            # 否则侧栏会留下一个没有内容的空会话。
            attachments = service.describe_attachments(payload.attachment_ids)
            agent = service.resolve_agent(payload.agent_id)

            if payload.conversation_id is None:
                # 只传附件不写字时，标题退回用第一个附件的文件名。
                title = derive_title(question) or derive_title(
                    f"附件：{attachments[0].filename}"
                )
                conversation = service.create_conversation(
                    title=title,
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
            # 图片交给纯文本模型会被上游整条拒绝，拦在这里给出能看懂的原因。
            service.assert_vision_ready(attachments, config)

            project_id = conversation.project_id
            type_ids = conversation.type_ids

            # Tell the client which conversation this belongs to before any
            # tokens arrive, so a first message can update the URL / sidebar
            # even if the answer later fails.
            yield sse_event(
                "meta",
                {
                    "conversation_id": conversation.id,
                    "agent_id": agent.id,
                    "project_id": project_id,
                    "type_ids": type_ids,
                    "model_config_id": conversation.model_config_id,
                },
            )

            service.add_message(
                conversation.id,
                ROLE_USER,
                question,
                attachment_ids=payload.attachment_ids,
            )

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
                yield sse_event(event, data)

        except BusinessError as exc:
            yield sse_event("error", {"code": exc.code, "message": exc.msg})
        except LookupError as exc:
            yield sse_event("error", {"code": 404, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the stream must not just stop
            logger.exception(
                "chat completion failed for conversation %s",
                getattr(conversation, "id", None),
            )
            yield sse_event("error", {"code": -1, "message": f"回答生成失败：{exc}"})

    return StreamingResponse(
        with_heartbeat(stream()),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
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
            # 先解析智能体与模型，再动悬空的提问：模型不可用时不该把用户
            # 已经写下的问题从历史里删掉。
            agent = service.resolve_agent(conversation.agent_id)
            config = service.require_chat_config(conversation.model_config_id)

            # 拿不到悬空提问（比如上次其实成功了）直接报错事件。
            question, attachment_ids = service.pop_dangling_user_message(
                conversation.id
            )
            attachments = service.describe_attachments(attachment_ids)
            try:
                service.assert_vision_ready(attachments, config)
            except BusinessError:
                # 两次尝试之间模型被改成不支持图片：把提问带着附件原样放回，
                # 别让历史里凭空少一条。
                service.add_message(
                    conversation.id,
                    ROLE_USER,
                    question,
                    attachment_ids=attachment_ids,
                )
                raise

            yield sse_event(
                "meta",
                {
                    "conversation_id": conversation.id,
                    "agent_id": agent.id,
                    "project_id": conversation.project_id,
                    "type_ids": conversation.type_ids,
                    "model_config_id": conversation.model_config_id,
                },
            )

            service.add_message(
                conversation.id,
                ROLE_USER,
                question,
                attachment_ids=attachment_ids,
            )

            async for event, data in service.stream_answer(
                conversation_id=conversation.id,
                agent=agent,
                question=question,
                config=config,
                project_id=conversation.project_id,
                type_ids=conversation.type_ids,
                is_disconnected=request.is_disconnected,
            ):
                yield sse_event(event, data)

        except BusinessError as exc:
            yield sse_event("error", {"code": exc.code, "message": exc.msg})
        except LookupError as exc:
            yield sse_event("error", {"code": 404, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the stream must not just stop
            logger.exception("chat retry failed for conversation %s", conversation_id)
            yield sse_event("error", {"code": -1, "message": f"重试失败：{exc}"})

    return StreamingResponse(
        with_heartbeat(stream()),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )
