"""首页工作台概览（DashboardService）的单元测试。

用 SQLite 内存库跑聚合，不连真实台账实例：这里要验证的是「数了哪些行」，
而不是数据库本身。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.entities import (
    Agent,
    AiCallLog,
    AiChannel,
    ChatConversation,
    InterviewRecord,
    KbDocument,
    KbProject,
    Note,
    OpsDatabase,
    OpsServer,
    Resume,
    SlideDeck,
    Workflow,
    WorkflowRun,
)
from app.services.dashboard import DashboardService

OWNER = 7
OTHER = 8


@pytest.fixture()
def db():
    # StaticPool + 单连接：内存库默认每个连接各有一份数据，测试会看不到自己写的行。
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _mine(db, owner_id: int = OWNER) -> None:
    """给某个 owner 铺一套各模块一行数据。"""
    workflow = Workflow(owner_id=owner_id, slug=f"w{owner_id}", name="流水线")
    db.add_all(
        [
            Agent(owner_id=owner_id, slug=f"a{owner_id}", name="自建", system_prompt="p"),
            KbProject(owner_id=owner_id, name="项目"),
            KbDocument(owner_id=owner_id, filename="d.md", content="x"),
            ChatConversation(owner_id=owner_id, title="会话"),
            Resume(owner_id=owner_id, title="r", filename="r.pdf", content="x"),
            SlideDeck(owner_id=owner_id, title="s", source_text="x"),
            InterviewRecord(owner_id=owner_id, company="c", position="p"),
            Note(owner_id=owner_id, title="n"),
            OpsServer(owner_id=owner_id, name=f"s{owner_id}", host="1.1.1.1", username="root"),
            OpsDatabase(owner_id=owner_id, name=f"d{owner_id}", host="1.1.1.1"),
            workflow,
        ]
    )
    db.commit()
    db.add(
        WorkflowRun(
            owner_id=owner_id,
            workflow_id=workflow.id,
            workflow_slug=workflow.slug,
            inputs={},
            status="succeeded",
            created_at=datetime.now(),
        )
    )
    db.commit()


def test_counts_are_scoped_to_owner(db):
    _mine(db)
    _mine(db, OTHER)

    data = DashboardService(db, OWNER).summary(is_admin=False)
    assert data.knowledge_project_count == 1
    assert data.knowledge_document_count == 1
    assert data.resume_count == 1
    assert data.slide_count == 1
    assert data.interview_count == 1
    assert data.note_count == 1
    assert data.server_count == 1
    assert data.database_count == 1
    # 会话按 owner 隔离；内置智能体（owner_id=0）对所有人可用，算进每个人的计数。
    assert data.conversation_count == 1
    assert data.agent_count == 1
    assert data.workflow_count == 1
    assert data.run_count_today == 1


def test_builtin_workflows_counted_but_disabled_excluded(db):
    db.add_all(
        [
            Workflow(owner_id=0, slug="builtin", name="内置"),
            Workflow(owner_id=0, slug="off", name="停用", enabled=False),
        ]
    )
    db.commit()

    data = DashboardService(db, OWNER).summary(is_admin=False)
    # 内置流程对所有人可用（与智能体同一口径），停用的不算。
    assert data.workflow_count == 1


def test_run_count_today_excludes_yesterday(db):
    workflow = Workflow(owner_id=OWNER, slug="w", name="流水线")
    db.add(workflow)
    db.commit()
    db.add_all(
        [
            WorkflowRun(
                owner_id=OWNER,
                workflow_id=workflow.id,
                workflow_slug="w",
                inputs={},
                status="succeeded",
                created_at=datetime.now(),
            ),
            WorkflowRun(
                owner_id=OWNER,
                workflow_id=workflow.id,
                workflow_slug="w",
                inputs={},
                status="succeeded",
                created_at=datetime.now() - timedelta(days=1),
            ),
            # 别人今天的运行不进我的口径。
            WorkflowRun(
                owner_id=OTHER,
                workflow_id=workflow.id,
                workflow_slug="w",
                inputs={},
                status="succeeded",
                created_at=datetime.now(),
            ),
        ]
    )
    db.commit()

    data = DashboardService(db, OWNER).summary(is_admin=False)
    assert data.run_count_today == 1


def test_builtin_agents_counted_but_disabled_excluded(db):
    db.add_all(
        [
            Agent(owner_id=0, slug="builtin", name="内置", system_prompt="p"),
            Agent(owner_id=0, slug="off", name="停用", system_prompt="p", enabled=False),
        ]
    )
    db.commit()

    data = DashboardService(db, OWNER).summary(is_admin=False)
    assert data.agent_count == 1


def test_ops_bound_conversations_excluded(db):
    db.add_all(
        [
            ChatConversation(owner_id=OWNER, title="主对话"),
            ChatConversation(
                owner_id=OWNER, title="运维问答", ops_target_type="server", ops_target_id=1
            ),
        ]
    )
    db.commit()

    data = DashboardService(db, OWNER).summary(is_admin=False)
    assert data.conversation_count == 1


def test_gateway_fields_hidden_from_non_admin(db):
    db.add(AiChannel(name="c", base_url="https://api.example.com/v1"))
    db.commit()

    data = DashboardService(db, OWNER).summary(is_admin=False)
    assert data.ai_channel_count is None
    assert data.ai_call_count_today is None
    assert data.ai_token_count_today is None


def test_admin_gateway_counts_are_today_only(db):
    now = datetime.now()
    db.add_all(
        [
            AiChannel(name="c1", base_url="https://a.example.com/v1"),
            AiChannel(name="c2", base_url="https://b.example.com/v1", enabled=False),
            AiCallLog(
                request_id="r1",
                endpoint="chat/completions",
                status_code=200,
                success=True,
                total_tokens=40,
                created_at=now,
            ),
            AiCallLog(
                request_id="r2",
                endpoint="chat/completions",
                status_code=200,
                success=True,
                total_tokens=99,
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    db.commit()

    data = DashboardService(db, OWNER).summary(is_admin=True)
    assert data.ai_channel_count == 2
    assert data.ai_call_count_today == 1
    assert data.ai_token_count_today == 40
