"""创作模块 HTTP 链路：统一信封、列表/详情字段分离、422、跨账号 404、导出响应头。

service 层的清洗/对账细节在 test_writing.py 钉过，这里只钉 Web 边界：
路径、信封结构、状态码与响应头，以及业务拒绝走 ``code=-1`` 而不是抛 500。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.schemas import CurrentUser

OWNER = 1
OTHER = 2


# ---- 环境 -------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'writing_api.db'}",
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
    """同 test_note_api：依赖覆盖是 app 全局字典，用例里不要交错身份发请求。"""

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


def _create(client: TestClient, kind: str, title: str) -> dict:
    return client.post(
        "/api/studio/works", json={"kind": kind, "title": title}
    ).json()["data"]


# ---- 列表与详情 ---------------------------------------------------------------


def test_list_empty_envelope(client):
    body = client.get("/api/studio/works").json()
    assert body["code"] == 0
    assert body["data"] == {"items": [], "total": 0}


def test_create_and_kind_filter_and_list_shape(client):
    novel = _create(client, "novel", "星河彼岸")
    article = _create(client, "article", "增长手记")

    rows = client.get("/api/studio/works").json()["data"]
    assert rows["total"] == 2

    novel_rows = client.get("/api/studio/works", params={"kind": "novel"}).json()["data"]
    assert [item["id"] for item in novel_rows["items"]] == [novel["id"]]
    article_rows = client.get("/api/studio/works", params={"kind": "article"}).json()["data"]
    assert [item["id"] for item in article_rows["items"]] == [article["id"]]

    row = novel_rows["items"][0]
    assert row["part_count"] == 0 and row["word_count"] == 0
    # 列表行不拖长字段：brief/outline/parts 只在详情里出现。
    assert "brief" not in row and "outline" not in row and "parts" not in row


def test_detail_contains_outline_and_parts(client):
    work = _create(client, "novel", "雨夜书")
    detail = client.get(f"/api/studio/works/{work['id']}").json()["data"]
    assert detail["outline"] == [] and detail["parts"] == []
    assert detail["brief"] == "" and detail["meta"] == {}


def test_blank_title_and_unknown_kind_are_422(client):
    assert client.post(
        "/api/studio/works", json={"kind": "novel", "title": "   "}
    ).status_code == 422
    assert client.post(
        "/api/studio/works", json={"kind": "poetry", "title": "诗集"}
    ).status_code == 422


# ---- 大纲与正文块链路 ----------------------------------------------------------


def test_outline_save_part_autosave_and_delete_http(client):
    work = _create(client, "novel", "我的书")
    url = f"/api/studio/works/{work['id']}"

    saved = client.put(
        f"{url}/outline",
        json={"nodes": [
            {"kind": "chapter", "title": "第一章"},
            {"kind": "chapter", "title": "第二章"},
        ]},
    ).json()["data"]
    assert saved["part_count"] == 2
    cid = saved["parts"][0]["cid"]
    assert cid.startswith("p")

    # 正文自动保存：字数服务端统计，改名回写大纲叶子。
    detail = client.put(
        f"{url}/parts/{cid}",
        json={"title": "第一章 雨夜", "content": "少年策马下山，city 不相信眼泪。"},
    ).json()["data"]
    part = next(item for item in detail["parts"] if item["cid"] == cid)
    # CJK 11（少年策马下山 + 不相信眼泪）+ 拉丁词 city 1 = 12。
    assert part["title"] == "第一章 雨夜" and part["word_count"] == 12
    assert detail["outline"][0]["title"] == "第一章 雨夜"

    after_delete = client.delete(f"{url}/parts/{cid}").json()["data"]
    assert after_delete["part_count"] == 1
    assert all(item["cid"] != cid for item in after_delete["parts"])


def test_part_cap_returns_business_envelope(client, monkeypatch):
    work = _create(client, "novel", "受限的书")
    monkeypatch.setattr(get_settings(), "WRITING_MAX_PARTS_PER_WORK", 1)
    response = client.put(
        f"/api/studio/works/{work['id']}/outline",
        json={"nodes": [
            {"kind": "chapter", "title": "第一章"},
            {"kind": "chapter", "title": "第二章"},
        ]},
    )
    # 业务拒绝保持 HTTP 200，靠信封里的非零 code 表达，前端走提示而不是错误页。
    body = response.json()
    assert body["code"] == -1
    assert "最多" in body["message"]


# ---- 隔离 ---------------------------------------------------------------------


def test_other_owner_gets_404(client_for):
    owner = client_for(OWNER)
    work = _create(owner, "novel", "私密书稿")
    cid = owner.put(
        f"/api/studio/works/{work['id']}/outline",
        json={"nodes": [{"kind": "chapter", "title": "第一章"}]},
    ).json()["data"]["parts"][0]["cid"]

    other = client_for(OTHER)
    assert other.get(f"/api/studio/works/{work['id']}").status_code == 404
    assert other.get("/api/studio/works").json()["data"]["total"] == 0
    assert other.put(
        f"/api/studio/works/{work['id']}/parts/{cid}", json={"content": "偷改"}
    ).status_code == 404
    assert other.get(
        f"/api/studio/works/{work['id']}/export", params={"format": "md"}
    ).status_code == 404
    assert other.delete(f"/api/studio/works/{work['id']}").status_code == 404


# ---- 导出 ---------------------------------------------------------------------


def test_export_markdown_and_text_headers(client):
    work = _create(client, "novel", "我的书")
    url = f"/api/studio/works/{work['id']}"
    cid = client.put(
        f"{url}/outline", json={"nodes": [{"kind": "chapter", "title": "第一章"}]}
    ).json()["data"]["parts"][0]["cid"]
    client.put(f"{url}/parts/{cid}", json={"content": "第一章正文。"})

    md = client.get(f"{url}/export", params={"format": "md"})
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.headers["x-content-type-options"] == "nosniff"
    assert md.headers["cache-control"] == "no-store"
    disposition = md.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition and disposition.endswith(".md")
    assert "# 我的书" in md.text and "第一章正文。" in md.text

    txt = client.get(f"{url}/export", params={"format": "txt"})
    assert txt.headers["content-type"].startswith("text/plain")
    assert txt.headers["content-disposition"].endswith(".txt")
    assert "#" not in txt.text and "第一章正文。" in txt.text


# ---- 删除 ---------------------------------------------------------------------


def test_delete_work_http(client):
    work = _create(client, "article", "待删稿")
    message = client.delete(f"/api/studio/works/{work['id']}").json()["message"]
    assert message == "deleted"
    assert client.get(f"/api/studio/works/{work['id']}").status_code == 404
