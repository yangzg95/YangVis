"""Knowledge-base endpoints: types, documents, indexing and retrieval."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.errors import BusinessError
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    DocBatchPayload,
    DocBatchResult,
    IndexState,
    KbProject,
    KbProjectCreate,
    KbProjectUpdate,
    KnowledgeDocument,
    KnowledgeType,
    KnowledgeTypeCreate,
    KnowledgeTypeUpdate,
    ListResponse,
    RebuildResult,
    SearchRequest,
    SearchResponse,
)
from app.services.chunking import UnsupportedFileType, extract_text
from app.services.knowledge import KnowledgeService, index_document_task

logger = logging.getLogger("yangvis.knowledge")

settings = get_settings()

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> KnowledgeService:
    """Bind the service to the caller so handlers never see a bare session."""
    return KnowledgeService(db, user.user_id)


def require_embedding_ready(
    service: KnowledgeService = Depends(get_service),
) -> KnowledgeService:
    """Gate the endpoints that cannot work without a verified embedding model.

    Applied as a dependency rather than a check inside each handler so a new
    endpoint has to opt out explicitly instead of silently forgetting.
    """
    service.require_embedding_config()
    return service


# ---- Projects ---------------------------------------------------------------

@router.get("/projects", response_model=APIResponse[ListResponse[KbProject]])
async def list_projects(
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[ListResponse[KbProject]]:
    items = service.list_projects()
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/projects", response_model=APIResponse[KbProject])
async def create_project(
    payload: KbProjectCreate,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[KbProject]:
    item = service.create_project(payload)
    return APIResponse(
        data=KbProject(
            id=item.id,
            name=item.name,
            description=item.description,
            type_count=0,
            document_count=0,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
    )


@router.put("/projects/{project_id}", response_model=APIResponse[KbProject])
async def update_project(
    project_id: int,
    payload: KbProjectUpdate,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[KbProject]:
    try:
        service.update_project(project_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    view = next((p for p in service.list_projects() if p.id == project_id), None)
    if view is None:  # pragma: no cover - it was just updated
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return APIResponse(data=view)


@router.delete("/projects/{project_id}", response_model=APIResponse[None])
async def delete_project(
    project_id: int,
    background: BackgroundTasks,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[None]:
    """Delete a project along with every type and document inside it."""
    try:
        doc_ids = service.delete_project(project_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    for doc_id in doc_ids:
        background.add_task(_purge_vectors, service.collection, service.owner_id, doc_id)
    return APIResponse(data=None, message="deleted")


# ---- Knowledge types --------------------------------------------------------

@router.get("/types", response_model=APIResponse[ListResponse[KnowledgeType]])
async def list_knowledge_types(
    project_id: int = Query(..., ge=1),
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[ListResponse[KnowledgeType]]:
    """Types in one project. Required, so nothing ever lists across projects."""
    try:
        service.get_project(project_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    items = service.list_types(project_id)
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/types", response_model=APIResponse[KnowledgeType])
async def create_knowledge_type(
    payload: KnowledgeTypeCreate,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[KnowledgeType]:
    try:
        item = service.create_type(payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(
        data=KnowledgeType(
            id=item.id,
            project_id=item.project_id,
            name=item.name,
            description=item.description,
            document_count=0,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
    )


@router.put("/types/{type_id}", response_model=APIResponse[KnowledgeType])
async def update_knowledge_type(
    type_id: int,
    payload: KnowledgeTypeUpdate,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[KnowledgeType]:
    try:
        item = service.update_type(type_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    counts = {t.id: t.document_count for t in service.list_types(item.project_id)}
    return APIResponse(
        data=KnowledgeType(
            id=item.id,
            project_id=item.project_id,
            name=item.name,
            description=item.description,
            document_count=counts.get(item.id, 0),
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
    )


@router.delete("/types/{type_id}", response_model=APIResponse[None])
async def delete_knowledge_type(
    type_id: int,
    background: BackgroundTasks,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[None]:
    try:
        doc_ids = service.delete_type(type_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    # Vectors are cleaned up out of band: the rows are already gone, so a
    # Qdrant hiccup must not turn a successful delete into an error. Any point
    # that survives is filtered out at query time because its chunk row no
    # longer exists.
    for doc_id in doc_ids:
        background.add_task(_purge_vectors, service.collection, service.owner_id, doc_id)
    return APIResponse(data=None, message="deleted")


async def _purge_vectors(collection: str, owner_id: int, doc_id: int) -> None:
    from app.services import vectorstore

    try:
        await vectorstore.delete_by_doc(collection, owner_id, doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to purge vectors for doc %s: %s", doc_id, exc)


# ---- Documents --------------------------------------------------------------

@router.get(
    "/types/{type_id}/documents",
    response_model=APIResponse[ListResponse[KnowledgeDocument]],
)
async def list_documents(
    type_id: int,
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=200),
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[ListResponse[KnowledgeDocument]]:
    """不传 page/page_size 时全量返回（兼容旧调用方）；传了才分页。

    total 始终是真实总数，好让前端分页器知道一共几页。
    """
    try:
        service.get_type(type_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    items, total = service.list_documents(type_id, page=page, page_size=page_size)
    return APIResponse(data=ListResponse(items=items, total=total))


@router.get("/documents", response_model=APIResponse[ListResponse[KnowledgeDocument]])
async def list_all_documents(
    type_id: Optional[int] = Query(default=None),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=200),
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[ListResponse[KnowledgeDocument]]:
    items, total = service.list_documents(type_id, page=page, page_size=page_size)
    return APIResponse(data=ListResponse(items=items, total=total))


@router.post("/types/{type_id}/documents", response_model=APIResponse[KnowledgeDocument])
async def upload_document(
    type_id: int,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    service: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[KnowledgeDocument]:
    """Store a document and queue it for indexing.

    Chunking and embedding happen in the background: a few hundred KB of text
    is dozens of provider round-trips, which would blow past both the SPA's
    30s axios timeout and Gunicorn's worker timeout if done inline.
    """
    try:
        service.get_type(type_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    raw = await file.read()
    if len(raw) > settings.KB_MAX_UPLOAD_BYTES:
        limit_mb = settings.KB_MAX_UPLOAD_BYTES / 1024 / 1024
        return APIResponse(code=-1, message=f"文件过大，上限 {limit_mb:.0f} MB")

    filename = name or file.filename or "untitled.txt"
    try:
        text = extract_text(file.filename or filename, raw)
    except UnsupportedFileType as exc:
        return APIResponse(code=-1, message=str(exc))

    if not text:
        return APIResponse(code=-1, message="文件内容为空或无法解析出文本")

    doc = service.create_document(
        filename=filename,
        mime=file.content_type,
        size=len(raw),
        content=text,
        type_id=type_id,
    )
    background.add_task(index_document_task, service.owner_id, doc.id)
    return APIResponse(data=KnowledgeService.to_document(doc))


@router.post("/documents/{doc_id}/reindex", response_model=APIResponse[KnowledgeDocument])
async def reindex_document(
    doc_id: int,
    background: BackgroundTasks,
    service: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[KnowledgeDocument]:
    """Re-chunk and re-embed one document from its stored text."""
    try:
        doc = service.get_document(doc_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    from app.models.schemas import DocumentStatus

    service.set_document_status(doc, DocumentStatus.PENDING)
    background.add_task(index_document_task, service.owner_id, doc.id)
    return APIResponse(data=KnowledgeService.to_document(doc))


@router.delete("/documents/{doc_id}", response_model=APIResponse[None])
async def delete_document(
    doc_id: int,
    background: BackgroundTasks,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[None]:
    try:
        service.delete_document(doc_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    background.add_task(_purge_vectors, service.collection, service.owner_id, doc_id)
    return APIResponse(data=None, message="deleted")


@router.post("/documents/batch-delete", response_model=APIResponse[DocBatchResult])
async def batch_delete_documents(
    payload: DocBatchPayload,
    background: BackgroundTasks,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[DocBatchResult]:
    """批量删除：一次请求省掉前端的 N 次往返。

    查无此文档（已被删、或不属于当前用户）的 id 跳过，不拖垮整批。
    向量清理照旧走后台任务，Qdrant 抖动不影响删除本身。
    """
    done = 0
    for doc_id in payload.ids:
        try:
            service.delete_document(doc_id)
        except LookupError:
            continue
        done += 1
        background.add_task(_purge_vectors, service.collection, service.owner_id, doc_id)
    return APIResponse(
        data=DocBatchResult(requested=len(payload.ids), done=done),
        message=f"已删除 {done} 篇文档",
    )


@router.post("/documents/batch-reindex", response_model=APIResponse[DocBatchResult])
async def batch_reindex_documents(
    payload: DocBatchPayload,
    background: BackgroundTasks,
    service: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[DocBatchResult]:
    """批量重建索引：BackgroundTasks 在同一次请求里是串行执行的，
    N 篇文档不会并发打满 embedding 供应商。
    """
    from app.models.schemas import DocumentStatus

    done = 0
    for doc_id in payload.ids:
        try:
            doc = service.get_document(doc_id)
        except LookupError:
            continue
        service.set_document_status(doc, DocumentStatus.PENDING)
        background.add_task(index_document_task, service.owner_id, doc.id)
        done += 1
    return APIResponse(
        data=DocBatchResult(requested=len(payload.ids), done=done),
        message=f"已重新排队索引 {done} 篇文档",
    )


# ---- Index ------------------------------------------------------------------

@router.get("/index/state", response_model=APIResponse[IndexState])
async def index_state(
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[IndexState]:
    """Not gated: the SPA needs this to explain *why* the rest is unavailable."""
    return APIResponse(data=service.index_state_view())


@router.post("/index/rebuild", response_model=APIResponse[RebuildResult])
async def rebuild_index(
    background: BackgroundTasks,
    service: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[RebuildResult]:
    """Drop and rebuild the whole collection from the stored document text.

    Required after switching embedding models: a collection's vector size is
    fixed at creation, and even a same-size model produces an incompatible
    vector space.
    """
    config = service.require_embedding_config()
    docs = service.pending_documents()
    if not docs:
        return APIResponse(
            data=RebuildResult(started=False, message="知识库中还没有文档", doc_count=0)
        )

    background.add_task(_rebuild_task, service.owner_id)
    return APIResponse(
        data=RebuildResult(
            started=True,
            message=f"已开始重建索引，共 {len(docs)} 篇文档，期间检索不可用",
            doc_count=len(docs),
        )
    )


async def _rebuild_task(owner_id: int) -> None:
    from app.database import SessionLocal

    with SessionLocal() as db:
        service = KnowledgeService(db, owner_id)
        try:
            config = service.require_embedding_config()
            await service.rebuild_all(config)
        except Exception as exc:  # noqa: BLE001 - state already records it
            logger.warning("rebuild task ended with an error: %s", exc)


# ---- Retrieval --------------------------------------------------------------

@router.post("/search", response_model=APIResponse[SearchResponse])
async def search(
    payload: SearchRequest,
    service: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[SearchResponse]:
    """Retrieval preview: exactly what the chat endpoint will feed the model."""
    hits = await service.search(
        payload.query,
        top_k=payload.top_k,
        project_id=payload.project_id,
        type_ids=payload.type_ids,
        score_threshold=payload.score_threshold,
    )
    return APIResponse(data=SearchResponse(hits=hits, total=len(hits)))
