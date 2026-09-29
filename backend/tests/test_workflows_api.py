"""/workflows 路由：清单、SSE 进度流与运行详情。

只测 HTTP 这一层该管的事：路由顺序、鉴权注入、事件协议与统一信封。编排本身
在 ``tests/test_workflows.py`` 里已经钉过，这里把模型调用打桩后走一遍真实请求。
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
from app.models.entities import ModelConfig, Workflow
from app.models.schemas import CurrentUser, ModelPurpose
from app.routers import workflows as workflows_router
from app.services import workflows as workflows_module
from app.services.workflows import seed_builtin_workflows

OWNER = 1


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)
    # 引擎与步骤各开各的 session，走的就是这个工厂。
    monkeypatch.setattr("app.database.SessionLocal", Session)

    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: CurrentUser(user_id=OWNER, username="t")

    with Session() as db:
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
    return TestClient(app), Session


def _stub_model(monkeypatch, reply: str):
    async def fake_run_agent(
        db, owner_id, agent_slug, user_prompt, *, agent=None, config_id=None, timeout=None
    ):
        return reply

    monkeypatch.setattr(workflows_module, "run_agent", fake_run_agent)


def _events(response) -> list[tuple[str, dict]]:
    """把 SSE 响应解析成 ``(event, data)`` 列表。"""
    parsed = []
    name = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            parsed.append((name, json.loads(line[len("data: ") :])))
    return parsed


# ---- 定义清单 ---------------------------------------------------------------


def test_list_workflows_returns_builtins_with_steps(env):
    client, _ = env
    body = client.get("/api/workflows").json()
    assert body["code"] == 0
    slugs = {item["slug"] for item in body["data"]["items"]}
    assert "resume-analysis" in slugs
    analysis = next(item for item in body["data"]["items"] if item["slug"] == "resume-analysis")
    assert [step["key"] for step in analysis["steps"]] == ["analyze"]
    assert [field["name"] for field in analysis["inputs"]] == ["resume_content", "description_note"]


def test_get_workflow_by_id_and_missing_id_is_404(env):
    client, Session = env
    with Session() as db:
        workflow_id = db.scalar(select(Workflow).where(Workflow.slug == "resume-analysis")).id

    assert client.get(f"/api/workflows/{workflow_id}").json()["data"]["slug"] == "resume-analysis"
    assert client.get("/api/workflows/999999").status_code == 404


def test_runs_route_is_not_shadowed_by_workflow_id(env):
    """``/workflows/runs`` 必须排在 ``/workflows/{workflow_id}`` 前面。

    顺序写错的报错很难看：int 路径参数解析失败，422 而不是一列表。
    """
    client, _ = env
    response = client.get("/api/workflows/runs")
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


# ---- 运行 -------------------------------------------------------------------


def test_stream_run_emits_progress_and_persists_the_scene(env, monkeypatch):
    client, Session = env
    _stub_model(monkeypatch, json.dumps({"report": "报告", "suggestions": ["补量化"]}, ensure_ascii=False))
    with Session() as db:
        workflow_id = db.scalar(select(Workflow).where(Workflow.slug == "resume-analysis")).id

    with client.stream(
        "POST",
        f"/api/workflows/{workflow_id}/runs/stream",
        json={"inputs": {"resume_content": "十年经验"}},
    ) as response:
        events = _events(response)

    assert [name for name, _ in events] == ["run_start", "step_start", "step_end", "run_end"]
    assert events[0][1]["steps"][0]["key"] == "analyze"
    assert events[-1][1]["status"] == "succeeded"
    run_id = events[-1][1]["run_id"]

    # 正文不在事件里：进度流只报状态，现场按 run 取。
    assert "output" not in events[2][1]

    detail = client.get(f"/api/workflows/runs/{run_id}").json()["data"]
    assert detail["inputs"]["resume_content"] == "十年经验"
    assert detail["steps"][0]["prompt"] == '简历全文：\n"""\n十年经验\n"""'
    assert detail["steps"][0]["parsed"] == {"report": "报告", "suggestions": ["补量化"]}


def test_stream_run_reports_missing_model_as_error_event(env, monkeypatch):
    client, Session = env
    with Session() as db:
        workflow_id = db.scalar(select(Workflow).where(Workflow.slug == "resume-analysis")).id
        db.execute(ModelConfig.__table__.delete())
        db.commit()

    with client.stream(
        "POST", f"/api/workflows/{workflow_id}/runs/stream", json={"inputs": {"resume_content": "x"}}
    ) as response:
        events = _events(response)

    # 流已经开始，200 与响应头早就发出去了，所以错误只能是事件。
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["code"] == 4004
    assert "模型管理" in events[0][1]["message"]


def test_stream_run_rejects_unknown_input_before_any_step(env, monkeypatch):
    client, Session = env
    calls = []

    async def no_call(*args, **kwargs):  # 校验没过就不该碰模型
        calls.append(args)
        return "x"

    monkeypatch.setattr(workflows_module, "run_agent", no_call)
    with Session() as db:
        workflow_id = db.scalar(select(Workflow).where(Workflow.slug == "resume-analysis")).id

    with client.stream(
        "POST",
        f"/api/workflows/{workflow_id}/runs/stream",
        json={"inputs": {"resume_context": "拼错的变量名"}},
    ) as response:
        events = _events(response)

    assert events[0][0] == "error"
    assert "缺少必填输入" in events[0][1]["message"]
    assert calls == []
