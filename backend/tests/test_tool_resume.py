"""本地工具 get_resume_doc：按归属读取在线简历实时内容。

SQLite 文件库 + monkeypatch 工具模块自己 import 进来的 SessionLocal
（from import 已在 resume_tools 里形成新的名字绑定，patch app.database 不影响它）。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.services.resume_builder import ResumeBuilderService
from app.services.tools import ToolContext, specs
from app.services.tools.builtins import resume_tools


OWNER = 1
OTHER = 2


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool_resume.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    # 工具是同步函数、在线程池里自开 session：直接 patch 它模块内的绑定。
    monkeypatch.setattr(resume_tools, "SessionLocal", maker)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _service(db, owner_id=OWNER):
    return ResumeBuilderService(db, owner_id)


def test_registry_shape():
    spec = {s.key: s for s in specs()}["get_resume_doc"]
    assert spec.title == "读取在线简历"
    assert spec.takes_ctx is True
    assert [p.name for p in spec.params] == ["doc_id"]
    assert spec.params[0].json_type == "integer"
    assert spec.params[0].required is True


def test_read_returns_latest_content(db):
    service = _service(db)
    row = service.create_doc("我的简历")
    # JSON 往返造一个全新 dict：与 API 入口拿到的反序列化对象同构，直接改 row.doc
    # 会因为 JSON 列检测不到就地变更而不发 UPDATE。
    doc = json.loads(json.dumps(row.doc))
    doc["profile"]["name"] = "张三"
    doc["profile"]["job_target"] = "后端工程师"
    doc["skills_html"] = "<ul><li>Python</li></ul>"
    service.update_doc(row.id, title=None, doc=doc)

    out = resume_tools.get_resume_doc(ToolContext(OWNER, 0), row.id)
    assert "在线简历《我的简历》" in out
    assert "张三" in out
    assert "后端工程师" in out
    assert "Python" in out
    assert f"id：{row.id}" in out

    # 读到的是实时数据：改完再读立即反映，不依赖对话早期的快照。
    doc["profile"]["name"] = "李四"
    service.update_doc(row.id, title=None, doc=json.loads(json.dumps(doc)))
    assert "李四" in resume_tools.get_resume_doc(ToolContext(OWNER, 0), row.id)


def test_other_owner_gets_not_found_message(db):
    row = _service(db).create_doc("私密简历")
    out = resume_tools.get_resume_doc(ToolContext(OTHER, 0), row.id)
    assert "未找到" in out
    assert "私密简历" not in out


def test_missing_id_message(db):
    out = resume_tools.get_resume_doc(ToolContext(OWNER, 0), 999)
    assert "未找到 id 为 999" in out
