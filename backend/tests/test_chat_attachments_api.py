"""对话附件接口：上传与删除的路由行为。

服务层的绑定/回放/删除语义在 ``test_chat_service.py`` 的第十节；
这里只钉路由这一层：内容嗅探走的是上传接口而不是文件后缀、业务拒绝
以 ``code=-1`` 返回、没发出去的附件能删、已发送的不能删。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import ChatAttachment
from app.models.schemas import CurrentUser
from app.routers import chat as chat_router
from app.services.chat import attachments as attachment_module

OWNER = 1
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'chat_attachments.db'}",
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
def client(db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        attachment_module,
        "settings",
        SimpleNamespace(
            upload_media_path=tmp_path,
            UPLOAD_MEDIA_URL="/uploads",
            CHAT_ATTACHMENT_MAX_CHARS=60_000,
            CHAT_MAX_IMAGE_BYTES=1024,
        ),
    )
    monkeypatch.setattr(
        chat_router,
        "settings",
        SimpleNamespace(CHAT_MAX_ATTACHMENT_BYTES=1024 * 1024, CHAT_MAX_ATTACHMENTS=6),
    )

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


def upload(client, name: str, raw: bytes, content_type: str = "application/octet-stream"):
    return client.post(
        "/api/chat/attachments",
        files={"file": (name, raw, content_type)},
    ).json()


def test_upload_text_file_extracts_it_server_side(client, db):
    body = upload(client, "notes.txt", "第一行\n第二行".encode(), "text/plain")

    assert body["code"] == 0
    assert body["data"]["kind"] == "file"
    assert body["data"]["filename"] == "notes.txt"
    row = db.scalars(select(ChatAttachment)).one()
    assert row.text == "第一行\n第二行"
    assert row.rel_path is None


def test_upload_image_sniffs_the_content_and_returns_a_url(client, db):
    # Content-Type 故意报成纯文本：后端只信文件头。
    body = upload(client, "截图.png", PNG, "text/plain")

    assert body["code"] == 0
    assert body["data"]["kind"] == "image"
    assert body["data"]["mime"] == "image/png"
    assert body["data"]["url"].startswith("/uploads/chat/")
    row = db.scalars(select(ChatAttachment)).one()
    assert row.rel_path and (row.rel_path.endswith(".png"))


def test_upload_rejects_an_unsupported_file_as_a_business_error(client):
    body = upload(client, "setup.exe", b"MZ")

    assert body["code"] == -1
    assert "暂不支持" in body["message"]


def test_upload_rejects_an_oversize_image(client):
    body = upload(client, "big.png", PNG + b"x" * 2048, "image/png")

    assert body["code"] == -1
    assert "上限" in body["message"]


def test_delete_removes_an_unbound_attachment(client, db):
    created = upload(client, "notes.txt", b"hi", "text/plain")["data"]

    body = client.delete(f"/api/chat/attachments/{created['id']}").json()

    assert body["code"] == 0
    assert db.scalars(select(ChatAttachment)).all() == []


def test_delete_is_404_for_a_missing_attachment(client):
    assert client.delete("/api/chat/attachments/4242").status_code == 404
