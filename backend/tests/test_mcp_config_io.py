"""mcpServers 标准格式（Claude Desktop / Cursor 通用）的导入导出。

钉的是这几件事：
- 解析：stdio / sse / streamable-http 三种形态、裸 dict 容错、type 推断、逐条收集错误；
- 导出：明文密钥、按 ids 选择、顺序、他人/不存在 id 拒绝；
- 同名策略：rename / skip / overwrite（overwrite 保留 id，挂载引用不断）；
- 接口层：/mcp/import、/mcp/export 与 /mcp/export 对未知 id 的 404。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.models.entities import McpServer
from app.models.schemas import CurrentUser, McpServerCreate
from app.routers import mcp as mcp_router
from app.services.mcp import (
    McpServerService,
    export_document,
    parse_mcp_config,
    read_secret,
)

OWNER = 1
OTHER = 2

LARK_DOC = {
    "mcpServers": {
        "lark-mcp": {
            "command": "npx",
            "args": [
                "-y",
                "@larksuiteoapi/lark-mcp",
                "mcp",
                "-a",
                "<App_ID>",
                "-s",
                "<App_Secret>",
                "--oauth",
            ],
        }
    }
}


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp_io.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture()
def db(engine, monkeypatch):
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    monkeypatch.setattr("app.database.SessionLocal", maker)
    try:
        yield session
    finally:
        session.close()


# ---- 小工具 -----------------------------------------------------------------


def _row(
    db,
    *,
    name: str,
    owner_id: int = OWNER,
    transport: str = "stdio",
    enabled: bool = True,
) -> McpServer:
    server = McpServer(
        owner_id=owner_id,
        name=name,
        transport=transport,
        enabled=enabled,
        command="python" if transport == "stdio" else None,
        args=[] if transport == "stdio" else None,
        url=None if transport == "stdio" else "http://localhost:1/sse",
    )
    db.add(server)
    db.commit()
    return server


# ---- 解析 -------------------------------------------------------------------


def test_parse_lark_example_as_stdio(db):
    entries, errors = parse_mcp_config(LARK_DOC)
    assert errors == []
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "lark-mcp"
    assert entry.transport == "stdio"
    assert entry.command == "npx"
    assert entry.args == [
        "-y",
        "@larksuiteoapi/lark-mcp",
        "mcp",
        "-a",
        "<App_ID>",
        "-s",
        "<App_Secret>",
        "--oauth",
    ]


def test_parse_bare_dict_and_remote_type_aliases(db):
    # 没有 mcpServers 包裹的裸 dict 也接受。
    entries, errors = parse_mcp_config({"s1": {"command": "x"}})
    assert errors == []
    assert entries[0].name == "s1"

    doc = {
        "mcpServers": {
            "srv-sse": {"type": "sse", "url": "http://x/sse", "headers": {"A": "b"}},
            "srv-http": {"type": "streamable-http", "url": "http://x/mcp"},
            "srv-http2": {"type": "http", "url": "http://x/mcp2"},
            # 千帆/Cline 导出的驼峰写法。
            "srv-http3": {"type": "streamableHttp", "url": "http://x/mcp3"},
        }
    }
    entries, errors = parse_mcp_config(doc)
    assert errors == []
    assert entries[0].transport == "sse"
    assert entries[0].headers == {"A": "b"}
    assert entries[1].transport == "streamable_http"
    assert entries[2].transport == "streamable_http"
    assert entries[3].transport == "streamable_http"


def test_parse_infers_remote_type_when_type_missing(db):
    entries, errors = parse_mcp_config(
        {
            "mcpServers": {
                "a": {"url": "http://x/sse"},
                "b": {"url": "http://x/mcp"},
            }
        }
    )
    assert errors == []
    assert entries[0].transport == "sse"
    assert entries[1].transport == "streamable_http"


def test_parse_collects_per_entry_errors_without_aborting_batch(db):
    doc = {
        "mcpServers": {
            "bad-empty": {"type": "stdio"},  # 没有 command
            "bad-args": {"command": "x", "args": "nope"},
            "bad-env": {"command": "x", "env": []},
            "bad-type": {"type": "websocket"},
            "bad-notdict": "x",
            "good": {"command": "ok"},
            " ": {"command": "ok"},  # 空白名称
        }
    }
    entries, errors = parse_mcp_config(doc)

    assert [entry.name for entry in entries] == ["good"]
    # 6 条坏配置全部有错误，且各带自己的条目名。
    assert len(errors) == 6
    assert any(error.startswith("bad-args") for error in errors)
    assert any("服务名称为空" in error for error in errors)


def test_parse_rejects_wrong_top_level_shape(db):
    with pytest.raises(ValueError, match="JSON 对象"):
        parse_mcp_config([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="mcpServers"):
        parse_mcp_config({"mcpServers": "x"})


# ---- 导出 -------------------------------------------------------------------


def test_export_document_round_trip_content(db):
    service = McpServerService(db, OWNER)
    service.create(
        McpServerCreate(
            name="lark-mcp",
            transport="stdio",
            command="npx",
            args=["-y", "x"],
            env={"TOKEN": "secret1234"},
        )
    )
    service.create(
        McpServerCreate(
            name="远端",
            transport="sse",
            url="http://x/sse",
            headers={"Authorization": "Bearer abc"},
        )
    )

    document, warnings = export_document(service.export_rows(None))

    assert warnings == []
    assert document["mcpServers"]["lark-mcp"] == {
        "command": "npx",
        "args": ["-y", "x"],
        "env": {"TOKEN": "secret1234"},
    }
    assert document["mcpServers"]["远端"] == {
        "type": "sse",
        "url": "http://x/sse",
        "headers": {"Authorization": "Bearer abc"},
    }


def test_export_rows_filters_by_ids_and_order(db):
    a = _row(db, name="a")
    b = _row(db, name="b")
    _row(db, name="停用", enabled=False)
    service = McpServerService(db, OWNER)

    # 不带 ids：全部（含停用）。
    assert {row.name for row in service.export_rows(None)} == {"a", "b", "停用"}

    # 带 ids：按入参顺序返回，与表内默认排序无关。
    rows = service.export_rows([b.id, a.id])
    assert [row.id for row in rows] == [b.id, a.id]

    with pytest.raises(LookupError):
        service.export_rows([99999])


def test_export_rows_rejects_foreign_id(db):
    _row(db, name="别人的", owner_id=OTHER)
    service = McpServerService(db, OWNER)
    with pytest.raises(LookupError):
        service.export_rows([db.query(McpServer).one().id])


def test_export_keeps_connection_fields_when_secret_unreadable(db):
    service = McpServerService(db, OWNER)
    server = service.create(
        McpServerCreate(
            name="坏密钥",
            transport="stdio",
            command="npx",
            args=["x"],
            env={"TOKEN": "secret1234"},
        )
    )
    server.env_enc = b"garbage-not-fernet"
    db.commit()

    document, warnings = export_document([server])

    assert len(warnings) == 1
    assert "密钥解密失败" in warnings[0]
    item = document["mcpServers"]["坏密钥"]
    assert item["command"] == "npx"
    assert "env" not in item  # 凭据未导出，连接字段照出。


# ---- 跨用户往返 ---------------------------------------------------------------


def test_export_then_import_between_owners_is_identical(db):
    svc_a = McpServerService(db, OWNER)
    svc_a.create(
        McpServerCreate(
            name="lark-mcp", transport="stdio", command="npx",
            args=["-y", "x"], env={"TOKEN": "secret1234"},
        )
    )
    svc_a.create(
        McpServerCreate(
            name="远端", transport="streamable_http", url="http://x/mcp",
            headers={"Authorization": "Bearer abc"},
        )
    )
    document, warnings = export_document(svc_a.export_rows(None))
    assert warnings == []

    svc_b = McpServerService(db, OTHER)
    report = svc_b.import_config(document, conflict="rename")
    assert report.created == 2
    assert report.errors == []

    document_b, _ = export_document(svc_b.export_rows(None))
    assert document_b == document


# ---- 同名策略 -----------------------------------------------------------------


def test_import_rename_conflict_gets_suffix(db):
    service = McpServerService(db, OWNER)
    service.create(
        McpServerCreate(name="lark-mcp", transport="stdio", command="old")
    )

    report = service.import_config(LARK_DOC, conflict="rename")

    assert report.created == 1
    assert report.renamed == ["lark-mcp -> lark-mcp-2"]
    assert {row.name for row in service.list()} == {"lark-mcp", "lark-mcp-2"}


def test_import_rename_handles_duplicate_names_within_one_batch(db):
    # 两个 key trim 后同名：第二个也得走改名。
    doc = {
        "mcpServers": {
            "dup": {"command": "a"},
            " dup": {"command": "b"},
        }
    }
    report = McpServerService(db, OWNER).import_config(doc, conflict="rename")

    assert report.created == 2
    assert report.renamed == ["dup -> dup-2"]


def test_import_skip_conflict_creates_nothing(db):
    service = McpServerService(db, OWNER)
    service.create(
        McpServerCreate(name="lark-mcp", transport="stdio", command="old")
    )

    report = service.import_config(LARK_DOC, conflict="skip")

    assert report.skipped == 1
    assert report.created == 0
    assert service.list()[0].command == "old"


def test_import_overwrite_replaces_everything_but_keeps_id(db):
    service = McpServerService(db, OWNER)
    existing = service.create(
        McpServerCreate(
            name="lark-mcp", transport="stdio", command="old",
            env={"STALE": "oldvalue"},
        )
    )
    old_id = existing.id

    report = service.import_config(LARK_DOC, conflict="overwrite")

    assert report.overwritten == 1
    assert report.created == 0
    updated = service.require(old_id)  # id 不变：工作流挂载引用不断。
    assert updated.command == "npx"
    assert updated.args == LARK_DOC["mcpServers"]["lark-mcp"]["args"]
    # 导入文档没有 env：旧密钥袋整份替换为空，而不是残留 STALE。
    assert read_secret(updated.env_enc) == {}


# ---- 接口层 ------------------------------------------------------------------


@pytest.fixture()
def client(db, monkeypatch):
    maker = sessionmaker(bind=db.bind, future=True)
    app = FastAPI()
    app.include_router(mcp_router.router, prefix="/api")

    def override_get_db():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=OWNER, username="t"
    )
    return TestClient(app)


def test_export_api_returns_empty_servers(client):
    res = client.get("/api/mcp/export")
    assert res.status_code == 200
    assert res.json()["data"]["mcpServers"] == {}


def test_import_then_export_api_flow(client):
    res = client.post("/api/mcp/import", json={"config": LARK_DOC})
    assert res.status_code == 200
    assert res.json()["data"]["created"] == 1

    res = client.get("/api/mcp/export")
    assert res.status_code == 200
    servers = res.json()["data"]["mcpServers"]
    assert servers["lark-mcp"]["command"] == "npx"


def test_export_api_unknown_id_is_404(client):
    res = client.get("/api/mcp/export", params=[("ids", "99999")])
    assert res.status_code == 404
