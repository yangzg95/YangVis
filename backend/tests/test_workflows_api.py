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
from app.services.agents import seed_builtin_agents
from app.services.workflows import WorkflowService, seed_builtin_workflows

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


def test_runs_filter_by_business_subject(env):
    """按业务对象收敛运行记录：功能页面的入口要的是「这个对象跑过哪几次」。

    kind 与 ref 必须成对——简历 3 与 deck 3 的 id 字符串一样，只按 ref 筛会把
    两个对象的运行混在同一页里。
    """
    client, Session = env
    with Session() as db:
        service = WorkflowService(db, OWNER)
        workflow_id = db.scalar(select(Workflow).where(Workflow.slug == "resume-analysis")).id
        for kind, ref, label in (
            ("resume", 1, "张三的简历"),
            ("resume", 2, "李四的简历"),
            ("slide-deck", 1, "季度复盘"),
        ):
            service.create_run(
                workflow_id,
                {"resume_content": "x"},
                subject_kind=kind,
                subject_ref=ref,
                subject_label=label,
            )

    items = client.get(
        "/api/workflows/runs", params={"subject_kind": "resume", "subject_ref": "1"}
    ).json()["data"]["items"]
    assert [(item["subject_kind"], item["subject_ref"], item["subject_label"]) for item in items] == [
        ("resume", "1", "张三的简历")
    ]

    assert client.get(
        "/api/workflows/runs", params={"subject_ref": "1"}
    ).status_code == 400


# ---- 定义编辑 ---------------------------------------------------------------


def _body(**overrides) -> dict:
    """一条最小可运行的个人工作流（人设取内置的通用助手）。

    校验细节在 ``test_workflows.py`` 里钉，这里只验 HTTP 层：整份提交、错误怎么
    呈现、内置项的改法。
    """
    payload = {
        "slug": "my-flow",
        "name": "我的流程",
        "inputs": [{"name": "topic", "label": "主题", "required": True}],
        "steps": [
            {
                "key": "draft",
                "group_key": "draft",
                "agent_slug": "general-chat",
                "prompt_tpl": "就{{topic}}写一段话",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_create_update_and_delete_a_personal_workflow(env):
    client, _ = env

    created = client.post("/api/workflows", json=_body()).json()
    assert created["code"] == 0
    assert created["data"]["is_builtin"] is False
    assert [step["key"] for step in created["data"]["steps"]] == ["draft"]
    assert created["data"]["steps"][0]["agent_name"] == "通用助手"
    workflow_id = created["data"]["id"]

    updated = client.put(
        f"/api/workflows/{workflow_id}",
        json={**_body(), "name": "改过名的流程", "enabled": False},
    ).json()
    assert updated["data"]["name"] == "改过名的流程"
    assert updated["data"]["enabled"] is False

    assert client.delete(f"/api/workflows/{workflow_id}").json()["message"] == "deleted"
    assert client.get(f"/api/workflows/{workflow_id}").status_code == 404


def test_create_reports_a_validation_failure_as_an_envelope_error(env):
    client, Session = env

    body = client.post(
        "/api/workflows", json=_body(steps=[{**_body()["steps"][0], "agent_slug": "ghost"}])
    ).json()

    # 与智能体同一套约定：形状没错但内容不合法是业务拒绝，走 code 不走 HTTP 状态。
    assert body["code"] == -1
    assert "不存在的智能体" in body["message"]
    with Session() as db:
        assert db.query(Workflow).filter_by(slug="my-flow").first() is None


def test_builtin_definition_is_read_only_but_can_be_copied(env):
    client, Session = env
    with Session() as db:
        builtin_id = db.scalar(select(Workflow).where(Workflow.slug == "resume-analysis")).id

    refused = client.put(f"/api/workflows/{builtin_id}", json=_body()).json()
    assert refused["code"] == -1
    assert "内置工作流不可修改" in refused["message"]

    copied = client.post(f"/api/workflows/{builtin_id}/duplicate").json()["data"]
    assert copied["is_builtin"] is False
    assert copied["slug"] == "resume-analysis-copy"
    assert [step["key"] for step in copied["steps"]] == ["analyze"]

    edited = client.put(
        f"/api/workflows/{copied['id']}",
        json={
            "name": copied["name"],
            "description": copied["description"],
            "inputs": [{"name": "resume_content", "label": "简历全文", "required": True}],
            "steps": [
                {
                    "key": "analyze",
                    "group_key": "analyze",
                    "agent_slug": "resume-analyzer",
                    "prompt_tpl": "只看{{resume_content}}",
                    "output_kind": "json",
                    "output_keys": ["report"],
                    "output_var": "analysis",
                }
            ],
        },
    ).json()
    assert edited["data"]["steps"][0]["prompt_tpl"] == "只看{{resume_content}}"
    assert edited["data"]["inputs"] == [
        {"name": "resume_content", "label": "简历全文", "required": True}
    ]


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
