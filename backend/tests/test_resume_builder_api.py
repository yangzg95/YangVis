"""在线简历「保存到简历」接口：只钉路由这一层。

同步的新建/更新/失效语义在 ``test_resume_builder.py`` 第五节；这里验证
HTTP 链路：路径与信封、空简历以 ``code=-1`` 业务拒绝、文档不存在 404。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import Resume
from app.models.schemas import CurrentUser

OWNER = 1


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'resume_sync_api.db'}",
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
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=OWNER, username="tester"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def create_doc(client, title: str = "我的简历", doc: dict | None = None) -> int:
    payload = {"title": title}
    if doc is not None:
        # 创建后再 PUT 内容：POST 只收标题。
        rid = client.post("/api/office/resume-docs", json=payload).json()["data"]["id"]
        client.put(f"/api/office/resume-docs/{rid}", json={"doc": doc})
        return rid
    return client.post("/api/office/resume-docs", json=payload).json()["data"]["id"]


def test_save_to_resume_creates_library_row(client, db):
    doc_id = create_doc(
        client,
        doc={
            "profile": {"name": "张三", "job_target": "后端工程师", "phone": "13800000000"},
            "skills_html": "<ul><li>Java、Spring</li></ul>",
        },
    )

    response = client.post(f"/api/office/resume-docs/{doc_id}/save-to-resume")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "已同步到简历库"
    item = body["data"]
    assert item["filename"] == "我的简历.txt"
    assert item["mime"] == "text/plain"

    row = db.query(Resume).one()
    assert row.owner_id == OWNER
    assert "张三" in row.content and "Java" in row.content

    # 再同步一次：更新同一行，信封消息换成更新口径。
    second = client.post(f"/api/office/resume-docs/{doc_id}/save-to-resume").json()
    assert second["code"] == 0
    assert second["message"] == "简历库中的内容已更新"
    assert second["data"]["id"] == item["id"]
    assert db.query(Resume).count() == 1


def test_save_to_resume_rejects_blank_doc(client):
    doc_id = create_doc(client)
    body = client.post(f"/api/office/resume-docs/{doc_id}/save-to-resume").json()
    assert body["code"] == -1
    assert "先填写" in body["message"]


def test_save_to_resume_unknown_doc_is_404(client):
    response = client.post("/api/office/resume-docs/999/save-to-resume")
    assert response.status_code == 404
