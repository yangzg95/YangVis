"""长期记忆的管理接口：查看、手动增删改、一键清空。

记忆主要由后台任务自动提取，这里的接口是给用户的管理入口——能看到
AI 记住了什么、能改能删，是记忆功能值得被信任的前提。
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    MemoryCreate,
    MemoryItem,
    MemoryUpdate,
)
from app.services.memory import MemoryService

logger = logging.getLogger("yangvis.memory")

router = APIRouter(prefix="/memory", tags=["memory"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> MemoryService:
    return MemoryService(db, user.user_id)


@router.get("", response_model=APIResponse[ListResponse[MemoryItem]])
async def list_memories(
    service: MemoryService = Depends(get_service),
) -> APIResponse[ListResponse[MemoryItem]]:
    rows = service.list()
    items = [MemoryItem.model_validate(row) for row in rows]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("", response_model=APIResponse[MemoryItem])
async def create_memory(
    payload: MemoryCreate,
    service: MemoryService = Depends(get_service),
) -> APIResponse[MemoryItem]:
    row = service.create(payload.content)
    logger.info("owner %s manually added memory %s", service.owner_id, row.id)
    return APIResponse(data=MemoryItem.model_validate(row))


@router.put("/{memory_id}", response_model=APIResponse[MemoryItem])
async def update_memory(
    memory_id: int,
    payload: MemoryUpdate,
    service: MemoryService = Depends(get_service),
) -> APIResponse[MemoryItem]:
    try:
        row = service.update(memory_id, payload.content)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=MemoryItem.model_validate(row))


@router.delete("/{memory_id}", response_model=APIResponse[None])
async def delete_memory(
    memory_id: int,
    service: MemoryService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete(memory_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")


@router.delete("", response_model=APIResponse[None])
async def clear_memories(
    service: MemoryService = Depends(get_service),
) -> APIResponse[None]:
    count = service.clear()
    logger.info("owner %s cleared %d memories", service.owner_id, count)
    return APIResponse(data=None, message="cleared")
