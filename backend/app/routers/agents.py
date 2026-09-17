"""智能体接口：可供选择的助手角色。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models.schemas import (
    AgentCreate,
    AgentItem,
    AgentUpdate,
    APIResponse,
    CurrentUser,
    ListResponse,
)
from app.services.agents import AgentService

logger = logging.getLogger("yangvis.agents")

router = APIRouter(prefix="/agents", tags=["agents"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> AgentService:
    return AgentService(db, user.user_id)


@router.get("", response_model=APIResponse[ListResponse[AgentItem]])
async def list_agents(
    enabled_only: bool = Query(default=False),
    service: AgentService = Depends(get_service),
) -> APIResponse[ListResponse[AgentItem]]:
    """内置智能体加上调用方自己的智能体，按展示顺序排列。"""
    items = [AgentService.to_item(agent) for agent in service.list(enabled_only=enabled_only)]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("", response_model=APIResponse[AgentItem])
async def create_agent(
    payload: AgentCreate,
    service: AgentService = Depends(get_service),
) -> APIResponse[AgentItem]:
    try:
        agent = service.create(payload)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=AgentService.to_item(agent))


@router.put("/{agent_id}", response_model=APIResponse[AgentItem])
async def update_agent(
    agent_id: int,
    payload: AgentUpdate,
    service: AgentService = Depends(get_service),
) -> APIResponse[AgentItem]:
    try:
        agent = service.update(agent_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=AgentService.to_item(agent))


@router.delete("/{agent_id}", response_model=APIResponse[None])
async def delete_agent(
    agent_id: int,
    service: AgentService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=None, message="deleted")


@router.post("/{agent_id}/duplicate", response_model=APIResponse[AgentItem])
async def duplicate_agent(
    agent_id: int,
    service: AgentService = Depends(get_service),
) -> APIResponse[AgentItem]:
    """把一个智能体复制到调用方名下；这是定制内置智能体的方式。"""
    try:
        agent = service.duplicate(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=AgentService.to_item(agent))
