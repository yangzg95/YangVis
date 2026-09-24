"""知识库：文档、切分、embedding 与检索。

所有东西都按单个用户隔离。handler 拿到的是已经绑定到调用者的 service，
从来看不到裸的 session，因此也就不存在每个 handler 里那句可能漏写的
``where owner_id = ?``。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.errors import (
    CODE_EMBEDDING_NOT_READY,
    CODE_INDEX_MODEL_MISMATCH,
    BusinessError,
)
from app.models.entities import (
    KbChunk,
    KbDocument,
    KbIndexState,
    KbProject,
    KnowledgeType,
    ModelConfig,
)
from app.models.schemas import (
    DocumentStatus,
    IndexState,
    KbProject as KbProjectSchema,
    KbProjectCreate,
    KbProjectUpdate,
    KnowledgeDocument,
    KnowledgeType as KnowledgeTypeSchema,
    KnowledgeTypeCreate,
    KnowledgeTypeUpdate,
    ModelPurpose,
    SearchHit,
)
from app.services import vectorstore
from app.services.chunking import split_text
from app.services.providers import build_embeddings

logger = logging.getLogger("yangvis.knowledge")

settings = get_settings()

# 同一篇文档的索引互斥：上传排队、手动重建、全量重建可能撞在一起，并发执行
# 会互相覆盖切分结果与状态。进程内锁——多 worker 下的边界与 login_guard 相同。
_doc_index_locks: Dict[Tuple[int, int], asyncio.Lock] = {}


def _doc_index_lock(owner_id: int, doc_id: int) -> asyncio.Lock:
    return _doc_index_locks.setdefault((owner_id, doc_id), asyncio.Lock())


class IndexStatus:
    UNINITIALIZED = "uninitialized"
    INDEXING = "indexing"
    READY = "ready"
    REBUILDING = "rebuilding"
    ERROR = "error"


def model_signature(config: ModelConfig) -> str:
    """用来标识某个索引背后到底是哪个 embedding 模型的指纹。

    光看维度是不够的：两个都是 1024 维的不同模型，产生的向量空间彼此并不
    兼容，而 Qdrant 会心安理得地拿一个模型出来的 query 去和另一个模型的
    point 打分，一句抱怨都没有。
    """
    return f"{config.base_url}::{config.model_name}"


class KnowledgeService:
    """单个用户的知识库。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id
        self._collection = vectorstore.collection_name(owner_id)

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select, column) -> Select:
        """把查询限定在当前 owner 上。所有读操作都要经过这里。"""
        return stmt.where(column == self._owner_id)

    @property
    def owner_id(self) -> int:
        return self._owner_id

    @property
    def collection(self) -> str:
        return self._collection

    # -- embedding 配置 ----------------------------------------------------

    def get_embedding_config(self) -> Optional[ModelConfig]:
        """调用者默认的 embedding 模型，前提是它已经通过验证。"""
        config = self._db.scalar(
            select(ModelConfig).where(
                ModelConfig.owner_id == self._owner_id,
                ModelConfig.purpose == ModelPurpose.EMBEDDING.value,
                ModelConfig.is_default.is_(True),
            )
        )
        if config is None or not config.last_test_ok or not config.vector_size:
            return None
        return config

    def require_embedding_config(self) -> ModelConfig:
        config = self.get_embedding_config()
        if config is None:
            raise BusinessError(
                CODE_EMBEDDING_NOT_READY,
                "尚未配置可用的向量模型，无法使用知识库。"
                "请前往「系统设置 - 模型配置」添加向量模型并通过连通性测试。",
            )
        return config

    # -- 项目 ---------------------------------------------------------------

    def list_projects(self) -> List[KbProjectSchema]:
        projects = list(
            self._db.scalars(
                self._scope(select(KbProject), KbProject.owner_id).order_by(
                    KbProject.id.desc()
                )
            ).all()
        )
        if not projects:
            return []

        type_counts = dict(
            self._db.execute(
                select(KnowledgeType.project_id, func.count(KnowledgeType.id))
                .where(KnowledgeType.owner_id == self._owner_id)
                .group_by(KnowledgeType.project_id)
            ).all()
        )
        # 文档是通过它的知识类型才归到项目底下的，所以这个计数只能 join
        # 出来，不能直接从文档那行读。
        doc_counts = dict(
            self._db.execute(
                select(KnowledgeType.project_id, func.count(KbDocument.id))
                .join(KbDocument, KbDocument.type_id == KnowledgeType.id)
                .where(
                    KnowledgeType.owner_id == self._owner_id,
                    KbDocument.owner_id == self._owner_id,
                )
                .group_by(KnowledgeType.project_id)
            ).all()
        )
        return [
            KbProjectSchema(
                id=item.id,
                name=item.name,
                description=item.description,
                type_count=int(type_counts.get(item.id, 0)),
                document_count=int(doc_counts.get(item.id, 0)),
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in projects
        ]

    def get_project(self, project_id: int) -> KbProject:
        item = self._db.scalar(
            self._scope(select(KbProject), KbProject.owner_id).where(
                KbProject.id == project_id
            )
        )
        if item is None:
            raise LookupError("项目不存在")
        return item

    def create_project(self, payload: KbProjectCreate) -> KbProject:
        item = KbProject(
            owner_id=self._owner_id,
            name=payload.name,
            description=payload.description,
        )
        self._db.add(item)
        self._db.commit()
        self._db.refresh(item)
        return item

    def update_project(self, project_id: int, payload: KbProjectUpdate) -> KbProject:
        item = self.get_project(project_id)
        data = payload.model_dump(exclude_unset=True)
        if data.get("name") is not None:
            item.name = data["name"]
        if "description" in data:
            item.description = data["description"]
        self._db.commit()
        self._db.refresh(item)
        return item

    def delete_project(self, project_id: int) -> List[int]:
        """删除一个项目以及它下面的一切。返回被删掉的文档 id。"""
        item = self.get_project(project_id)
        doc_ids: List[int] = []
        for type_id in self.type_ids_of_project(project_id):
            doc_ids.extend(self.delete_type(type_id))
        self._db.delete(item)
        self._db.commit()
        return doc_ids

    def type_ids_of_project(self, project_id: int) -> List[int]:
        """一个项目下的所有知识类型。这就是项目级别的检索范围。"""
        return list(
            self._db.scalars(
                select(KnowledgeType.id).where(
                    KnowledgeType.owner_id == self._owner_id,
                    KnowledgeType.project_id == project_id,
                )
            ).all()
        )

    # -- 知识类型 -----------------------------------------------------------

    def list_types(self, project_id: int) -> List[KnowledgeTypeSchema]:
        types = list(
            self._db.scalars(
                self._scope(select(KnowledgeType), KnowledgeType.owner_id)
                .where(KnowledgeType.project_id == project_id)
                .order_by(KnowledgeType.id.desc())
            ).all()
        )
        counts = dict(
            self._db.execute(
                select(KbDocument.type_id, func.count(KbDocument.id))
                .where(KbDocument.owner_id == self._owner_id)
                .group_by(KbDocument.type_id)
            ).all()
        )
        return [
            KnowledgeTypeSchema(
                id=item.id,
                project_id=item.project_id,
                name=item.name,
                description=item.description,
                document_count=int(counts.get(item.id, 0)),
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in types
        ]

    def get_type(self, type_id: int) -> KnowledgeType:
        item = self._db.scalar(
            self._scope(select(KnowledgeType), KnowledgeType.owner_id).where(
                KnowledgeType.id == type_id
            )
        )
        if item is None:
            # 返回 404 而不是 403：确认别人的那行记录存在，本身就是一种
            # 泄露。
            raise LookupError("知识类型不存在")
        return item

    def create_type(self, payload: KnowledgeTypeCreate) -> KnowledgeType:
        # 走的是带 owner 限定的 getter，所以一个知识类型绝不可能被挂到
        # 别人的项目下面。
        project = self.get_project(payload.project_id)
        item = KnowledgeType(
            owner_id=self._owner_id,
            project_id=project.id,
            name=payload.name,
            description=payload.description,
        )
        self._db.add(item)
        self._db.commit()
        self._db.refresh(item)
        return item

    def update_type(self, type_id: int, payload: KnowledgeTypeUpdate) -> KnowledgeType:
        item = self.get_type(type_id)
        data = payload.model_dump(exclude_unset=True)
        if data.get("name") is not None:
            item.name = data["name"]
        if "description" in data:
            item.description = data["description"]
        self._db.commit()
        self._db.refresh(item)
        return item

    def delete_type(self, type_id: int) -> List[int]:
        """删除一个知识类型及其下的文档。返回被删掉的文档 id。"""
        item = self.get_type(type_id)
        doc_ids = list(
            self._db.scalars(
                select(KbDocument.id).where(
                    KbDocument.owner_id == self._owner_id,
                    KbDocument.type_id == type_id,
                )
            ).all()
        )
        for doc_id in doc_ids:
            self._db.execute(
                delete(KbChunk).where(
                    KbChunk.owner_id == self._owner_id, KbChunk.doc_id == doc_id
                )
            )
        self._db.execute(
            delete(KbDocument).where(
                KbDocument.owner_id == self._owner_id, KbDocument.type_id == type_id
            )
        )
        self._db.delete(item)
        self._db.commit()
        self._refresh_counts()
        return doc_ids

    # -- 文档 ---------------------------------------------------------------

    def list_documents(
        self,
        type_id: Optional[int] = None,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Tuple[List[KnowledgeDocument], int]:
        """文档列表 + 真实总数。不传 page 时全量返回（兼容旧调用方）。"""
        stmt = self._scope(select(KbDocument), KbDocument.owner_id)
        if type_id is not None:
            stmt = stmt.where(KbDocument.type_id == type_id)
        total = self._db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(KbDocument.id.desc())
        if page is not None and page_size is not None:
            stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        docs = list(self._db.scalars(stmt).all())
        return [self.to_document(doc) for doc in docs], total

    def get_document(self, doc_id: int) -> KbDocument:
        doc = self._db.scalar(
            self._scope(select(KbDocument), KbDocument.owner_id).where(KbDocument.id == doc_id)
        )
        if doc is None:
            raise LookupError("文档不存在")
        return doc

    def create_document(
        self,
        *,
        filename: str,
        mime: Optional[str],
        size: int,
        content: str,
        type_id: Optional[int],
    ) -> KbDocument:
        """把抽取出来的文本存下来，并排队等待建索引。"""
        doc = KbDocument(
            owner_id=self._owner_id,
            scope="private",
            type_id=type_id,
            filename=filename,
            mime=mime,
            size=size,
            content=content,
            status=DocumentStatus.PENDING.value,
        )
        self._db.add(doc)
        self._db.commit()
        self._db.refresh(doc)
        return doc

    def delete_document(self, doc_id: int) -> None:
        doc = self.get_document(doc_id)
        self._db.execute(
            delete(KbChunk).where(KbChunk.owner_id == self._owner_id, KbChunk.doc_id == doc.id)
        )
        self._db.delete(doc)
        self._db.commit()
        self._refresh_counts()

    def set_document_status(
        self,
        doc: KbDocument,
        status: DocumentStatus,
        *,
        error: Optional[str] = None,
        chunk_count: Optional[int] = None,
    ) -> None:
        doc.status = status.value
        doc.error_msg = error[:512] if error else None
        if chunk_count is not None:
            doc.chunk_count = chunk_count
        self._db.commit()

    @staticmethod
    def to_document(doc: KbDocument) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=doc.id,
            name=doc.filename,
            type_id=doc.type_id,
            size=doc.size,
            mime=doc.mime,
            status=DocumentStatus(doc.status),
            error_msg=doc.error_msg,
            chunk_count=doc.chunk_count,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )

    # -- 索引状态 -----------------------------------------------------------

    def get_index_state(self) -> Optional[KbIndexState]:
        return self._db.get(KbIndexState, self._owner_id)

    def _ensure_index_state(self, config: ModelConfig) -> KbIndexState:
        state = self.get_index_state()
        if state is None:
            state = KbIndexState(
                owner_id=self._owner_id,
                collection_name=self._collection,
                embed_model_id=config.id,
                embed_model_sig=model_signature(config),
                vector_size=config.vector_size or 0,
                status=IndexStatus.UNINITIALIZED,
            )
            self._db.add(state)
            self._db.commit()
            self._db.refresh(state)
        return state

    def set_index_status(self, status: str, *, error: Optional[str] = None) -> None:
        state = self.get_index_state()
        if state is None:
            return
        state.status = status
        state.error_msg = error[:512] if error else None
        state.updated_at = datetime.now(timezone.utc)
        self._db.commit()

    def _refresh_counts(self) -> None:
        state = self.get_index_state()
        if state is None:
            return
        state.doc_count = int(
            self._db.scalar(
                select(func.count(KbDocument.id)).where(
                    KbDocument.owner_id == self._owner_id,
                    KbDocument.status == DocumentStatus.READY.value,
                )
            )
            or 0
        )
        state.chunk_count = int(
            self._db.scalar(
                select(func.count(KbChunk.id)).where(KbChunk.owner_id == self._owner_id)
            )
            or 0
        )
        if state.status in (IndexStatus.UNINITIALIZED, IndexStatus.READY) and state.chunk_count:
            state.status = IndexStatus.READY
        self._db.commit()

    def index_state_view(self) -> IndexState:
        """给 SPA 用的索引状态，含模型漂移标记。"""
        state = self.get_index_state()
        config = self.get_embedding_config()

        if state is None:
            return IndexState(
                status=IndexStatus.UNINITIALIZED,
                collection_name=self._collection,
            )

        mismatch = (
            config is not None
            and state.chunk_count > 0
            and state.embed_model_sig != ""
            and state.embed_model_sig != model_signature(config)
        )
        return IndexState(
            status=state.status,
            collection_name=state.collection_name,
            vector_size=state.vector_size,
            doc_count=state.doc_count,
            chunk_count=state.chunk_count,
            error_msg=state.error_msg,
            model_mismatch=mismatch,
            updated_at=state.updated_at,
        )

    def assert_index_usable(self, config: ModelConfig) -> KbIndexState:
        """拒绝去检索一个由别的 embedding 模型建出来的索引。

        这种情况下 Qdrant 什么都不会报：维度甚至可能刚好对得上，它会返回
        一批打分自信、语义上却毫不相干的 chunk。唯一的症状就是答案变烂，
        所以这道检查只能放在这里做。
        """
        state = self.get_index_state()
        if state is None or state.chunk_count == 0:
            return self._ensure_index_state(config)

        if state.embed_model_sig and state.embed_model_sig != model_signature(config):
            raise BusinessError(
                CODE_INDEX_MODEL_MISMATCH,
                "当前向量模型与知识库索引所用模型不一致，检索结果将不可靠。"
                "请在知识库页面重建索引后再试。",
            )
        return state

    # -- 索引构建 -----------------------------------------------------------

    def replace_chunks(self, doc: KbDocument, texts: Sequence[str]) -> List[KbChunk]:
        """把一篇文档的 chunk 记录整体换成新的一批。"""
        self._db.execute(
            delete(KbChunk).where(KbChunk.owner_id == self._owner_id, KbChunk.doc_id == doc.id)
        )
        chunks = [
            KbChunk(
                owner_id=self._owner_id,
                doc_id=doc.id,
                seq=seq,
                content=text,
                char_count=len(text),
                point_id=vectorstore.new_point_id(),
            )
            for seq, text in enumerate(texts)
        ]
        self._db.add_all(chunks)
        self._db.commit()
        for chunk in chunks:
            self._db.refresh(chunk)
        return chunks

    def pending_documents(self) -> List[KbDocument]:
        return list(
            self._db.scalars(
                self._scope(select(KbDocument), KbDocument.owner_id).order_by(KbDocument.id)
            ).all()
        )

    async def index_document(self, doc: KbDocument, config: ModelConfig) -> int:
        """对一篇文档做切分、embedding 并写入。返回 chunk 数量。"""
        state = self._ensure_index_state(config)
        vector_size = config.vector_size or 0
        if vector_size <= 0:
            raise BusinessError(
                CODE_EMBEDDING_NOT_READY,
                "向量模型尚未探测到维度，请在模型配置中重新执行连通性测试。",
            )

        self.set_document_status(doc, DocumentStatus.INDEXING)

        texts = split_text(doc.content)
        if not texts:
            self.set_document_status(doc, DocumentStatus.READY, chunk_count=0)
            return 0

        chunks = self.replace_chunks(doc, texts)

        # 写新向量之前，先把上一轮残留的向量清掉：point id 是重新生成的，
        # 旧的那份不删就会一直留在那儿。
        await vectorstore.delete_by_doc(self._collection, self._owner_id, doc.id)
        await vectorstore.ensure_collection(self._collection, vector_size)

        embeddings = build_embeddings(config)
        batch_size = max(1, settings.KB_EMBED_BATCH_SIZE)

        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = await embeddings.aembed_documents([chunk.content for chunk in batch])

            if vectors and len(vectors[0]) != vector_size:
                # provider 在我们眼皮底下改了输出形状；这些东西写进去会把
                # 整个 collection 搞坏。
                raise BusinessError(
                    CODE_INDEX_MODEL_MISMATCH,
                    f"向量维度与索引不符（模型返回 {len(vectors[0])}，索引为 {vector_size}），"
                    "请重建索引。",
                )

            await vectorstore.upsert_points(
                self._collection,
                point_ids=[chunk.point_id for chunk in batch],
                vectors=vectors,
                payloads=[
                    {
                        "owner_id": self._owner_id,
                        "doc_id": doc.id,
                        "chunk_id": chunk.id,
                        "seq": chunk.seq,
                        "filename": doc.filename,
                        "type_id": doc.type_id,
                    }
                    for chunk in batch
                ],
            )

        state.embed_model_id = config.id
        state.embed_model_sig = model_signature(config)
        state.vector_size = vector_size
        self._db.commit()

        self.set_document_status(doc, DocumentStatus.READY, chunk_count=len(chunks))
        self._refresh_counts()
        return len(chunks)

    async def rebuild_all(self, config: ModelConfig) -> Tuple[int, int]:
        """拿当前的 embedding 模型，把所有文档重新索引一遍。

        collection 被删掉重建，因为它的向量维度是不可变的；真正的事实来源
        是 MySQL 里的 ``kb_document.content``——这也正是上传时要把抽取出来的
        文本落库的原因。
        """
        vector_size = config.vector_size or 0
        state = self._ensure_index_state(config)
        state.status = IndexStatus.REBUILDING
        state.error_msg = None
        self._db.commit()

        docs = self.pending_documents()
        indexed_docs = 0
        indexed_chunks = 0
        done_ids: set[int] = set()
        started = time.monotonic()

        try:
            await vectorstore.ensure_collection(self._collection, vector_size, recreate=True)
            for doc in docs:
                async with _doc_index_lock(self._owner_id, doc.id):
                    count = await self.index_document(doc, config)
                done_ids.add(doc.id)
                indexed_docs += 1
                indexed_chunks += count
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落成状态暴露出来
            logger.exception("rebuild failed for owner %s", self._owner_id)
            # collection 已被清空重建：没走完的文档虽然 MySQL 里还是 READY，
            # 向量其实已经没了。标成 ERROR，避免「就绪但检索不到」的假象。
            interrupted = "索引重建中断，请重新执行重建"
            for doc in docs:
                if doc.id not in done_ids:
                    try:
                        self.set_document_status(doc, DocumentStatus.ERROR, error=interrupted)
                    except Exception:  # noqa: BLE001 - 清理动作不能盖住原始异常
                        logger.warning("failed to mark document %s as interrupted", doc.id)
            self.set_index_status(IndexStatus.ERROR, error=str(exc))
            raise

        self.set_index_status(IndexStatus.READY)
        self._refresh_counts()
        logger.info(
            "owner %s rebuilt the index: %d document(s), %d chunk(s), %.1fs",
            self._owner_id,
            indexed_docs,
            indexed_chunks,
            time.monotonic() - started,
        )
        return indexed_docs, indexed_chunks

    # -- 检索 ---------------------------------------------------------------

    def _resolve_type_scope(
        self, project_id: Optional[int], type_ids: Optional[Sequence[int]]
    ) -> Optional[List[int]]:
        """把（项目, 类型）这组选择换算成实际用于过滤的 type id 列表。

        ``None`` 表示「完全不过滤」；空列表表示「这组选择解析下来什么都没
        剩」，调用方必须就地短路掉，而不是继续往下传。
        """
        if project_id is None:
            # 没有指定项目：如果显式给了类型列表就照办，否则就在这个 owner
            # 的所有内容里搜。
            return None if type_ids is None else list(dict.fromkeys(type_ids))

        allowed = self.type_ids_of_project(project_id)
        if type_ids is None:
            return allowed

        permitted = set(allowed)
        return [type_id for type_id in dict.fromkeys(type_ids) if type_id in permitted]

    async def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        project_id: Optional[int] = None,
        type_ids: Optional[Sequence[int]] = None,
        score_threshold: Optional[float] = None,
    ) -> List[SearchHit]:
        """检索 chunk，限定在某个项目内，可以再进一步限定到它的部分类型。

        ``project_id`` 是在这里解析成一组 type id 的，而不是下推给 Qdrant，
        因为在项目这个概念出现之前建好的 chunk，payload 里没有
        ``project_id``。显式传入的 ``type_ids`` 会把这组范围再缩小一层，并
        且是与之取交集的，这样即便 SPA 传来一个过期的 id，也伸不到所选项目
        之外去。
        """
        config = self.require_embedding_config()
        self.assert_index_usable(config)

        scope = self._resolve_type_scope(project_id, type_ids)
        if scope is not None and not scope:
            # 范围里什么都没有。拿一个空的 MatchAny 去搜会匹配到所有东西，
            # 那和要求的正好相反。
            return []

        threshold = (
            settings.KB_SCORE_THRESHOLD if score_threshold is None else score_threshold
        )

        try:
            embeddings = build_embeddings(config)
            vector = await embeddings.aembed_query(query)

            points = await vectorstore.search(
                self._collection,
                vector=vector,
                owner_id=self._owner_id,
                top_k=top_k,
                type_ids=scope,
                score_threshold=threshold,
            )
        except Exception as exc:
            # 外部调用失败（embed provider / qdrant）：用户只看到「检索失败」，
            # 原因得留在这里。
            logger.warning("knowledge search failed for owner %s: %s", self._owner_id, exc)
            raise
        if not points:
            return []

        # chunk 文本是从 MySQL 回读的，而不是从 Qdrant 的 payload 里取：
        # MySQL 才是事实来源，而且 payload 保持精简，也省得把每篇文档都存
        # 两份。
        chunk_ids = [int(point.payload.get("chunk_id", 0)) for point in points]
        rows = {
            chunk.id: chunk
            for chunk in self._db.scalars(
                select(KbChunk).where(
                    KbChunk.owner_id == self._owner_id, KbChunk.id.in_(chunk_ids)
                )
            ).all()
        }

        hits: List[SearchHit] = []
        for point in points:
            payload = point.payload or {}
            chunk = rows.get(int(payload.get("chunk_id", 0)))
            if chunk is None:
                # 这条向量比它对应的记录活得还久（查询过程中文档被删了）。
                logger.debug("skipping an orphan vector (chunk %s)", payload.get("chunk_id"))
                continue
            hits.append(
                SearchHit(
                    chunk_id=chunk.id,
                    doc_id=chunk.doc_id,
                    filename=str(payload.get("filename", "")),
                    seq=chunk.seq,
                    score=float(point.score),
                    content=chunk.content,
                )
            )
        return hits


# ---- 后台索引 ---------------------------------------------------------------

async def index_document_task(owner_id: int, doc_id: int) -> None:
    """在请求生命周期之外给一篇文档建索引。

    自己开一个 session：BackgroundTask 真正跑起来的时候，请求级别的那个
    session 早就关掉了。
    """
    with SessionLocal() as db:
        service = KnowledgeService(db, owner_id)
        try:
            doc = service.get_document(doc_id)
        except LookupError:
            return

        started = time.monotonic()
        try:
            config = service.require_embedding_config()
            async with _doc_index_lock(owner_id, doc_id):
                chunks = await service.index_document(doc, config)
            logger.info(
                "indexed document %s (%s): %d chunk(s), %.1fs",
                doc_id,
                doc.filename,
                chunks,
                time.monotonic() - started,
            )
        except BusinessError as exc:
            logger.warning("indexing document %s was rejected: %s", doc_id, exc.msg)
            service.set_document_status(doc, DocumentStatus.ERROR, error=exc.msg)
        except Exception as exc:  # noqa: BLE001 - 绝不让后台任务无声无息地死掉
            logger.exception("indexing failed for document %s", doc_id)
            service.set_document_status(doc, DocumentStatus.ERROR, error=str(exc))


def reset_stale_indexing(db: Session) -> int:
    """把卡在 ``indexing`` 状态的文档标记为失败，好让它们能被重试。

    BackgroundTask 是活在 Gunicorn worker 里的。一次重启或者一次崩溃会把
    任务一起带走，留下那行记录永远停在 ``indexing``，于是 SPA 上转着一个
    永远不会结束的圈。启动时把这些清掉，就能把它变成一个看得见、也能重试
    的错误。
    """
    stale = list(
        db.scalars(
            select(KbDocument).where(KbDocument.status == DocumentStatus.INDEXING.value)
        ).all()
    )
    for doc in stale:
        doc.status = DocumentStatus.ERROR.value
        doc.error_msg = "索引任务在服务重启时中断，请重新索引"
    if stale:
        db.commit()
        logger.warning("reset %s document(s) stuck in indexing", len(stale))
    return len(stale)
