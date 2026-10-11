"""知识库域：作用域隔离、索引生命周期与检索范围。

这个域此前一个测试都没有，而它偏偏属于「错了也不报错、只是答案变烂」的那类
代码：维度刚好相同的两个模型，Qdrant 会一声不响地拿 A 的 query 去比 B 的
point，分数自信、结果全是噪声。所以这里钉的重点是边界而不是 happy path：

- 作用域：service 绑死单个 owner，别人的行一律 ``LookupError``（404 而非 403）。
- 模型漂移：检索前必须比对索引指纹；重建中断要把「向量已经没了」的文档标出来。
- 删除：文档删掉要把它那把互斥锁从表里摘掉，且不许回查刚删掉的行。
- 检索范围：项目要就地解析成一组 type id 再过滤，交集为空必须短路。
- 接口层：超限拒绝、索引排队、批量操作跳过别人的 id。

全部跑文件型 SQLite 的假连接；Qdrant 与 embedding provider 用桩替掉，不连真实
服务、也不碰共享库。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.errors import CODE_EMBEDDING_NOT_READY, CODE_INDEX_MODEL_MISMATCH, BusinessError
from app.main import app
from app.models.entities import (
    KbChunk,
    KbDocument,
    KbIndexState,
    KbProject,
    KnowledgeType,
    ModelConfig,
)
from app.models.schemas import (
    CurrentUser,
    DocumentStatus,
    KbProjectCreate,
    KbProjectUpdate,
    KnowledgeTypeCreate,
    KnowledgeTypeUpdate,
    ModelPurpose,
)
from app.routers import knowledge as knowledge_router
from app.services.knowledge import (
    IndexStatus,
    KnowledgeService,
    model_signature,
    reset_stale_indexing,
    vectorstore,
)
from app.services.knowledge import service as kb
from app.services.knowledge.service import (
    _doc_index_lock,
    _doc_index_locks,
    _release_doc_lock,
)

OWNER = 1
OTHER = 2
DIM = 4
BASE_URL = "https://embed.test/v1"


# ---- 造数据的小工具 ---------------------------------------------------------


def _project(db, *, owner_id: int = OWNER, name: str = "项目 A") -> KbProject:
    item = KbProject(owner_id=owner_id, name=name, description=None)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _type(db, project_id: int, *, owner_id: int = OWNER, name: str = "类型 A") -> KnowledgeType:
    item = KnowledgeType(
        owner_id=owner_id, project_id=project_id, name=name, description=None
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _document(
    db,
    *,
    owner_id: int = OWNER,
    type_id: int | None = None,
    content: str = "第一段内容。第二段内容。",
    filename: str = "note.txt",
    status: str = DocumentStatus.PENDING.value,
) -> KbDocument:
    doc = KbDocument(
        owner_id=owner_id,
        type_id=type_id,
        filename=filename,
        mime="text/plain",
        size=len(content.encode()),
        content=content,
        status=status,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _chunk(
    db, doc: KbDocument, *, owner_id: int = OWNER, seq: int = 0, content: str = "切片"
) -> KbChunk:
    chunk = KbChunk(
        owner_id=owner_id,
        doc_id=doc.id,
        seq=seq,
        content=content,
        char_count=len(content),
        point_id=vectorstore.new_point_id(),
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk


def _embedding_model(
    db,
    *,
    owner_id: int = OWNER,
    model_name: str = "bge-m3",
    base_url: str = BASE_URL,
    title: str = "本地向量",
    vector_size: int | None = DIM,
    is_default: bool = True,
    last_test_ok: bool = True,
) -> ModelConfig:
    config = ModelConfig(
        owner_id=owner_id,
        purpose=ModelPurpose.EMBEDDING.value,
        title=title,
        model_name=model_name,
        base_url=base_url,
        is_default=is_default,
        vector_size=vector_size,
        last_test_ok=last_test_ok,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def _point(chunk_id: int, score: float, **extra) -> SimpleNamespace:
    """伪造一个 ``models.ScoredPoint``：service 只读 payload 与 score。"""
    payload = {"chunk_id": chunk_id, "filename": "note.txt"}
    payload.update(extra)
    return SimpleNamespace(payload=payload, score=score)


class _Embeddings:
    """假的 embedding provider：维度可控，可指定第几批开始失败。"""

    def __init__(self, dim: int = DIM, *, fail_after: int | None = None, fail_with=None):
        self.dim = dim
        self.fail_after = fail_after
        self.fail_with = fail_with or RuntimeError("provider 503")
        self.doc_batches: list[list[str]] = []
        self.queries: list[str] = []

    def _boom(self) -> None:
        if self.fail_after is not None and len(self.doc_batches) >= self.fail_after:
            raise self.fail_with

    async def aembed_documents(self, texts):
        self._boom()
        self.doc_batches.append(list(texts))
        return [[0.1] * self.dim for _ in texts]

    async def aembed_query(self, text):
        if self.fail_with is not None and self.fail_after == 0:
            raise self.fail_with
        self.queries.append(text)
        return [0.1] * self.dim


class _VectorStore:
    """假向量库：记录每次调用，检索结果由测试喂进去。"""

    def __init__(self):
        self.deleted: list[tuple] = []
        self.ensured: list[tuple] = []
        self.upserted: list[dict] = []
        self.queries: list[SimpleNamespace] = []
        self.points: list = []
        self.fail_delete: Exception | None = None

    async def delete_by_doc(self, name, owner_id, doc_id):
        if self.fail_delete is not None:
            raise self.fail_delete
        self.deleted.append((name, owner_id, doc_id))

    async def ensure_collection(self, name, vector_size, *, recreate=False):
        self.ensured.append((name, vector_size, recreate))

    async def upsert_points(self, name, *, point_ids, vectors, payloads):
        if not point_ids:
            return
        self.upserted.append(
            {
                "collection": name,
                "point_ids": list(point_ids),
                "vectors": [list(v) for v in vectors],
                "payloads": [dict(p) for p in payloads],
            }
        )

    async def search(self, name, *, vector, owner_id, top_k=8, type_ids=None, score_threshold=None):
        self.queries.append(
            SimpleNamespace(
                collection=name,
                vector=list(vector),
                owner_id=owner_id,
                top_k=top_k,
                type_ids=type_ids,
                score_threshold=score_threshold,
            )
        )
        return list(self.points)


def _stub_embeddings(monkeypatch, **kwargs) -> _Embeddings:
    fake = _Embeddings(**kwargs)
    monkeypatch.setattr(kb, "build_embeddings", lambda config: fake)
    return fake


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'knowledge.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, future=True)


@pytest.fixture()
def db(maker, monkeypatch):
    """一个假 session。

    两处 ``SessionLocal`` 都要换掉：``get_db`` 走 ``app.database`` 那个，而后台
    索引任务在 ``app.services.knowledge.service`` 里另开一个，不改就会连到真实库上。
    """
    session = maker()
    monkeypatch.setattr("app.database.SessionLocal", maker)
    monkeypatch.setattr(kb, "SessionLocal", maker)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def svc(db):
    return KnowledgeService(db, OWNER)


@pytest.fixture(autouse=True)
def _isolate_lock_table():
    """锁表是模块级 dict，测试之间必须互不残留。"""
    _doc_index_locks.clear()
    yield
    _doc_index_locks.clear()


@pytest.fixture()
def vs(monkeypatch):
    fake = _VectorStore()
    for name in ("delete_by_doc", "ensure_collection", "upsert_points", "search"):
        monkeypatch.setattr(vectorstore, name, getattr(fake, name))
    return fake


@pytest.fixture()
def queued():
    """接口层只关心「排了哪个后台任务」，真实执行在后面的分节单独测。"""
    return []


@pytest.fixture()
def client(db, queued, monkeypatch):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=OWNER, username="tester"
    )
    monkeypatch.setattr(
        knowledge_router,
        "index_document_task",
        lambda owner_id, doc_id: queued.append(("index", owner_id, doc_id)),
    )
    monkeypatch.setattr(
        knowledge_router, "_rebuild_task", lambda owner_id: queued.append(("rebuild", owner_id))
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---- 作用域与越权 -----------------------------------------------------------


def test_service_is_bound_to_one_owner(svc):
    assert svc.owner_id == OWNER
    assert svc.collection == f"kb_u{OWNER}"


def test_lists_only_contain_callers_own_rows(svc, db):
    mine = _project(db, name="我的")
    theirs = _project(db, owner_id=OTHER, name="别人的")
    _type(db, mine.id, name="我的类型")
    _type(db, theirs.id, owner_id=OTHER, name="别人的类型")

    names = [p.name for p in svc.list_projects()]
    assert names == ["我的"]
    assert [t.name for t in svc.list_types(mine.id)] == ["我的类型"]
    assert svc.list_types(theirs.id) == []


def test_getters_raise_lookup_for_foreign_rows(svc, db):
    theirs_project = _project(db, owner_id=OTHER, name="别人的项目")
    theirs_type = _type(db, theirs_project.id, owner_id=OTHER, name="别人的类型")
    theirs_doc = _document(db, owner_id=OTHER, type_id=theirs_type.id)

    # 404 而不是 403：承认那一行存在本身就是泄露。
    with pytest.raises(LookupError):
        svc.get_project(theirs_project.id)
    with pytest.raises(LookupError):
        svc.get_type(theirs_type.id)
    with pytest.raises(LookupError):
        svc.get_document(theirs_doc.id)


def test_type_cannot_be_attached_to_another_project(svc, db):
    theirs = _project(db, owner_id=OTHER, name="别人的项目")

    with pytest.raises(LookupError):
        svc.create_type(KnowledgeTypeCreate(name="挂不过去", project_id=theirs.id))
    assert db.scalar(select(KnowledgeType).where(KnowledgeType.name == "挂不过去")) is None


def test_deleting_foreign_document_leaves_row(svc, db):
    theirs = _document(db, owner_id=OTHER)

    with pytest.raises(LookupError):
        svc.delete_document(theirs.id)
    assert db.get(KbDocument, theirs.id) is not None


def test_deleting_foreign_type_leaves_row(svc, db):
    theirs_project = _project(db, owner_id=OTHER, name="别人的项目")
    theirs_type = _type(db, theirs_project.id, owner_id=OTHER)

    with pytest.raises(LookupError):
        svc.delete_type(theirs_type.id)
    assert db.get(KnowledgeType, theirs_type.id) is not None


def test_document_chunk_delete_filters_by_owner(svc, db):
    doc = _document(db)
    mine = _chunk(db, doc, content="我的切片")
    # 同一个 doc_id 上挂着别人的切片（脏数据），删除只能带走自己那份。
    theirs = _chunk(db, doc, owner_id=OTHER, seq=9, content="别人的切片")
    mine_id, theirs_id, doc_id = mine.id, theirs.id, doc.id

    svc.delete_document(doc_id)

    assert db.get(KbChunk, mine_id) is None
    assert db.get(KbChunk, theirs_id) is not None


# ---- 项目与类型：CRUD、计数与级联 -------------------------------------------


def test_project_counts_join_through_types(svc, db):
    project = _project(db, name="计数项目")
    first = _type(db, project.id, name="类型一")
    _type(db, project.id, name="类型二")
    _document(db, type_id=first.id)
    _document(db, type_id=first.id)

    view = svc.list_projects()[0]
    assert view.type_count == 2
    # 文档是靠类型才归到项目底下的，这个计数只能 join 出来。
    assert view.document_count == 2


def test_create_project_persists_fields(svc, db):
    item = svc.create_project(KbProjectCreate(name="新项目", description="说明"))
    assert item.owner_id == OWNER
    assert svc.get_project(item.id).description == "说明"


def test_update_project_treats_name_and_description_differently(svc, db):
    project = _project(db, name="原名")
    project.description = "旧说明"
    db.commit()

    svc.update_project(project.id, KbProjectUpdate(name=None, description="新说明"))
    assert db.get(KbProject, project.id).name == "原名"

    # description 显式给了 null 就该清空；name 的 null 表示「不改」。
    svc.update_project(project.id, KbProjectUpdate(description=None))
    assert db.get(KbProject, project.id).description is None


def test_delete_project_cascades_and_returns_doc_ids(svc, db):
    project = _project(db, name="待删项目")
    type_a = _type(db, project.id, name="类型甲")
    type_b = _type(db, project.id, name="类型乙")
    doc_a = _document(db, type_id=type_a.id)
    doc_b = _document(db, type_id=type_b.id)
    chunk = _chunk(db, doc_a)
    # 这条级联走的是 bulk delete，session 里受影响的实例会被一并标记删除，
    # commit 之后再读它们的属性直接抛 ObjectDeletedError，所以先把 id 记下来。
    project_id, a_id, b_id = project.id, type_a.id, type_b.id
    doc_a_id, doc_b_id, chunk_id = doc_a.id, doc_b.id, chunk.id

    doc_ids = svc.delete_project(project_id)

    assert sorted(doc_ids) == sorted([doc_a_id, doc_b_id])
    assert db.get(KbProject, project_id) is None
    assert db.get(KnowledgeType, a_id) is None
    assert db.get(KnowledgeType, b_id) is None
    assert db.get(KbDocument, doc_a_id) is None
    assert db.get(KbChunk, chunk_id) is None


def test_delete_project_missing_raises(svc, db):
    with pytest.raises(LookupError):
        svc.delete_project(9999)


def test_type_listing_counts_documents(svc, db):
    project = _project(db, name="类型计数")
    with_docs = _type(db, project.id, name="有文档")
    empty = _type(db, project.id, name="空的")
    _document(db, type_id=with_docs.id)

    counts = {t.name: t.document_count for t in svc.list_types(project.id)}
    assert counts == {with_docs.name: 1, empty.name: 0}


def test_update_type_name_and_description(svc, db):
    project = _project(db, name="类型更新")
    item = _type(db, project.id, name="旧名")

    svc.update_type(item.id, KnowledgeTypeUpdate(name="新名"))
    assert db.get(KnowledgeType, item.id).name == "新名"


def test_delete_type_returns_doc_ids_and_removes_chunks(svc, db):
    project = _project(db, name="类型删除")
    item = _type(db, project.id, name="要删的类型")
    doc = _document(db, type_id=item.id)
    chunk = _chunk(db, doc)
    keeper = _document(db, type_id=_type(db, _project(db, name="另一个").id).id)
    type_id, doc_id, chunk_id, keeper_id = item.id, doc.id, chunk.id, keeper.id

    doc_ids = svc.delete_type(type_id)

    assert doc_ids == [doc_id]
    assert db.get(KnowledgeType, type_id) is None
    assert db.get(KbDocument, doc_id) is None
    assert db.get(KbChunk, chunk_id) is None
    assert db.get(KbDocument, keeper_id) is not None


def test_type_ids_of_project_is_the_retrieval_scope(svc, db):
    project = _project(db, name="范围项目")
    other = _project(db, name="另一个范围项目")
    first = _type(db, project.id, name="范围甲")
    second = _type(db, project.id, name="范围乙")
    _type(db, other.id, owner_id=OWNER, name="范围丙")

    assert sorted(svc.type_ids_of_project(project.id)) == sorted([first.id, second.id])


# ---- 文档：列表、分页与删除时的锁回收 --------------------------------------


def test_document_listing_is_owner_scoped(svc, db):
    _document(db)
    _document(db, owner_id=OTHER)

    items, total = svc.list_documents()
    assert total == 1
    assert len(items) == 1
    assert items[0].name == "note.txt"


def test_document_pagination_slices_but_counts_all(svc, db):
    project = _project(db, name="分页项目")
    item = _type(db, project.id, name="分页类型")
    for _ in range(5):
        _document(db, type_id=item.id)

    page_items, total = svc.list_documents(item.id, page=2, page_size=2)
    assert total == 5
    # 新的在前：第二页拿到的是第 3、4 新的那两篇。
    assert [d.id for d in page_items] == sorted([d.id for d in page_items], reverse=True)
    assert len(page_items) == 2

    everything, total_all = svc.list_documents(item.id)
    assert len(everything) == 5 == total_all


def test_create_document_starts_as_pending(svc, db):
    doc = svc.create_document(
        filename="新文档.txt", mime="text/plain", size=12, content="内容", type_id=None
    )
    assert doc.status == DocumentStatus.PENDING.value
    assert doc.scope == "private"


def test_delete_document_releases_its_lock(svc, db):
    doc = _document(db)
    doc_id = doc.id
    _doc_index_lock(OWNER, doc_id)
    assert (OWNER, doc_id) in _doc_index_locks

    svc.delete_document(doc_id)

    assert (OWNER, doc_id) not in _doc_index_locks
    # 删除能走完也是证据：实现里用的是传进来的 doc_id，而不是 commit 之后已经脱钩
    # 的那个实例的属性。
    assert db.get(KbDocument, doc_id) is None


def test_delete_type_releases_every_lock(svc, db):
    project = _project(db, name="锁回收")
    item = _type(db, project.id, name="锁回收类型")
    docs = [_document(db, type_id=item.id) for _ in range(3)]
    for doc in docs:
        _doc_index_lock(OWNER, doc.id)

    svc.delete_type(item.id)

    assert _doc_index_locks == {}


def test_release_missing_lock_key_is_noop():
    _release_doc_lock(OWNER, 12345)
    assert _doc_index_locks == {}


def test_pending_documents_returns_every_status(svc, db):
    """名字叫 pending，实际是全量：重建索引要吃下所有文档。"""
    _document(db, status=DocumentStatus.READY.value)
    _document(db, status=DocumentStatus.ERROR.value)

    assert len(svc.pending_documents()) == 2


def test_set_document_status_truncates_error_and_keeps_chunk_count(svc, db):
    doc = _document(db)

    svc.set_document_status(
        doc, DocumentStatus.ERROR, error="E" * 900, chunk_count=7
    )
    db.refresh(doc)

    assert doc.status == DocumentStatus.ERROR.value
    assert len(doc.error_msg) == 512
    assert doc.chunk_count == 7


# ---- embedding 配置门槛 -----------------------------------------------------


def test_embedding_config_needs_default_verified_and_sized(svc, db):
    assert svc.get_embedding_config() is None

    config = _embedding_model(db, is_default=False)
    assert svc.get_embedding_config() is None

    config.is_default = True
    config.last_test_ok = False
    db.commit()
    assert svc.get_embedding_config() is None

    config.last_test_ok = True
    config.vector_size = None
    db.commit()
    assert svc.get_embedding_config() is None

    config.vector_size = DIM
    db.commit()
    assert svc.get_embedding_config() is config


def test_embedding_config_is_owner_scoped(svc, db):
    _embedding_model(db, owner_id=OTHER)
    assert svc.get_embedding_config() is None
    with pytest.raises(BusinessError) as exc:
        svc.require_embedding_config()
    assert exc.value.code == CODE_EMBEDDING_NOT_READY


def test_require_embedding_config_raises_business_error(svc, db):
    with pytest.raises(BusinessError) as exc:
        svc.require_embedding_config()
    assert exc.value.code == CODE_EMBEDDING_NOT_READY
    assert "向量模型" in exc.value.msg


# ---- 索引状态与模型漂移 -----------------------------------------------------


def test_index_state_view_without_any_state(svc, db):
    view = svc.index_state_view()
    assert view.status == IndexStatus.UNINITIALIZED
    assert view.collection_name == f"kb_u{OWNER}"
    assert view.chunk_count == 0


def test_ensure_index_state_is_idempotent(svc, db):
    config = _embedding_model(db)
    first = svc._ensure_index_state(config)
    second = svc._ensure_index_state(config)

    assert second is first
    assert db.query(KbIndexState).count() == 1
    assert first.embed_model_sig == model_signature(config)
    assert first.status == IndexStatus.UNINITIALIZED


def test_assert_index_usable_rejects_a_different_model(svc, db):
    config = _embedding_model(db)
    state = svc._ensure_index_state(config)
    state.chunk_count = 5
    # 索引建好之后用户在模型配置里换了服务商/模型名，指纹就对不上了。
    config.model_name = "换掉的模型"
    db.commit()

    with pytest.raises(BusinessError) as exc:
        svc.assert_index_usable(config)
    assert exc.value.code == CODE_INDEX_MODEL_MISMATCH


def test_assert_index_usable_allows_empty_index(svc, db):
    """没向量时换模型不算漂移——重建一次就会用新模型建起来。"""
    config = _embedding_model(db)
    state = svc._ensure_index_state(config)
    state.chunk_count = 0
    config.model_name = "换掉的模型"
    db.commit()

    assert svc.assert_index_usable(config) is state


def test_index_state_view_flags_model_drift_only_when_vectors_exist(svc, db):
    config = _embedding_model(db)
    state = svc._ensure_index_state(config)
    # 索引是另一个模型建起来的（换了 base_url），当前配置本身仍是默认项。
    state.embed_model_sig = "https://old.test/v1::bge-m3"
    state.chunk_count = 3
    db.commit()

    assert svc.index_state_view().model_mismatch is True

    # 还没写过向量：换模型不算漂移，重建一次就会用新模型建起来。
    state.chunk_count = 0
    db.commit()
    assert svc.index_state_view().model_mismatch is False


def test_index_state_view_cannot_flag_drift_without_a_usable_model(svc, db):
    """模型配置被删或没通过测试时无从比对，只能不报漂移。"""
    config = _embedding_model(db)
    state = svc._ensure_index_state(config)
    state.embed_model_sig = "https://old.test/v1::别的模型"
    state.chunk_count = 2
    config.last_test_ok = False
    db.commit()

    view = svc.index_state_view()
    assert view.model_mismatch is False
    assert view.chunk_count == 2


def test_set_index_status_is_noop_without_state(svc, db):
    svc.set_index_status(IndexStatus.ERROR, error="没有状态行")
    assert db.query(KbIndexState).count() == 0


def test_refresh_counts_only_counts_ready_documents(svc, db):
    config = _embedding_model(db)
    state = svc._ensure_index_state(config)
    ready = _document(db, status=DocumentStatus.READY.value)
    _document(db, status=DocumentStatus.ERROR.value)
    pending = _document(db)
    _chunk(db, ready)
    _chunk(db, pending, seq=1)
    # 别人的切片不计进来，即便 doc_id 撞上。
    _chunk(db, ready, owner_id=OTHER, seq=2)

    svc._refresh_counts()
    db.refresh(state)

    assert state.doc_count == 1
    assert state.chunk_count == 2


def test_refresh_counts_promotes_uninitialized_to_ready(svc, db):
    config = _embedding_model(db)
    state = svc._ensure_index_state(config)
    _document(db, status=DocumentStatus.READY.value)
    _chunk(db, _document(db, status=DocumentStatus.READY.value))

    svc._refresh_counts()
    db.refresh(state)
    assert state.status == IndexStatus.READY


def test_refresh_counts_does_not_overwrite_error_state(svc, db):
    config = _embedding_model(db)
    state = svc._ensure_index_state(config)
    state.status = IndexStatus.ERROR
    _document(db, status=DocumentStatus.READY.value)
    _chunk(db, _document(db, status=DocumentStatus.READY.value))

    svc._refresh_counts()
    db.refresh(state)
    assert state.status == IndexStatus.ERROR


def test_refresh_counts_without_state_is_silent(svc, db):
    svc._refresh_counts()
    assert db.query(KbIndexState).count() == 0


# ---- 索引构建 ---------------------------------------------------------------


def test_index_document_writes_chunks_then_vectors(svc, db, vs, monkeypatch):
    config = _embedding_model(db)
    doc = _document(db, content="甲。" * 400)  # 超过 700 字符，切成多块
    embed = _stub_embeddings(monkeypatch)

    count = asyncio.run(svc.index_document(doc, config))

    assert count == len(embed.doc_batches[0]) > 0
    db.refresh(doc)
    assert doc.status == DocumentStatus.READY.value
    assert doc.chunk_count == count

    # 写新向量之前先清旧的那批：point id 是重新生成的，旧的不删就成了孤儿。
    assert vs.deleted == [(f"kb_u{OWNER}", OWNER, doc.id)]
    assert vs.ensured == [(f"kb_u{OWNER}", DIM, False)]
    upserted = vs.upserted[0]
    assert upserted["payloads"][0]["owner_id"] == OWNER
    assert upserted["payloads"][0]["doc_id"] == doc.id
    assert set(upserted["payloads"][0]) == {
        "owner_id",
        "doc_id",
        "chunk_id",
        "seq",
        "filename",
        "type_id",
    }
    assert len(upserted["point_ids"]) == count

    state = svc.get_index_state()
    assert state.embed_model_sig == model_signature(config)
    assert state.vector_size == DIM
    assert state.doc_count == 1
    assert state.chunk_count == count


def test_index_document_batches_by_setting(svc, db, vs, monkeypatch):
    """批大小只影响 provider 的往返次数，不影响最终写进去的 chunk 数。"""
    monkeypatch.setattr(kb.settings, "KB_EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(kb, "split_text", lambda text: [f"片段{i}" for i in range(5)])
    config = _embedding_model(db)
    doc = _document(db)
    embed = _stub_embeddings(monkeypatch)

    count = asyncio.run(svc.index_document(doc, config))

    assert count == 5
    assert [len(batch) for batch in embed.doc_batches] == [2, 2, 1]
    assert [len(item["point_ids"]) for item in vs.upserted] == [2, 2, 1]


def test_index_document_batches_default_size_is_one_call(svc, db, vs, monkeypatch):
    monkeypatch.setattr(kb.settings, "KB_EMBED_BATCH_SIZE", 16)
    monkeypatch.setattr(kb, "split_text", lambda text: [f"片段{i}" for i in range(3)])
    config = _embedding_model(db)
    doc = _document(db)
    embed = _stub_embeddings(monkeypatch)

    assert asyncio.run(svc.index_document(doc, config)) == 3
    assert len(embed.doc_batches) == 1 == len(vs.upserted)


def test_index_document_batch_size_of_one_sends_each_chunk_alone(svc, db, vs, monkeypatch):
    monkeypatch.setattr(kb.settings, "KB_EMBED_BATCH_SIZE", 1)
    monkeypatch.setattr(kb, "split_text", lambda text: ["A", "B", "C"])
    config = _embedding_model(db)
    doc = _document(db)
    embed = _stub_embeddings(monkeypatch)

    asyncio.run(svc.index_document(doc, config))

    assert embed.doc_batches == [["A"], ["B"], ["C"]]


def test_index_document_without_text_skips_vector_store(svc, db, vs, monkeypatch):
    config = _embedding_model(db)
    doc = _document(db, content="   \n  ")
    _stub_embeddings(monkeypatch)

    assert asyncio.run(svc.index_document(doc, config)) == 0
    db.refresh(doc)
    assert doc.status == DocumentStatus.READY.value
    assert doc.chunk_count == 0
    assert vs.ensured == [] and vs.upserted == []


def test_index_document_needs_a_probed_vector_size(svc, db, vs, monkeypatch):
    config = _embedding_model(db, vector_size=None)
    doc = _document(db)
    _stub_embeddings(monkeypatch)

    with pytest.raises(BusinessError) as exc:
        asyncio.run(svc.index_document(doc, config))
    assert exc.value.code == CODE_EMBEDDING_NOT_READY
    assert vs.upserted == []


def test_index_document_stops_when_provider_changes_shape(svc, db, vs, monkeypatch):
    """模型偷偷换了输出维度：写进去会毁掉整个 collection，必须在写之前拦下。"""
    config = _embedding_model(db, vector_size=DIM)
    doc = _document(db)
    _stub_embeddings(monkeypatch, dim=DIM * 2)

    with pytest.raises(BusinessError) as exc:
        asyncio.run(svc.index_document(doc, config))
    assert exc.value.code == CODE_INDEX_MODEL_MISMATCH
    assert vs.upserted == []


def test_replace_chunks_replaces_previous_row_set(svc, db):
    doc = _document(db)
    first = svc.replace_chunks(doc, ["A", "B", "C"])
    assert [chunk.seq for chunk in first] == [0, 1, 2]
    assert all(chunk.point_id for chunk in first)

    second = svc.replace_chunks(doc, ["X"])
    assert db.query(KbChunk).filter(KbChunk.doc_id == doc.id).count() == 1
    assert second[0].point_id not in {chunk.point_id for chunk in first}


# ---- 全量重建 ---------------------------------------------------------------


def test_rebuild_recreates_collection_and_indexes_every_doc(svc, db, vs, monkeypatch):
    config = _embedding_model(db)
    doc = _document(db, content="内容一。")
    _document(db, content="内容二。")
    _stub_embeddings(monkeypatch)

    docs, chunks = asyncio.run(svc.rebuild_all(config))

    assert (docs, chunks) == (2, 2)
    assert vs.ensured[0] == (f"kb_u{OWNER}", DIM, True)
    db.refresh(doc)
    assert doc.status == DocumentStatus.READY.value
    assert svc.get_index_state().status == IndexStatus.READY


def test_rebuild_marks_not_yet_done_documents_as_error(svc, db, vs, monkeypatch):
    """collection 已被清空重建：没走完的文档 MySQL 里还是 READY，向量其实没了。"""
    config = _embedding_model(db)
    done = _document(db, content="第一篇。")
    interrupted = _document(db, content="第二篇。")
    # 第一篇吃掉唯一一次成功调用，第二篇起失败。
    _stub_embeddings(monkeypatch, fail_after=1)

    with pytest.raises(RuntimeError):
        asyncio.run(svc.rebuild_all(config))

    db.refresh(done)
    db.refresh(interrupted)
    assert done.status == DocumentStatus.READY.value
    assert interrupted.status == DocumentStatus.ERROR.value
    assert "重建中断" in interrupted.error_msg
    assert svc.get_index_state().status == IndexStatus.ERROR
    assert "provider" in svc.get_index_state().error_msg


def test_rebuild_on_empty_knowledge_base_marks_ready(svc, db, vs, monkeypatch):
    config = _embedding_model(db)
    _stub_embeddings(monkeypatch)

    assert asyncio.run(svc.rebuild_all(config)) == (0, 0)
    assert svc.get_index_state().status == IndexStatus.READY


# ---- 后台索引任务与启动收尾 -------------------------------------------------


def test_index_task_reindexes_a_pending_document(maker, db, vs, monkeypatch):
    _embedding_model(db)
    with maker() as session:
        doc = _document(session, content="正文内容。")
        doc_id = doc.id

    _stub_embeddings(monkeypatch)
    asyncio.run(kb.index_document_task(OWNER, doc_id))

    with maker() as session:
        assert session.get(KbDocument, doc_id).status == DocumentStatus.READY.value


def test_index_task_forgets_a_document_that_is_gone(maker, db, vs, monkeypatch):
    """后台任务跑起来时那篇文档可能已被删掉：静默返回，别抛到 worker 里。"""
    _stub_embeddings(monkeypatch)
    asyncio.run(kb.index_document_task(OWNER, 4242))

    with maker() as session:
        assert session.query(KbDocument).count() == 0


def test_index_task_records_business_rejection_as_error(maker, db, vs, monkeypatch):
    with maker() as session:
        doc = _document(session, content="正文。")
        doc_id = doc.id

    _stub_embeddings(monkeypatch)
    asyncio.run(kb.index_document_task(OWNER, doc_id))

    with maker() as session:
        row = session.get(KbDocument, doc_id)
        assert row.status == DocumentStatus.ERROR.value
        assert "向量模型" in row.error_msg


def test_index_task_records_unexpected_failure_as_error(maker, db, vs, monkeypatch):
    _embedding_model(db)
    with maker() as session:
        doc = _document(session, content="正文。")
        doc_id = doc.id

    _stub_embeddings(monkeypatch, fail_after=0)
    asyncio.run(kb.index_document_task(OWNER, doc_id))

    with maker() as session:
        row = session.get(KbDocument, doc_id)
        assert row.status == DocumentStatus.ERROR.value
        assert "provider" in row.error_msg


def test_index_task_waits_while_the_document_lock_is_held(maker, db, vs, monkeypatch):
    """同一篇文档的索引互斥：上传排队、手动重建、全量重建撞在一起会互相覆盖。"""
    _embedding_model(db)
    with maker() as session:
        doc = _document(session, content="正文内容。")
        doc_id = doc.id

    fake = _stub_embeddings(monkeypatch)

    async def drive():
        lock = _doc_index_lock(OWNER, doc_id)
        await lock.acquire()
        task = asyncio.create_task(kb.index_document_task(OWNER, doc_id))
        await asyncio.sleep(0.05)
        blocked = fake.doc_batches == []
        lock.release()
        await task
        return blocked

    assert asyncio.run(drive()) is True
    assert fake.doc_batches != []


def test_reset_stale_indexing_only_touches_indexing_rows(db):
    stuck = _document(db, status=DocumentStatus.INDEXING.value)
    ready = _document(db, status=DocumentStatus.READY.value)
    pending = _document(db, status=DocumentStatus.PENDING.value)

    assert reset_stale_indexing(db) == 1
    db.refresh(stuck)
    assert stuck.status == DocumentStatus.ERROR.value
    assert "重新索引" in stuck.error_msg
    db.refresh(ready)
    db.refresh(pending)
    assert ready.status == DocumentStatus.READY.value
    assert pending.status == DocumentStatus.PENDING.value


def test_reset_stale_indexing_is_not_owner_scoped(db):
    """启动收尾跨所有用户：worker 死掉时卡住的是上一任进程留下的全部行。"""
    _document(db, owner_id=OTHER, status=DocumentStatus.INDEXING.value)
    assert reset_stale_indexing(db) == 1


def test_reset_stale_indexing_without_anything_stuck(db):
    assert reset_stale_indexing(db) == 0


# ---- 检索范围与结果回读 -----------------------------------------------------


def test_search_requires_embedding_config(svc, db, vs):
    with pytest.raises(BusinessError) as exc:
        asyncio.run(svc.search("问题"))
    assert exc.value.code == CODE_EMBEDDING_NOT_READY


def test_search_refuses_a_stale_model_index(svc, db, vs, monkeypatch):
    config = _embedding_model(db)
    state = svc._ensure_index_state(config)
    state.chunk_count = 4
    config.model_name = "换掉的模型"
    db.commit()
    # 桩替掉 provider：这道闸门必须在任何出站调用之前拦下，一次请求都不该发出去。
    embed = _stub_embeddings(monkeypatch)

    with pytest.raises(BusinessError) as exc:
        asyncio.run(svc.search("问题"))
    assert exc.value.code == CODE_INDEX_MODEL_MISMATCH
    assert embed.queries == [] and vs.queries == []


def test_search_without_project_does_not_filter(svc, db, vs, monkeypatch):
    _embedding_model(db)
    _stub_embeddings(monkeypatch)
    vs.points = []

    asyncio.run(svc.search("问题"))

    query = vs.queries[0]
    assert query.type_ids is None
    assert query.owner_id == OWNER
    assert query.top_k == 8
    assert query.vector == [0.1] * DIM


def test_search_resolves_project_into_type_ids(svc, db, vs, monkeypatch):
    _embedding_model(db)
    project = _project(db, name="检索项目")
    included = _type(db, project.id, name="在项目里")
    outside = _type(db, _project(db, name="另一个项目").id, name="在项目外")
    _stub_embeddings(monkeypatch)

    asyncio.run(svc.search("问题", project_id=project.id))

    assert vs.queries[0].type_ids == [included.id]

    # 显式给的 type_ids 与项目范围取交集，越界的那个直接被剔掉。
    asyncio.run(svc.search("问题", project_id=project.id, type_ids=[included.id, outside.id]))
    assert vs.queries[-1].type_ids == [included.id]

    # 交集为空：短路返回，绝不带一个空 MatchAny 去搜（那等于匹配全部）。
    results = asyncio.run(svc.search("问题", project_id=project.id, type_ids=[outside.id]))
    assert results == []
    assert len(vs.queries) == 2


def test_search_deduplicates_explicit_type_ids(svc, db, vs, monkeypatch):
    _embedding_model(db)
    item = _type(db, _project(db, name="去重项目").id, name="去重类型")
    _stub_embeddings(monkeypatch)

    asyncio.run(svc.search("问题", type_ids=[item.id, item.id]))

    assert vs.queries[0].type_ids == [item.id]


def test_search_threshold_defaults_to_settings_then_overrides(svc, db, vs, monkeypatch):
    _embedding_model(db)
    monkeypatch.setattr(kb.settings, "KB_SCORE_THRESHOLD", 0.42)
    _stub_embeddings(monkeypatch)

    asyncio.run(svc.search("问题"))
    assert vs.queries[0].score_threshold == 0.42

    asyncio.run(svc.search("问题", score_threshold=0.05))
    assert vs.queries[-1].score_threshold == 0.05


def test_search_reads_chunk_text_back_from_mysql(svc, db, vs, monkeypatch):
    """命中内容来自 MySQL 而不是 payload：事实来源只有一份，payload 保持精简。"""
    _embedding_model(db)
    doc = _document(db, filename="手册.txt")
    chunk = _chunk(db, doc, seq=3, content="真正的切片正文")
    _stub_embeddings(monkeypatch)
    vs.points = [_point(chunk.id, 0.87, filename="手册.txt")]

    hits = asyncio.run(svc.search("问题"))

    assert len(hits) == 1
    hit = hits[0]
    assert (hit.chunk_id, hit.doc_id, hit.seq, hit.score) == (chunk.id, doc.id, 3, 0.87)
    assert hit.content == "真正的切片正文"
    assert hit.filename == "手册.txt"


def test_search_skips_vectors_whose_row_is_gone(svc, db, vs, monkeypatch):
    """查询途中文档被删：孤儿向量跳过，而不是整次检索失败。"""
    _embedding_model(db)
    doc = _document(db)
    live = _chunk(db, doc, content="还在的切片")
    _stub_embeddings(monkeypatch)
    vs.points = [
        _point(live.id, 0.9),
        _point(99999, 0.8),
        SimpleNamespace(payload={}, score=0.7),
    ]

    hits = asyncio.run(svc.search("问题"))

    assert [h.chunk_id for h in hits] == [live.id]


def test_search_returns_empty_when_vector_store_has_nothing(svc, db, vs, monkeypatch):
    _embedding_model(db)
    _stub_embeddings(monkeypatch)
    vs.points = []

    assert asyncio.run(svc.search("问题")) == []


def test_search_lets_upstream_failure_through(svc, db, vs, monkeypatch):
    _embedding_model(db)
    _stub_embeddings(monkeypatch, fail_after=0)

    with pytest.raises(RuntimeError):
        asyncio.run(svc.search("问题"))

    assert vs.queries == []


def test_search_propagates_vector_store_errors(svc, db, vs, monkeypatch):
    _embedding_model(db)
    _stub_embeddings(monkeypatch)

    async def boom(*args, **kwargs):
        raise vectorstore.VectorStoreError("qdrant down")

    monkeypatch.setattr(vectorstore, "search", boom)
    with pytest.raises(vectorstore.VectorStoreError):
        asyncio.run(svc.search("问题"))


# ---- 接口层 -----------------------------------------------------------------


def _ready_project(db):
    """一个配好向量模型、带项目与类型的调用者视角。"""
    project = _project(db, name="接口项目")
    item = _type(db, project.id, name="接口类型")
    config = _embedding_model(db)
    return project, item, config


def test_api_project_and_type_crud(client, db):
    created = client.post("/api/knowledge/projects", json={"name": "新项目"}).json()["data"]
    project_id = created["id"]
    assert created["type_count"] == 0

    type_created = client.post(
        "/api/knowledge/types", json={"name": "新类型", "project_id": project_id}
    ).json()["data"]
    assert type_created["project_id"] == project_id

    listed = client.get("/api/knowledge/projects").json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["type_count"] == 1

    types = client.get(f"/api/knowledge/types?project_id={project_id}").json()["data"]
    assert [t["name"] for t in types["items"]] == ["新类型"]

    updated = client.put(
        f"/api/knowledge/types/{type_created['id']}", json={"name": "改名"}
    ).json()["data"]
    assert updated["name"] == "改名"
    assert updated["document_count"] == 0


def test_api_missing_project_and_type_return_404(client, db):
    _ready_project(db)
    assert client.get("/api/knowledge/types?project_id=9999").status_code == 404
    assert client.put("/api/knowledge/projects/9999", json={"name": "x"}).status_code == 404
    assert client.post("/api/knowledge/types", json={"name": "t", "project_id": 9999}).status_code == 404
    assert client.delete("/api/knowledge/types/9999").status_code == 404
    assert client.get("/api/knowledge/types/9999/documents").status_code == 404


def test_api_search_endpoint_returns_hits(client, db, vs, monkeypatch):
    project, item, _ = _ready_project(db)
    doc = _document(db, type_id=item.id)
    chunk = _chunk(db, doc, content="命中的正文")
    _stub_embeddings(monkeypatch)
    vs.points = [_point(chunk.id, 0.91, filename="note.txt", type_id=item.id)]

    body = client.post(
        "/api/knowledge/search",
        json={"query": "问题", "top_k": 3, "project_id": project.id},
    ).json()

    assert body["code"] == 0
    assert body["data"]["total"] == 1
    assert body["data"]["hits"][0]["content"] == "命中的正文"
    assert vs.queries[0].top_k == 3
    assert vs.queries[0].type_ids == [item.id]


def test_api_search_without_model_returns_guidance_code(client, db, vs):
    body = client.post("/api/knowledge/search", json={"query": "问题"}).json()
    assert body["code"] == CODE_EMBEDDING_NOT_READY
    assert vs.queries == []


def test_api_invalid_search_body_keeps_the_envelope(client, db):
    _ready_project(db)
    res = client.post("/api/knowledge/search", json={"query": ""})
    assert res.status_code == 422
    assert res.json()["code"] == 422


def test_api_index_state_is_not_gated(client, db):
    body = client.get("/api/knowledge/index/state").json()
    assert body["code"] == 0
    assert body["data"]["status"] == IndexStatus.UNINITIALIZED
    assert body["data"]["collection_name"] == f"kb_u{OWNER}"


def test_api_upload_stores_document_and_queues_indexing(client, db, queued, monkeypatch):
    _, item, _ = _ready_project(db)
    _stub_embeddings(monkeypatch)

    res = client.post(
        f"/api/knowledge/types/{item.id}/documents",
        files={"file": ("手册.txt", "正文内容。".encode(), "text/plain")},
    ).json()

    assert res["code"] == 0
    assert res["data"]["status"] == DocumentStatus.PENDING.value
    assert res["data"]["name"] == "手册.txt"
    doc_id = res["data"]["id"]
    assert db.get(KbDocument, doc_id).content == "正文内容。"
    # 切分与 embedding 全部推到后台：几百 KB 文本是几十次 provider 往返，
    # 同步做会顶穿 SPA 的 30s 超时和 worker 超时。
    assert queued == [("index", OWNER, doc_id)]


def test_api_upload_rejects_unsupported_extension(client, db, monkeypatch):
    _, item, _ = _ready_project(db)

    res = client.post(
        f"/api/knowledge/types/{item.id}/documents",
        files={"file": ("slide.pptx", b"\x00\x01", "application/vnd.ms-powerpoint")},
    ).json()
    assert res["code"] == -1
    assert "暂不支持" in res["message"]
    assert db.query(KbDocument).count() == 0


def test_api_upload_rejects_file_without_text(client, db, monkeypatch):
    _, item, _ = _ready_project(db)

    res = client.post(
        f"/api/knowledge/types/{item.id}/documents",
        files={"file": ("empty.txt", b"   \n\n  ", "text/plain")},
    ).json()
    assert res["code"] == -1
    assert "无法解析出文本" in res["message"]
    assert db.query(KbDocument).count() == 0


def test_api_upload_rejects_by_content_length_before_reading(client, db, monkeypatch):
    _, item, _ = _ready_project(db)
    monkeypatch.setattr(knowledge_router.settings, "KB_MAX_UPLOAD_BYTES", 1)

    res = client.post(
        f"/api/knowledge/types/{item.id}/documents",
        files={"file": ("big.txt", b"x" * (1024 * 1024 * 2), "text/plain")},
    ).json()
    assert res["code"] == -1
    assert "文件过大" in res["message"]
    assert db.query(KbDocument).count() == 0


def test_api_upload_stops_reading_once_over_limit(client, db, monkeypatch):
    """Content-Length 靠表单余量放行了，读取阶段还是要按实际字节数刹车。"""
    _, item, _ = _ready_project(db)
    monkeypatch.setattr(knowledge_router.settings, "KB_MAX_UPLOAD_BYTES", 2048)

    res = client.post(
        f"/api/knowledge/types/{item.id}/documents",
        files={"file": ("big.txt", b"x" * 6000, "text/plain")},
    ).json()
    assert res["code"] == -1
    assert "文件过大" in res["message"]
    assert db.query(KbDocument).count() == 0


def test_api_upload_without_model_returns_guidance_code(client, db, queued):
    project = _project(db, name="未配置模型")
    item = _type(db, project.id, name="类型")

    res = client.post(
        f"/api/knowledge/types/{item.id}/documents",
        files={"file": ("note.txt", "内容".encode(), "text/plain")},
    )
    assert res.json()["code"] == CODE_EMBEDDING_NOT_READY
    assert queued == []


def test_api_document_list_pagination(client, db):
    _, item, _ = _ready_project(db)
    for _ in range(5):
        _document(db, type_id=item.id)

    everything = client.get(f"/api/knowledge/types/{item.id}/documents").json()["data"]
    assert everything["total"] == 5
    assert len(everything["items"]) == 5

    page = client.get(
        f"/api/knowledge/documents?type_id={item.id}&page=2&page_size=2"
    ).json()["data"]
    assert page["total"] == 5
    assert len(page["items"]) == 2

    res = client.get(f"/api/knowledge/documents?page=1&page_size=201")
    assert res.status_code == 422


def test_api_reindex_single_document(client, db, queued):
    _, item, _ = _ready_project(db)
    doc = _document(db, type_id=item.id, status=DocumentStatus.ERROR.value, content="内容。")

    body = client.post(f"/api/knowledge/documents/{doc.id}/reindex").json()
    assert body["code"] == 0
    assert body["data"]["status"] == DocumentStatus.PENDING.value
    assert queued == [("index", OWNER, doc.id)]

    assert client.post("/api/knowledge/documents/9999/reindex").status_code == 404


def test_api_batch_reindex_skips_rows_that_are_not_ours(client, db, queued):
    _, item, _ = _ready_project(db)
    mine_a = _document(db, type_id=item.id)
    mine_b = _document(db, type_id=item.id)
    theirs = _document(db, owner_id=OTHER)

    body = client.post(
        "/api/knowledge/documents/batch-reindex",
        json={"ids": [mine_a.id, mine_b.id, theirs.id, 9999]},
    ).json()

    assert body["data"] == {"requested": 4, "done": 2}
    assert sorted(queued) == sorted(
        [("index", OWNER, mine_a.id), ("index", OWNER, mine_b.id)]
    )
    db.refresh(theirs)
    assert theirs.status == DocumentStatus.PENDING.value


def test_api_batch_delete_counts_only_what_it_removed(client, db, vs):
    _, item, _ = _ready_project(db)
    mine_a = _document(db, type_id=item.id)
    mine_b = _document(db, type_id=item.id)
    theirs = _document(db, owner_id=OTHER)

    body = client.post(
        "/api/knowledge/documents/batch-delete",
        json={"ids": [mine_a.id, theirs.id, 9999]},
    ).json()

    assert body["data"] == {"requested": 3, "done": 1}
    assert db.get(KbDocument, mine_a.id) is None
    assert db.get(KbDocument, theirs.id) is not None
    assert vs.deleted == [(f"kb_u{OWNER}", OWNER, mine_a.id)]


def test_api_delete_document_still_succeeds_when_qdrant_is_down(client, db, vs):
    """行已经删掉了，向量抖动不该把成功的删除翻成错误。"""
    doc = _document(db)
    vs.fail_delete = vectorstore.VectorStoreError("qdrant down")

    res = client.delete(f"/api/knowledge/documents/{doc.id}")
    assert res.status_code == 200
    assert res.json()["message"] == "deleted"
    assert db.get(KbDocument, doc.id) is None


def test_api_document_delete_releases_lock(client, db, vs):
    doc = _document(db)
    _doc_index_lock(OWNER, doc.id)

    client.delete(f"/api/knowledge/documents/{doc.id}")

    assert _doc_index_locks == {}


def test_api_delete_type_and_project_purge_vectors(client, db, vs):
    project, item, _ = _ready_project(db)
    _document(db, type_id=item.id)
    _document(db, type_id=item.id)

    client.delete(f"/api/knowledge/types/{item.id}")
    assert len(vs.deleted) == 2
    assert {entry[0] for entry in vs.deleted} == {f"kb_u{OWNER}"}

    vs.deleted.clear()
    other_project = _project(db, name="级联项目")
    other_type = _type(db, other_project.id, name="级联类型")
    doc = _document(db, type_id=other_type.id)

    client.delete(f"/api/knowledge/projects/{other_project.id}")
    assert vs.deleted == [(f"kb_u{OWNER}", OWNER, doc.id)]
    assert db.get(KbDocument, doc.id) is None
    assert client.delete("/api/knowledge/projects/9999").status_code == 404


def test_api_rebuild_starts_only_when_documents_exist(client, db, queued):
    _ready_project(db)

    empty = client.post("/api/knowledge/index/rebuild").json()
    assert empty["data"]["started"] is False
    assert queued == []

    _document(db)
    started = client.post("/api/knowledge/index/rebuild").json()
    assert started["data"] == {
        "started": True,
        "message": started["data"]["message"],
        "doc_count": 1,
    }
    assert queued == [("rebuild", OWNER)]


def test_api_rebuild_without_model_returns_guidance_code(client, db, queued):
    _project(db, name="重建未配置")
    assert client.post("/api/knowledge/index/rebuild").json()["code"] == CODE_EMBEDDING_NOT_READY
    assert queued == []
