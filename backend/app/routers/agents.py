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
    AgentVersionDetail,
    AgentVersionItem,
    APIResponse,
    CurrentUser,
    ListResponse,
)
from app.services import agent_versions
from app.services.agents import AgentService

logger = logging.getLogger("yangvis.agents")

router = APIRouter(prefix="/agents", tags=["agents"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> AgentService:
    return AgentService(db, user.user_id)


@router.get("", response_model=APIResponse[ListResponse[AgentItem]])
def list_agents(
    enabled_only: bool = Query(default=False),
    service: AgentService = Depends(get_service),
) -> APIResponse[ListResponse[AgentItem]]:
    """内置智能体加上调用方自己的智能体，按展示顺序排列。"""
    items = [AgentService.to_item(agent) for agent in service.list(enabled_only=enabled_only)]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("", response_model=APIResponse[AgentItem])
def create_agent(
    payload: AgentCreate,
    service: AgentService = Depends(get_service),
) -> APIResponse[AgentItem]:
    try:
        agent = service.create(payload)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=AgentService.to_item(agent))


@router.put("/{agent_id}", response_model=APIResponse[AgentItem])
def update_agent(
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
def delete_agent(
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
def duplicate_agent(
    agent_id: int,
    service: AgentService = Depends(get_service),
) -> APIResponse[AgentItem]:
    """把一个智能体复制到调用方名下；这是定制内置智能体的方式。"""
    try:
        agent = service.duplicate(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=AgentService.to_item(agent))


@router.get("/{agent_id}/versions", response_model=APIResponse[ListResponse[AgentVersionItem]])
def list_agent_versions(
    agent_id: int,
    service: AgentService = Depends(get_service),
    db: Session = Depends(get_db),
) -> APIResponse[ListResponse[AgentVersionItem]]:
    """该智能体的版本史，新的在前；内置智能体人人可查。"""
    try:
        service.get(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    rows = agent_versions.list_versions(db, agent_id)
    items = [
        AgentVersionItem(version=row.version, name=row.snapshot.get("name", ""), created_at=row.created_at)
        for row in rows
    ]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.get(
    "/{agent_id}/versions/{version}",
    response_model=APIResponse[AgentVersionDetail],
)
def get_agent_version(
    agent_id: int,
    version: int,
    service: AgentService = Depends(get_service),
    db: Session = Depends(get_db),
) -> APIResponse[AgentVersionDetail]:
    """取某个版本的完整人设快照：历史运行步骤上的版本号就对到这里。"""
    try:
        service.get(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    row = agent_versions.get_version(db, agent_id, version)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="该版本不存在")
    return APIResponse(
        data=AgentVersionDetail(version=row.version, created_at=row.created_at, snapshot=row.snapshot)
    )
