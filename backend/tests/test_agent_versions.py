"""智能体版本快照：什么时候追加版本、快照不可变、内置 seed 对账、存量补 v1。

只测版本这一层的语义；HTTP 与工作流执行时的绑定在
``test_agent_versions_api.py`` 里走真实请求。
"""
from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entities import Agent, AgentVersion
from app.models.schemas import AgentCreate, AgentUpdate
from app.services import agent_versions
from app.services.agents import BUILTIN_AGENTS, AgentService, seed_builtin_agents

OWNER = 1


@pytest.fixture()
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agents.db'}")
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine, future=True)


def _create_payload(**overrides) -> AgentCreate:
    base = {
        "slug": "my-agent",
        "name": "我的人设",
        "system_prompt": "你是一个严谨的助手。",
    }
    base.update(overrides)
    return AgentCreate(**base)


def _versions(db, agent_id) -> list[AgentVersion]:
    return agent_versions.list_versions(db, agent_id)


def test_create_writes_v1_and_each_persona_edit_appends(Session):
    with Session() as db:
        service = AgentService(db, OWNER)
        agent = service.create(_create_payload())
        assert [v.version for v in _versions(db, agent.id)] == [1]
        assert _versions(db, agent.id)[0].snapshot["system_prompt"] == "你是一个严谨的助手。"

        # 改人设正文 → v2。
        service.update(agent.id, AgentUpdate(system_prompt="你是一个幽默的助手。"))
        assert [v.version for v in _versions(db, agent.id)] == [2, 1]

        # 只翻对话可见开关 / 调排序 / 改名：都不影响模型输出，不产生新版本。
        service.update(agent.id, AgentUpdate(chat_visible=False, sort_order=50, name="新名字"))
        assert [v.version for v in _versions(db, agent.id)] == [2, 1]
        # 名字照样落进后续版本的快照，当辨认标签用。
        service.update(agent.id, AgentUpdate(system_prompt="再改一版"))
        assert _versions(db, agent.id)[0].snapshot["name"] == "新名字"

        # 温度也是人设的一部分：显式设值与设回 NULL 都各产生一个版本。
        service.update(agent.id, AgentUpdate(temperature=80))
        service.update(agent.id, AgentUpdate(temperature=None))
        assert [v.version for v in _versions(db, agent.id)] == [5, 4, 3, 2, 1]


def test_old_snapshots_never_change(Session):
    with Session() as db:
        service = AgentService(db, OWNER)
        agent = service.create(_create_payload())
        service.update(agent.id, AgentUpdate(system_prompt="完全不同的第二版人设"))
        versions = _versions(db, agent.id)
        assert versions[-1].version == 1
        assert versions[-1].snapshot["system_prompt"] == "你是一个严谨的助手。"
        assert versions[0].snapshot["system_prompt"] == "完全不同的第二版人设"


def test_duplicate_starts_an_independent_v1(Session):
    with Session() as db:
        service = AgentService(db, OWNER)
        source = service.create(_create_payload(slug="source"))
        service.update(source.id, AgentUpdate(system_prompt="第二版"))
        clone = service.duplicate(source.id)
        versions = _versions(db, clone.id)
        assert [v.version for v in versions] == [1]
        assert versions[0].snapshot["system_prompt"] == "第二版"


def test_deleting_agent_keeps_its_versions(Session):
    with Session() as db:
        service = AgentService(db, OWNER)
        agent = service.create(_create_payload())
        agent_id = agent.id
        service.delete(agent.id)
        # 行还在：历史运行步骤上绑着的版本号仍然取得回当时的人设。
        assert db.scalar(select(Agent).where(Agent.id == agent_id)) is None
        assert [v.version for v in _versions(db, agent_id)] == [1]


def test_seed_reconciles_builtin_versions(Session, monkeypatch):
    with Session() as db:
        seed_builtin_agents(db)
        customer = db.scalar(select(Agent).where(Agent.slug == "customer-service"))
        first = _versions(db, customer.id)
        assert [v.version for v in first] == [1]

        # 再 seed 一遍、代码没改：幂等，不追加版本。
        seed_builtin_agents(db)
        assert [v.version for v in _versions(db, customer.id)] == [1]

    # 模拟代码升级：BUILTIN_AGENTS 里这条的 prompt 变了，再 seed 追加 v2。
    changed = tuple(
        dataclasses.replace(item, system_prompt="新版客服人设，必须自称小杨。")
        if item.slug == "customer-service"
        else item
        for item in BUILTIN_AGENTS
    )
    monkeypatch.setattr("app.services.agents.BUILTIN_AGENTS", changed)
    with Session() as db:
        seed_builtin_agents(db)
        customer = db.scalar(select(Agent).where(Agent.slug == "customer-service"))
        versions = _versions(db, customer.id)
        assert [v.version for v in versions] == [2, 1]
        assert versions[0].snapshot["system_prompt"] == "新版客服人设，必须自称小杨。"
        assert versions[1].snapshot["system_prompt"] != "新版客服人设，必须自称小杨。"


def test_ensure_versions_backfills_legacy_agents_only(Session):
    """上线前就存在的智能体没有快照行：启动时按当前值补 v1，且只补一次。"""
    with Session() as db:
        # 直接插行，绕开 create() 的 v1 写入——模拟老数据。
        legacy = Agent(
            owner_id=OWNER, slug="old", name="老人设", system_prompt="旧", is_builtin=False
        )
        fresh = Agent(
            owner_id=2, slug="new", name="新人设", system_prompt="新", is_builtin=False
        )
        db.add_all([legacy, fresh])
        db.commit()
        agent_versions.append(db, fresh)
        db.commit()

        agents = list(db.scalars(select(Agent)).all())
        assert agent_versions.ensure_versions(db, agents) == 1
        assert agent_versions.ensure_versions(db, agents) == 0
        assert [v.version for v in _versions(db, legacy.id)] == [1]
        assert _versions(db, legacy.id)[0].snapshot["system_prompt"] == "旧"
