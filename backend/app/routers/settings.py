"""Settings endpoints: per-user model configuration."""
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
    KnowledgeStatus,
    ListResponse,
    ModelConfigCreate,
    ModelConfigItem,
    ModelConfigUpdate,
    ModelPurpose,
    PurposeStatus,
    ReadinessStatus,
    TestResult,
)
from app.services.knowledge import KnowledgeService
from app.services.model_config import ModelConfigService
from app.services.providers import probe_embedding_dimension, test_chat_model

logger = logging.getLogger("yangvis.settings")

router = APIRouter(prefix="/settings", tags=["settings"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> ModelConfigService:
    """Bind the service to the caller so handlers never see a bare session."""
    return ModelConfigService(db, user.user_id)


@router.get("/models", response_model=APIResponse[ListResponse[ModelConfigItem]])
async def list_models(
    purpose: Optional[ModelPurpose] = Query(default=None),
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[ListResponse[ModelConfigItem]]:
    items = [service.to_item(config) for config in service.list(purpose)]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/models", response_model=APIResponse[ModelConfigItem])
async def create_model(
    payload: ModelConfigCreate,
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[ModelConfigItem]:
    config = service.create(payload)
    return APIResponse(data=service.to_item(config))


@router.put("/models/{config_id}", response_model=APIResponse[ModelConfigItem])
async def update_model(
    config_id: int,
    payload: ModelConfigUpdate,
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[ModelConfigItem]:
    try:
        config = service.update(config_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=service.to_item(config))


@router.delete("/models/{config_id}", response_model=APIResponse[None])
async def delete_model(
    config_id: int,
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete(config_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")


@router.post("/models/{config_id}/default", response_model=APIResponse[ModelConfigItem])
async def set_default_model(
    config_id: int,
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[ModelConfigItem]:
    try:
        config = service.set_default(config_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=service.to_item(config))


@router.post("/models/{config_id}/test", response_model=APIResponse[TestResult])
async def test_model(
    config_id: int,
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[TestResult]:
    """Verify a configuration against the live provider.

    For embeddings this doubles as the dimension probe: the measured size is
    persisted because the Qdrant collection is created from it and cannot be
    resized later.
    """
    try:
        config = service.get(config_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    vector_size: Optional[int] = None
    if config.purpose == ModelPurpose.EMBEDDING.value:
        ok, message, vector_size = await probe_embedding_dimension(config)
    else:
        ok, message = await test_chat_model(config)

    service.record_test_result(config, ok=ok, message=message, vector_size=vector_size)
    return APIResponse(data=TestResult(success=ok, message=message, vector_size=vector_size))


@router.get("/models/status", response_model=APIResponse[ReadinessStatus])
async def readiness_status(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[ReadinessStatus]:
    """Report whether chat, embedding and the knowledge base are usable.

    The SPA calls this before rendering the chat and knowledge pages so it can
    guide the user to configuration instead of failing mid-action.
    """
    service = ModelConfigService(db, user.user_id)
    result = ReadinessStatus()
    for purpose, field in ((ModelPurpose.CHAT, "chat"), (ModelPurpose.EMBEDDING, "embedding")):
        config = service.get_default(purpose)
        if config is not None:
            setattr(
                result,
                field,
                PurposeStatus(
                    ready=config.last_test_ok,
                    model=config.model_name,
                    config_id=config.id,
                ),
            )

    state = KnowledgeService(db, user.user_id).index_state_view()
    result.kb = KnowledgeStatus(
        status=state.status,
        doc_count=state.doc_count,
        chunk_count=state.chunk_count,
        model_mismatch=state.model_mismatch,
    )
    return APIResponse(data=result)
