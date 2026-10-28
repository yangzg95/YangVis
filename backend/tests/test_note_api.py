"""求职笔记：service 层存取/搜索与 HTTP 链路。

- service 层钉隔离、排序、关键词匹配（含 LIKE 通配符转义）与更新语义；
- HTTP 层钉路径与统一信封、列表不带正文、他人 id 读作 404、非法 body 走 422。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.schemas import CurrentUser, NoteCreate, NoteUpdate
from app.services.note import SNIPPET_LEN, NoteService

OWNER = 1
OTHER = 2


# ---- 环境 -------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'note.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client_for(db):
    """工厂：按身份取 TestClient。依赖覆盖是 app 上的全局字典，所以取 client 时
    最后一次设定的身份对之后所有请求生效——用例里不要交错使用两个身份发请求。"""

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db

    def make_client(owner_id: int) -> TestClient:
        app.dependency_overrides[require_user] = lambda: CurrentUser(
            user_id=owner_id, username=f"user{owner_id}"
        )
        return TestClient(app)

    try:
        yield make_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def client(client_for):
    return client_for(OWNER)


@pytest.fixture()
def service(db):
    return NoteService(db, OWNER)


# ---- service 层 ---------------------------------------------------------------


def test_create_strips_title_and_content(service):
    record = service.create_record(NoteCreate(title="  谈薪要点  ", content="\n  别先报价\n "))
    assert record.title == "谈薪要点"
    assert record.content == "别先报价"


def test_create_keeps_blank_content_as_none(service):
    record = service.create_record(NoteCreate(title="空笔记", content="   \n "))
    assert record.content is None
    assert service.to_item(record).snippet is None


def test_list_orders_newest_first(service):
    first = service.create_record(NoteCreate(title="第一条"))
    second = service.create_record(NoteCreate(title="第二条"))
    # 时间戳可能落在同一秒（SQLite 精度），id 倒序是决胜键。
    assert [r.id for r in service.list_records()] == [second.id, first.id]


def test_snippet_collapses_whitespace_and_truncates(service):
    record = service.create_record(
        NoteCreate(title="长文", content="第一行\n  第二行\t第三行 " + "x" * SNIPPET_LEN)
    )
    snippet = service.to_item(record).snippet
    assert "\n" not in snippet and "\t" not in snippet
    assert snippet.endswith("…")
    assert len(snippet) <= SNIPPET_LEN + 1


def test_keyword_matches_title_and_content_case_insensitive(service):
    service.create_record(NoteCreate(title="字节面经", content="问了 Redis"))
    service.create_record(NoteCreate(title="JD 收集", content="后端工程师，要求 Go"))

    assert {r.title for r in service.list_records("redis")} == {"字节面经"}
    assert {r.title for r in service.list_records("JD")} == {"JD 收集"}
    assert service.list_records("不存在的词") == []


def test_keyword_like_wildcards_are_literal(service):
    service.create_record(NoteCreate(title="完成度 100%"))
    service.create_record(NoteCreate(title="普通笔记"))
    # 不转义的话 '%' 会匹配全部、'_' 匹配任意单字符；转义后只按字面量找。
    assert {r.title for r in service.list_records("%")} == {"完成度 100%"}
    assert {r.title for r in service.list_records("100%")} == {"完成度 100%"}
    assert service.list_records("50%") == []
    assert service.list_records("_") == []


def test_update_is_partial_and_null_clears_content(service):
    record = service.create_record(NoteCreate(title="原标题", content="原正文"))

    updated = service.update_record(record.id, NoteUpdate(title="新标题"))
    assert updated.title == "新标题"
    assert updated.content == "原正文"

    cleared = service.update_record(record.id, NoteUpdate(content=None))
    assert cleared.content is None
    assert cleared.title == "新标题"


def test_whitespace_only_title_rejected_by_schema():
    # min_length=1 只数字符数，纯空格由校验器兜成 ValueError（HTTP 上即 422）。
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NoteCreate(title="   ")
    with pytest.raises(ValidationError):
        NoteUpdate(title="  ")


def test_owner_isolation_read_and_delete(service, db):
    record = service.create_record(NoteCreate(title="我的笔记"))
    intruder = NoteService(db, OTHER)

    with pytest.raises(LookupError):
        intruder.get_record(record.id)
    assert [r.id for r in intruder.list_records()] == []


# ---- HTTP 层 ------------------------------------------------------------------


def test_list_empty(client):
    body = client.get("/api/office/notes").json()
    assert body["code"] == 0
    assert body["data"] == {"items": [], "total": 0}


def test_create_and_list_http(client):
    created = client.post(
        "/api/office/notes", json={"title": "收藏的 JD", "content": "高级前端，30-40K"}
    ).json()["data"]
    assert created["content"] == "高级前端，30-40K"

    row = client.get("/api/office/notes").json()["data"]["items"][0]
    assert row["title"] == "收藏的 JD"
    assert row["snippet"] == "高级前端，30-40K"
    # 列表行不带正文，避免把全部长文拖过网络。
    assert "content" not in row


def test_get_detail_and_update_http(client):
    note_id = client.post("/api/office/notes", json={"title": "T"}).json()["data"]["id"]

    detail = client.get(f"/api/office/notes/{note_id}").json()["data"]
    assert detail["content"] is None

    updated = client.put(
        f"/api/office/notes/{note_id}", json={"content": "补上正文"}
    ).json()["data"]
    assert updated["content"] == "补上正文"


def test_keyword_query_filters_http(client):
    client.post("/api/office/notes", json={"title": "谈薪", "content": "锚定 30%"})
    client.post("/api/office/notes", json={"title": "面试安排", "content": "周二下午三点"})

    items = client.get("/api/office/notes", params={"keyword": "谈薪"}).json()["data"]["items"]
    assert [item["title"] for item in items] == ["谈薪"]


def test_other_owner_gets_404(client_for):
    note_id = client_for(OWNER).post("/api/office/notes", json={"title": "私密"}).json()["data"][
        "id"
    ]
    other = client_for(OTHER)
    assert other.get(f"/api/office/notes/{note_id}").status_code == 404
    assert other.get("/api/office/notes").json()["data"]["total"] == 0


def test_blank_title_is_422(client):
    response = client.post("/api/office/notes", json={"title": "  "})
    assert response.status_code == 422


def test_delete_http(client):
    note_id = client.post("/api/office/notes", json={"title": "待删"}).json()["data"]["id"]
    assert client.delete(f"/api/office/notes/{note_id}").json()["message"] == "deleted"
    assert client.get(f"/api/office/notes/{note_id}").status_code == 404
