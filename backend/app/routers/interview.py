"""智能办公 · 面试记录相关接口。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    InterviewCreate,
    InterviewDetail,
    InterviewItem,
    InterviewQuestionIn,
    InterviewUpdate,
    ListResponse,
)
from app.services.interview import InterviewService

logger = logging.getLogger("yangvis.interview")

router = APIRouter(prefix="/office", tags=["office"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> InterviewService:
    """把 service 绑定到调用者身上，handler 就碰不到裸 session 了。"""
    return InterviewService(db, user.user_id)


# ---- 面试场次 ----------------------------------------------------------------

@router.get("/interviews", response_model=APIResponse[ListResponse[InterviewItem]])
async def list_interviews(
    service: InterviewService = Depends(get_service),
) -> APIResponse[ListResponse[InterviewItem]]:
    items = [InterviewService.to_item(record) for record in service.list_records()]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/interviews", response_model=APIResponse[InterviewDetail])
async def create_interview(
    payload: InterviewCreate,
    service: InterviewService = Depends(get_service),
) -> APIResponse[InterviewDetail]:
    record = service.create_record(payload)
    return APIResponse(data=service.to_detail(record))


@router.get("/interviews/{record_id}", response_model=APIResponse[InterviewDetail])
async def get_interview(
    record_id: int,
    service: InterviewService = Depends(get_service),
) -> APIResponse[InterviewDetail]:
    try:
        record = service.get_record(record_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(record))


@router.put("/interviews/{record_id}", response_model=APIResponse[InterviewDetail])
async def update_interview(
    record_id: int,
    payload: InterviewUpdate,
    service: InterviewService = Depends(get_service),
) -> APIResponse[InterviewDetail]:
    """改面试场次的元数据。questions 走题目级端点，不在这里整体替换——
    那样会和后台生成任务回写的 ref_* 互相覆盖。"""
    try:
        record = service.update_record(record_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(record))


@router.delete("/interviews/{record_id}", response_model=APIResponse[None])
async def delete_interview(
    record_id: int,
    service: InterviewService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_record(record_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=None, message="deleted")


# ---- 题目 ---------------------------------------------------------------------

@router.post("/interviews/{record_id}/questions", response_model=APIResponse[InterviewDetail])
async def add_question(
    record_id: int,
    payload: InterviewQuestionIn,
    service: InterviewService = Depends(get_service),
) -> APIResponse[InterviewDetail]:
    """题目级写操作统一返回整条详情，前端直接整换抽屉里的数据。"""
    try:
        record = service.add_question(record_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(record))


@router.put(
    "/interviews/{record_id}/questions/{qid}", response_model=APIResponse[InterviewDetail]
)
async def update_question(
    record_id: int,
    qid: str,
    payload: InterviewQuestionIn,
    service: InterviewService = Depends(get_service),
) -> APIResponse[InterviewDetail]:
    try:
        record = service.update_question(record_id, qid, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(record))


@router.delete(
    "/interviews/{record_id}/questions/{qid}", response_model=APIResponse[InterviewDetail]
)
async def delete_question(
    record_id: int,
    qid: str,
    service: InterviewService = Depends(get_service),
) -> APIResponse[InterviewDetail]:
    try:
        record = service.delete_question(record_id, qid)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_detail(record))


@router.post(
    "/interviews/{record_id}/questions/{qid}/answer",
    response_model=APIResponse[InterviewDetail],
)
async def generate_answer(
    record_id: int,
    qid: str,
    background: BackgroundTasks,
    service: InterviewService = Depends(get_service),
) -> APIResponse[InterviewDetail]:
    """触发 AI 参考答案生成。先做配置检查，让用户在排队之前就知道还差什么。"""
    service.require_chat_config()
    try:
        record = service.start_answer(record_id, qid)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    background.add_task(_answer_task, service.owner_id, record_id, qid)
    return APIResponse(data=service.to_detail(record), message="已开始生成")


async def _answer_task(owner_id: int, record_id: int, qid: str) -> None:
    # 函数体内延迟导入：测试会 monkeypatch app.database.SessionLocal，
    # 模块顶层 import 进来的名字是替换不掉的（与 resume 路由同一约定）。
    from app.database import SessionLocal

    with SessionLocal() as db:
        service = InterviewService(db, owner_id)
        try:
            await service.run_answer(record_id, qid)
        except Exception:  # noqa: BLE001 - run_answer 内部已带堆栈记录
            pass
