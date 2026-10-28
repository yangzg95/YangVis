"""智能办公 · 求职笔记相关接口。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    NoteCreate,
    NoteDetail,
    NoteItem,
    NoteUpdate,
)
from app.services.note import NoteService

logger = logging.getLogger("yangvis.note")

router = APIRouter(prefix="/office", tags=["office"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> NoteService:
    """把 service 绑定到调用者身上，handler 就碰不到裸 session 了。"""
    return NoteService(db, user.user_id)


@router.get("/notes", response_model=APIResponse[ListResponse[NoteItem]])
def list_notes(
    keyword: Optional[str] = Query(default=None, max_length=200),
    service: NoteService = Depends(get_service),
) -> APIResponse[ListResponse[NoteItem]]:
    items = [NoteService.to_item(record) for record in service.list_records(keyword)]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/notes", response_model=APIResponse[NoteDetail])
def create_note(
    payload: NoteCreate,
    service: NoteService = Depends(get_service),
) -> APIResponse[NoteDetail]:
    record = service.create_record(payload)
    return APIResponse(data=service.to_detail(record))


@router.get("/notes/{note_id}", response_model=APIResponse[NoteDetail])
def get_note(
    note_id: int,
    service: NoteService = Depends(get_service),
) -> APIResponse[NoteDetail]:
    try:
        record = service.get_record(note_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(record))


@router.put("/notes/{note_id}", response_model=APIResponse[NoteDetail])
def update_note(
    note_id: int,
    payload: NoteUpdate,
    service: NoteService = Depends(get_service),
) -> APIResponse[NoteDetail]:
    try:
        record = service.update_record(note_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_detail(record))


@router.delete("/notes/{note_id}", response_model=APIResponse[None])
def delete_note(
    note_id: int,
    service: NoteService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_record(note_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")
