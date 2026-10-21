"""智能办公 · 在线简历编辑器接口。

文档本体是普通信封接口（CRUD）；AI 帮写是 SSE 流式接口——与对话流同一约定：
成功没有 JSON 信封，按 token/done 事件推；错误也在流内用 error 事件送达，
因为 200 与响应头通常已经发出。
"""
from __future__ import annotations

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
    CurrentUser,
    ListResponse,
    ResumeDocAiRequest,
    ResumeDocCreate,
    ResumeDocDetail,
    ResumeDocItem,
    ResumeDocUpdate,
)
from app.services.agent_run import model_error_text
from app.services.resume_builder import ResumeBuilderService
from app.sse import SSE_HEADERS, sse_event, with_heartbeat

logger = logging.getLogger("yangvis.resume_builder")

router = APIRouter(prefix="/office/resume-docs", tags=["office"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> ResumeBuilderService:
    return ResumeBuilderService(db, user.user_id)


# ---- 文档 CRUD ---------------------------------------------------------------


@router.get("", response_model=APIResponse[ListResponse[ResumeDocItem]])
def list_docs(
    service: ResumeBuilderService = Depends(get_service),
) -> APIResponse[ListResponse[ResumeDocItem]]:
    items = [service.to_item(row) for row in service.list_docs()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("", response_model=APIResponse[ResumeDocDetail])
def create_doc(
    payload: ResumeDocCreate,
    service: ResumeBuilderService = Depends(get_service),
) -> APIResponse[ResumeDocDetail]:
    row = service.create_doc(payload.title)
    return APIResponse(data=service.to_detail(row))


@router.get("/{doc_id}", response_model=APIResponse[ResumeDocDetail])
def get_doc(
    doc_id: int,
    service: ResumeBuilderService = Depends(get_service),
) -> APIResponse[ResumeDocDetail]:
    try:
        row = service.get_doc(doc_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(row))


@router.put("/{doc_id}", response_model=APIResponse[ResumeDocDetail])
def update_doc(
    doc_id: int,
    payload: ResumeDocUpdate,
    service: ResumeBuilderService = Depends(get_service),
) -> APIResponse[ResumeDocDetail]:
    try:
        row = service.update_doc(doc_id, title=payload.title, doc=payload.doc)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(row))


@router.delete("/{doc_id}", response_model=APIResponse[None])
def delete_doc(
    doc_id: int,
    service: ResumeBuilderService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_doc(doc_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")


# ---- AI 帮写（SSE）------------------------------------------------------------


@router.post("/ai/assist")
async def ai_assist(
    payload: ResumeDocAiRequest,
    request: Request,
    service: ResumeBuilderService = Depends(get_service),
) -> StreamingResponse:
    """流式返回帮写/润色的 HTML 片段或体检的 markdown。

    ``/{doc_id}`` 的路径参数声明为 int，``/ai/assist`` 天然不会匹配到它。
    """

    async def stream() -> AsyncIterator[str]:
        try:
            async for token in service.ai_stream(
                payload, is_disconnected=request.is_disconnected
            ):
                yield sse_event("token", token)
            yield sse_event("done", {})
        except BusinessError as exc:
            # 对话模型未就绪：前端按 code 走「去配置模型」引导（同对话流）。
            yield sse_event("error", {"code": exc.code, "message": exc.msg})
        except LookupError as exc:
            yield sse_event("error", {"code": 404, "message": str(exc)})
        except ValueError as exc:
            # 条目已删、没有可润色内容、空简历体检：文案本身就是操作指引。
            yield sse_event("error", {"code": -1, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 流不能因为异常静默中断
            logger.exception(
                "resume ai assist failed: owner=%s doc=%s module=%s action=%s",
                service.owner_id,
                payload.doc_id,
                payload.module,
                payload.action,
            )
            yield sse_event("error", {"code": -1, "message": model_error_text(exc)})

    return StreamingResponse(
        with_heartbeat(stream()),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
