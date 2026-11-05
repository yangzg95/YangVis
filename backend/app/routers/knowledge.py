"""Knowledge-base endpoints: types, documents, indexing and retrieval."""
from __future__ import annotations

import asyncio
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
    Request,
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
    DocumentContent,
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
    RepoCreate,
    RepoItem,
    RepoKeypair,
    RepoSyncResponse,
    RepoTestConnectionRequest,
    RepoTestConnectionResult,
    RepoUpdate,
    SearchRequest,
    SearchResponse,
)
from app.services.chunking import UnsupportedFileType, extract_text
from app.services.knowledge import KnowledgeService, index_document_task
from app.services.knowledge.repo_sync import (
    RepoError,
    RepoService,
    RepoSyncError,
    generate_keypair_sync,
    is_sync_running,
    probe_remote,
    repo_sync_task,
    rmtree_repo_dir,
)
from app.services.uploads import (
    content_length_exceeds,
    oversize_message,
    read_upload_limited,
)

logger = logging.getLogger("yangvis.knowledge")

settings = get_settings()

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def get_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> KnowledgeService:
    """Bind the service to the caller so handlers never see a bare session."""
    return KnowledgeService(db, user.user_id)


def get_repo_service(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> RepoService:
    return RepoService(db, user.user_id)


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
def list_projects(
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[ListResponse[KbProject]]:
    items = service.list_projects()
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/projects", response_model=APIResponse[KbProject])
def create_project(
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
def update_project(
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
def delete_project(
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
    logger.info(
        "owner %s deleted project %s (cascaded %d document(s))",
        service.owner_id,
        project_id,
        len(doc_ids),
    )
    return APIResponse(data=None, message="deleted")


# ---- Knowledge types --------------------------------------------------------

@router.get("/types", response_model=APIResponse[ListResponse[KnowledgeType]])
def list_knowledge_types(
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
def create_knowledge_type(
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
def update_knowledge_type(
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
def delete_knowledge_type(
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
    from app.services.knowledge import vectorstore

    try:
        await vectorstore.delete_by_doc(collection, owner_id, doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to purge vectors for doc %s: %s", doc_id, exc)


# ---- Documents --------------------------------------------------------------

@router.get(
    "/types/{type_id}/documents",
    response_model=APIResponse[ListResponse[KnowledgeDocument]],
)
def list_documents(
    type_id: int,
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=100),
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
def list_all_documents(
    type_id: Optional[int] = Query(default=None),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=100),
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[ListResponse[KnowledgeDocument]]:
    items, total = service.list_documents(type_id, page=page, page_size=page_size)
    return APIResponse(data=ListResponse(items=items, total=total))


@router.post("/types/{type_id}/documents", response_model=APIResponse[KnowledgeDocument])
async def upload_document(
    type_id: int,
    background: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    service: KnowledgeService = Depends(require_embedding_ready),
    repo_service: RepoService = Depends(get_repo_service),
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

    # 仓库类型里的文件只由同步物化进来；手动上传的文档下次同步既不会删除也不会
    # 更新，混在一起会让「类型内容 = 某次提交的快照」这个不变量破掉。
    if repo_service.get_by_type(type_id) is not None:
        return APIResponse(code=-1, message="代码仓库类型不能手动上传文档，请通过同步更新")

    # 先按 Content-Length 提前拒绝，再有界读取——read() 不设上限，会把
    # 整个上传体物化进内存。
    limit = settings.KB_MAX_UPLOAD_BYTES
    if content_length_exceeds(request.headers.get("content-length"), limit):
        return APIResponse(code=-1, message=oversize_message(limit))
    raw = await read_upload_limited(file, limit)
    if raw is None:
        return APIResponse(code=-1, message=oversize_message(limit))

    filename = name or file.filename or "untitled.txt"
    try:
        # PDF/DOCX 解析是纯 CPU 活：放在事件循环里会把正在推的 SSE 一起冻住。
        text = await asyncio.to_thread(extract_text, file.filename or filename, raw)
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
    logger.info(
        "owner %s uploaded document %s (%s, %d bytes), queued for indexing",
        service.owner_id,
        doc.id,
        filename,
        len(raw),
    )
    return APIResponse(data=KnowledgeService.to_document(doc))


@router.get("/documents/{doc_id}/content", response_model=APIResponse[DocumentContent])
def get_document_content(
    doc_id: int,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[DocumentContent]:
    """只读全文预览（仓库文件带行号的源码视图走这里）。

    不加向量模型闸门：读的是 MySQL 里已抽取的文本，不花任何 embedding 调用。
    """
    try:
        doc = service.get_document(doc_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # 末尾换行不影响行数；空内容给 0 行。splitlines 对 "a\n" 给 1 行，符合直觉。
    line_count = len(doc.content.splitlines()) if doc.content else 0
    return APIResponse(
        data=DocumentContent(
            id=doc.id,
            name=doc.filename,
            source=doc.source,
            language=doc.language,
            size=doc.size,
            content=doc.content,
            line_count=line_count,
            status=doc.status,
        )
    )


@router.post("/documents/{doc_id}/reindex", response_model=APIResponse[KnowledgeDocument])
def reindex_document(
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
def delete_document(
    doc_id: int,
    background: BackgroundTasks,
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[None]:
    try:
        doc = service.get_document(doc_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # 仓库文件只能通过同步（远端删除）或移除仓库回收，单删会被下次同步拉回来。
    if doc.source == "repo":
        return APIResponse(code=-1, message="仓库文件不能单独删除，请通过同步或移除仓库操作")
    service.delete_document(doc_id)
    background.add_task(_purge_vectors, service.collection, service.owner_id, doc_id)
    return APIResponse(data=None, message="deleted")


@router.post("/documents/batch-delete", response_model=APIResponse[DocBatchResult])
def batch_delete_documents(
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
            doc = service.get_document(doc_id)
        except LookupError:
            continue
        if doc.source == "repo":
            # 仓库文件跳过，和单删同一个口径。
            continue
        service.delete_document(doc_id)
        done += 1
        background.add_task(_purge_vectors, service.collection, service.owner_id, doc_id)
    return APIResponse(
        data=DocBatchResult(requested=len(payload.ids), done=done),
        message=f"已删除 {done} 篇文档",
    )


@router.post("/documents/batch-reindex", response_model=APIResponse[DocBatchResult])
def batch_reindex_documents(
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
def index_state(
    service: KnowledgeService = Depends(get_service),
) -> APIResponse[IndexState]:
    """Not gated: the SPA needs this to explain *why* the rest is unavailable."""
    return APIResponse(data=service.index_state_view())


@router.post("/index/rebuild", response_model=APIResponse[RebuildResult])
def rebuild_index(
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
    logger.info(
        "owner %s started an index rebuild over %d document(s)", service.owner_id, len(docs)
    )
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
        except Exception:  # noqa: BLE001 - rebuild_all 里已带堆栈记录
            pass


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


# ---- Code repositories ------------------------------------------------------


@router.post("/repos/keypair", response_model=APIResponse[RepoKeypair])
async def generate_repo_keypair(
    _gate: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[RepoKeypair]:
    """临时生成一对 ed25519 deploy key。不入库，私钥只出现这一次。"""
    private_pem, public_line = await asyncio.to_thread(generate_keypair_sync)
    return APIResponse(data=RepoKeypair(public_key=public_line, private_key=private_pem))


@router.post(
    "/test-connection", response_model=APIResponse[RepoTestConnectionResult]
)
async def test_repo_connection(
    payload: RepoTestConnectionRequest,
    _gate: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[RepoTestConnectionResult]:
    """探测远端：默认分支、HEAD、（SSH 时）首次记录的主机指纹。"""
    try:
        default_branch, head_sha, host, known_hosts, fingerprint = await probe_remote(
            payload.remote_url,
            private_key=payload.private_key,
            branch=payload.branch,
            http_token=payload.http_token,
            http_username=payload.http_username,
            expected_fingerprint=payload.expected_fingerprint,
        )
    except (RepoError, RepoSyncError) as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(
        data=RepoTestConnectionResult(
            default_branch=default_branch,
            head_sha=head_sha,
            host=host,
            host_fingerprint=fingerprint,
            known_hosts=known_hosts,
        )
    )


@router.get("/repos", response_model=APIResponse[ListResponse[RepoItem]])
def list_repos(
    project_id: int = Query(..., ge=1),
    service: RepoService = Depends(get_repo_service),
) -> APIResponse[ListResponse[RepoItem]]:
    try:
        service.knowledge.get_project(project_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    repos = service.list_for_project(project_id)
    items = [RepoItem(**service.to_item(repo)) for repo in repos]
    return APIResponse(data=ListResponse(items=items, total=len(items)))


@router.post("/repos", response_model=APIResponse[RepoItem])
def create_repo(
    payload: RepoCreate,
    background: BackgroundTasks,
    service: RepoService = Depends(get_repo_service),
    _gate: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[RepoItem]:
    """建同名知识类型 + 仓库行，并排队首次同步。"""
    try:
        repo = service.create(payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RepoError as exc:
        return APIResponse(code=-1, message=str(exc))
    background.add_task(repo_sync_task, service.knowledge.owner_id, repo.id)
    logger.info(
        "owner %s connected repo %s (%s), initial sync queued",
        service.knowledge.owner_id,
        repo.id,
        repo.remote_url,
    )
    return APIResponse(data=RepoItem(**service.to_item(repo)))


@router.get("/repos/{repo_id}", response_model=APIResponse[RepoItem])
def get_repo(
    repo_id: int,
    service: RepoService = Depends(get_repo_service),
) -> APIResponse[RepoItem]:
    try:
        repo = service.get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return APIResponse(data=RepoItem(**service.to_item(repo)))


@router.put("/repos/{repo_id}", response_model=APIResponse[RepoItem])
def update_repo(
    repo_id: int,
    payload: RepoUpdate,
    service: RepoService = Depends(get_repo_service),
) -> APIResponse[RepoItem]:
    try:
        repo = service.update(repo_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RepoError as exc:
        return APIResponse(code=-1, message=str(exc))
    return APIResponse(data=RepoItem(**service.to_item(repo)))


@router.post("/repos/{repo_id}/sync", response_model=APIResponse[RepoSyncResponse])
def sync_repo(
    repo_id: int,
    background: BackgroundTasks,
    service: RepoService = Depends(get_repo_service),
    _gate: KnowledgeService = Depends(require_embedding_ready),
) -> APIResponse[RepoSyncResponse]:
    """排队一次增量同步；已经在跑时不排队（避免重试刷出一串任务）。"""
    try:
        repo = service.get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if is_sync_running(repo_id):
        return APIResponse(
            data=RepoSyncResponse(
                started=False, already_running=True, message="该仓库正在同步中"
            )
        )
    background.add_task(repo_sync_task, service.knowledge.owner_id, repo.id)
    return APIResponse(
        data=RepoSyncResponse(started=True, message="已开始同步")
    )


@router.post("/repos/{repo_id}/retrust", response_model=APIResponse[RepoItem])
async def retrust_repo(
    repo_id: int,
    service: RepoService = Depends(get_repo_service),
) -> APIResponse[RepoItem]:
    """主机指纹变更后的人工确认：重新 accept-new 并覆盖记录。"""
    from app.crypto import decrypt

    try:
        repo = service.get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if repo.auth_type != "ssh_key" or not repo.private_key_enc:
        return APIResponse(code=-1, message="该仓库没有配置 SSH 凭据")
    try:
        private_pem = decrypt(repo.private_key_enc)
        _branch, _sha, _host, known_hosts, fingerprint = await probe_remote(
            repo.remote_url, private_key=private_pem
        )
    except (RepoError, RepoSyncError) as exc:
        return APIResponse(code=-1, message=str(exc))
    if not known_hosts:
        return APIResponse(code=-1, message="未能获取新的主机指纹")
    service.retrust(repo, known_hosts, fingerprint)
    logger.warning(
        "owner %s re-trusted host key for repo %s: %s",
        service.knowledge.owner_id,
        repo.id,
        fingerprint,
    )
    return APIResponse(data=RepoItem(**service.to_item(repo)))


@router.delete("/repos/{repo_id}", response_model=APIResponse[None])
def delete_repo(
    repo_id: int,
    background: BackgroundTasks,
    service: RepoService = Depends(get_repo_service),
) -> APIResponse[None]:
    """删仓库 = 删其类型（文档/分片级联）+ 后台清向量与本地目录。"""
    try:
        db_repo_id, doc_ids = service.delete(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    for doc_id in doc_ids:
        background.add_task(
            _purge_vectors, service.knowledge.collection, service.knowledge.owner_id, doc_id
        )
    background.add_task(rmtree_repo_dir, service.knowledge.owner_id, db_repo_id)
    return APIResponse(data=None, message="deleted")
