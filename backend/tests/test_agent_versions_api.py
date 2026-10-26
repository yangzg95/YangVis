"""版本接口的可见性 + 工作流执行时把人设版本绑到步骤行（端到端）。

这一层钉最关键的一条历史语义：人设后来改过，旧运行的步骤仍然指着它当时的版本，
新运行才指新版本。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.models.entities import Agent, ModelConfig, Workflow
from app.models.schemas import CurrentUser, ModelPurpose
from app.routers import agents as agents_router
from app.routers import workflows as workflows_router
from app.services.agents import seed_builtin_agents
from app.services.workflows import exec as workflow_exec
from app.services.workflows.builtin import seed_builtin_workflows

OWNER = 1
OTHER = 2


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-ver.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", Session)

    async def fake_run_agent(*args, **kwargs):
        return "ok"

    monkeypatch.setattr(workflow_exec, "run_agent", fake_run_agent)

    def make_client(owner: int) -> TestClient:
        app = FastAPI()
        app.include_router(agents_router.router, prefix="/api")
        app.include_router(workflows_router.router, prefix="/api")

        def override_get_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_user] = lambda: CurrentUser(
            user_id=owner, username=f"u{owner}"
        )
        return TestClient(app)

    with Session() as db:
        seed_builtin_agents(db)
        seed_builtin_workflows(db)
        db.add(
            ModelConfig(
                owner_id=OWNER,
                purpose=ModelPurpose.CHAT.value,
                title="测试对话模型",
                model_name="unit-test",
                base_url="http://localhost:1/v1",
                is_default=True,
                last_test_ok=True,
            )
        )
        db.commit()

    return make_client(OWNER), Session, make_client


def _events(response) -> list[tuple[str | None, dict]]:
    parsed = []
    name = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            parsed.append((name, json.loads(line[len("data: ") :])))
    return parsed


def _run(client, workflow_id: int, topic: str = "山") -> int:
    with client.stream(
        "POST",
        f"/api/workflows/{workflow_id}/runs/stream",
        json={"inputs": {"topic": topic}},
    ) as response:
        events = _events(response)
    return next(payload["run_id"] for name, payload in events if name == "run_end")


def test_steps_bind_the_agent_version_at_run_time(env):
    client, Session, _ = env
    # 用个人自建人设：seed 已给内置人设写了 v1，自建的经过 create() 也有 v1。
    agent_id = client.post(
        "/api/agents",
        json={"slug": "writer", "name": "写手", "system_prompt": "你是一版写手。"},
    ).json()["data"]["id"]

    workflow_id = client.post(
        "/api/workflows",
        json={
            "slug": "writer-flow",
            "name": "写手流程",
            "inputs": [{"name": "topic", "label": "主题", "required": True}],
            "steps": [
                {"key": "draft", "group_key": "draft", "agent_slug": "writer", "prompt_tpl": "写{{topic}}"}
            ],
        },
    ).json()["data"]["id"]

    run_one = _run(client, workflow_id)
    step_one = client.get(f"/api/workflows/runs/{run_one}").json()["data"]["steps"][0]
    assert step_one["agent_id"] == agent_id
    assert step_one["agent_version"] == 1

    # 人设改版：之后的运行绑 v2。
    client.put(f"/api/agents/{agent_id}", json={"system_prompt": "你是二版写手，更毒舌。"})
    run_two = _run(client, workflow_id, topic="水")
    step_two = client.get(f"/api/workflows/runs/{run_two}").json()["data"]["steps"][0]
    assert step_two["agent_version"] == 2

    # 旧运行不被追改：它的现场永远对着 v1。
    old_step = client.get(f"/api/workflows/runs/{run_one}").json()["data"]["steps"][0]
    assert old_step["agent_version"] == 1


def test_version_endpoints_list_detail_and_visibility(env):
    client, Session, make_client = env
    with Session() as db:
        builtin_id = db.scalar(select(Agent).where(Agent.slug == "general-chat")).id
        other = Agent(
            owner_id=OTHER, slug="secret", name="私房", system_prompt="不告诉你", is_builtin=False
        )
        db.add(other)
        db.commit()
        from app.services.agent_versions import append

        append(db, other)
        db.commit()
        other_id = other.id

    # 内置人设的版本史人人可读。
    listing = client.get(f"/api/agents/{builtin_id}/versions").json()["data"]
    assert listing["total"] >= 1
    assert listing["items"][0]["version"] == 1
    assert listing["items"][0]["name"]

    detail = client.get(f"/api/agents/{builtin_id}/versions/1").json()["data"]
    assert detail["snapshot"]["system_prompt"]
    assert "temperature" in detail["snapshot"]

    # 别人的私有人设：404，列表与具体版本都一样。
    assert client.get(f"/api/agents/{other_id}/versions").status_code == 404
    assert client.get(f"/api/agents/{other_id}/versions/1").status_code == 404
    # 本人读得到。
    assert make_client(OTHER).get(f"/api/agents/{other_id}/versions").json()["code"] == 0

    # 智能体不存在、版本号不存在，各自 404。
    assert client.get("/api/agents/999999/versions").status_code == 404
    assert client.get(f"/api/agents/{builtin_id}/versions/999").status_code == 404
