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
from app.services.media import ark
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
def list_models(
    purpose: Optional[ModelPurpose] = Query(default=None),
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[ListResponse[ModelConfigItem]]:
    items = [service.to_item(config) for config in service.list(purpose)]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/models", response_model=APIResponse[ModelConfigItem])
def create_model(
    payload: ModelConfigCreate,
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[ModelConfigItem]:
    config = service.create(payload)
    return APIResponse(data=service.to_item(config))


@router.put("/models/{config_id}", response_model=APIResponse[ModelConfigItem])
def update_model(
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
def delete_model(
    config_id: int,
    service: ModelConfigService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete(config_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=None, message="deleted")


@router.post("/models/{config_id}/default", response_model=APIResponse[ModelConfigItem])
def set_default_model(
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

    图片/视频这两类走方舟的媒体端点，不是 OpenAI 兼容的对话接口，所以探测的是
    一个**免费的只读请求**（任务列表）。它验得出 base_url 与 api_key，验不出
    Model ID——媒体调用一次就扣一次抵扣，没有「不花钱又能验证型号」的探测办法，
    探测通过的提示语里已经把这句说明白了。
    """
    try:
        config = service.get(config_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    vector_size: Optional[int] = None
    if config.purpose == ModelPurpose.EMBEDDING.value:
        ok, message, vector_size = await probe_embedding_dimension(config)
    elif config.purpose in (ModelPurpose.IMAGE.value, ModelPurpose.VIDEO.value):
        ok, message = await ark.probe(config)
    else:
        ok, message = await test_chat_model(config)

    service.record_test_result(config, ok=ok, message=message, vector_size=vector_size)
    return APIResponse(data=TestResult(success=ok, message=message, vector_size=vector_size))


@router.get("/models/status", response_model=APIResponse[ReadinessStatus])
def readiness_status(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> APIResponse[ReadinessStatus]:
    """Report whether chat, embedding, the two media purposes and the KB are usable.

    The SPA calls this before rendering the chat, knowledge and short-video pages
    so it can guide the user to configuration instead of failing mid-action.

    短视频把 image 与 video 两个位子分开报，是因为「只配了出图、没配出片」完全可
    能，而那正好对应工作台上「出首帧」和「渲染视频」两个不同的按钮。
    """
    service = ModelConfigService(db, user.user_id)
    result = ReadinessStatus()
    for purpose, field in (
        (ModelPurpose.CHAT, "chat"),
        (ModelPurpose.EMBEDDING, "embedding"),
        (ModelPurpose.IMAGE, "image"),
        (ModelPurpose.VIDEO, "video"),
    ):
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
