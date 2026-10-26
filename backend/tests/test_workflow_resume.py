"""断点续跑与带输入覆盖的单步重跑：引擎层集成测试。

三条单步组串成的自定义流程（经 ``WorkflowService.create`` 建好），每步产物以
``output_var`` 进上下文：

* g1 ``resume-analyzer`` 出 JSON（``analysis``，字段 report / suggestions）；
* g2 ``slide-architect`` 出 markdown（``draft``，模板引 ``{{analysis.report}}``）；
* g3 ``interview-answerer`` 出 markdown（``final``）。

模型调用打桩（替换 ``app.services.workflows.exec.run_agent``），按 ``agent_slug``
应答；不碰真实服务商。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entities import ModelConfig, WorkflowRun, WorkflowRunStep
from app.models.schemas import ModelPurpose, WorkflowCreate
from app.services.workflows import exec as workflow_exec
from app.services.agents import seed_builtin_agents
from app.services.workflows.builtin import seed_builtin_workflows
from app.services.workflows.defs import WorkflowService
from app.services.workflows.exec import execute_run
from app.services.workflows.rerun import prepare_resume, prepare_step_rerun, rerun_step, resume_run
from app.services.workflows import (
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_PENDING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
)

OWNER = 1
KEYS = ("analyze", "architect", "answer")

ANALYSIS = {
    "report": "候选人报告：八年后端，主导过一次迁移",
    "suggestions": "建议追问一致性与缓存",
}
DRAFT = "# 幻灯片草稿\n封面 + 三页要点"
FINAL = "终稿：综合草稿与建议的答复"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """文件库 + 可用对话模型 + seed 过的内置工作流（智能体随内置流程一起 seed）。

    文件库而非内存库：引擎里每个步骤自开一个 session，共享一条连接的内存库会让
    它们互相看不见对方写的行。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'resume.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    monkeypatch.setattr("app.database.SessionLocal", maker)

    # create 时要校验引用的智能体存在：与 API 测试 env 同一口径，两个 seed 都跑。
    seed_builtin_agents(session)
    seed_builtin_workflows(session)
    session.add(
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
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def routed_model(monkeypatch):
    """按 ``agent_slug`` 应答的模型桩。

    replies 里的值可以是字符串、callable（每次调用取一次）；callable 抛错即模型
    调用失败。每次实际调用以 ``(agent_slug, prompt)`` 记进 calls。
    """
    calls: list[tuple[str, str]] = []
    replies: dict[str, object] = {}

    async def fake_run_agent(
        db,
        owner_id,
        agent_slug,
        user_prompt,
        *,
        agent=None,
        config_id=None,
        timeout=None,
        tools=None,
        on_event=None,
    ):
        calls.append((agent_slug, user_prompt))
        reply = replies[agent_slug]
        result = reply() if callable(reply) else reply
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(workflow_exec, "run_agent", fake_run_agent)
    return calls, replies


def _three_stage_workflow(db):
    """建三单步组自定义流程，返回定义行。"""
    return WorkflowService(db, OWNER).create(
        WorkflowCreate(
            slug="custom-three-stage",
            name="三阶段自定义流程",
            inputs=[{"name": "resume_text", "label": "简历全文", "required": True}],
            steps=[
                {
                    "key": "analyze",
                    "group_key": "g1",
                    "agent_slug": "resume-analyzer",
                    "prompt_tpl": "请分析这份简历：\n{{resume_text}}",
                    "output_kind": "json",
                    "output_keys": ["report", "suggestions"],
                    "output_var": "analysis",
                },
                {
                    "key": "architect",
                    "group_key": "g2",
                    "agent_slug": "slide-architect",
                    "prompt_tpl": "依据分析报告起草幻灯片：\n{{analysis.report}}",
                    "output_var": "draft",
                },
                {
                    "key": "answer",
                    "group_key": "g3",
                    "agent_slug": "interview-answerer",
                    "prompt_tpl": "结合草稿与建议给终稿：\n草稿：{{draft}}\n建议：{{analysis.suggestions}}",
                    "output_var": "final",
                },
            ],
        )
    )


async def _drive_initial(run_id: int) -> None:
    """从头消费一次引擎执行（终态在生成器走完时落库）。"""
    async for _event, _payload in execute_run(owner_id=OWNER, run_id=run_id):
        pass


def _rows(db, run_id: int) -> dict[str, WorkflowRunStep]:
    """这次运行按 key 索引的步骤行（先 rollback：步骤写在别的连接上）。"""
    db.rollback()
    rows = db.scalars(
        select(WorkflowRunStep)
        .where(WorkflowRunStep.run_id == run_id)
        .order_by(WorkflowRunStep.sort_order, WorkflowRunStep.id)
    ).all()
    return {row.key: row for row in rows}


@pytest.fixture()
def started_scene(db, routed_model):
    """三阶段流程完整跑成功的现场：workflow / run / calls。"""
    calls, replies = routed_model
    replies.update(
        {
            "resume-analyzer": json.dumps(ANALYSIS, ensure_ascii=False),
            "slide-architect": DRAFT,
            "interview-answerer": FINAL,
        }
    )
    workflow = _three_stage_workflow(db)
    run = WorkflowService(db, OWNER).create_run(
        workflow.id, {"resume_text": "十年 Python 经验"}
    )
    asyncio.run(_drive_initial(run.id))
    db.rollback()
    return SimpleNamespace(workflow=workflow, run=run, calls=calls)


# ---- 用例 -------------------------------------------------------------------


def test_resume_from_g2_resets_only_later_groups(db, started_scene):
    run = started_scene.run
    calls = started_scene.calls
    g1_prompt_before = _rows(db, run.id)["analyze"].prompt

    prepared, start_index = prepare_resume(db, OWNER, run.id, "architect")
    assert start_index == 1
    assert prepared.status == RUN_RUNNING

    rows = _rows(db, run.id)
    # 上游组原样：状态与 prompt 都不动。
    assert rows["analyze"].status == STEP_SUCCEEDED
    assert rows["analyze"].prompt == g1_prompt_before
    # 目标组及之后重置为待运行，现场清空。
    for key in ("architect", "answer"):
        assert rows[key].status == STEP_PENDING
        assert rows[key].prompt is None and rows[key].output is None

    asyncio.run(resume_run(owner_id=OWNER, run_id=run.id, start_group_index=1))

    db.rollback()  # 终态写在引擎自己的连接上：别读身份映射里的 running。
    run_row = db.get(WorkflowRun, run.id)
    assert run_row.status == RUN_SUCCEEDED
    rows = _rows(db, run.id)
    assert ANALYSIS["report"] in rows["architect"].prompt
    assert rows["answer"].status == STEP_SUCCEEDED and FINAL in rows["answer"].output

    # g1 模型全程只调一次；续跑只产生 g2/g3 两次（总 5 次）。
    assert [slug for slug, _ in calls].count("resume-analyzer") == 1
    assert len(calls) == 5
    assert [slug for slug, _ in calls[-2:]] == ["slide-architect", "interview-answerer"]


def test_failed_g2_can_be_fixed_and_resumed(db, routed_model):
    calls, replies = routed_model
    state = {"fail_left": 1}

    def architect_reply():
        # 首跑失败一次，续跑恢复——模拟修好上游后接着跑。
        if state["fail_left"]:
            state["fail_left"] -= 1
            raise RuntimeError("上游 502")
        return DRAFT

    replies.update(
        {
            "resume-analyzer": json.dumps(ANALYSIS, ensure_ascii=False),
            "slide-architect": architect_reply,
            "interview-answerer": FINAL,
        }
    )
    workflow = _three_stage_workflow(db)
    run = WorkflowService(db, OWNER).create_run(
        workflow.id, {"resume_text": "十年 Python 经验"}
    )
    asyncio.run(_drive_initial(run.id))

    rows = _rows(db, run.id)
    assert db.get(WorkflowRun, run.id).status == "failed"
    assert rows["architect"].status == "failed"
    assert rows["answer"].status == STEP_SKIPPED

    _prepared, start_index = prepare_resume(db, OWNER, run.id, "architect")
    asyncio.run(resume_run(owner_id=OWNER, run_id=run.id, start_group_index=start_index))

    db.rollback()
    assert db.get(WorkflowRun, run.id).status == RUN_SUCCEEDED
    rows = _rows(db, run.id)
    assert rows["architect"].status == STEP_SUCCEEDED
    assert rows["answer"].status == STEP_SUCCEEDED
    # g3 是续跑时才真正调模型跑出来的（首跑 skipped，没调用）。
    assert [slug for slug, _ in calls].count("interview-answerer") == 1


def test_resume_uses_override_and_rejects_bad_inputs(db, started_scene):
    run = started_scene.run
    override = {"report": "覆盖报告：一段特殊经历", "suggestions": "覆盖建议"}
    raw = json.dumps(override, ensure_ascii=False)

    _prepared, start_index = prepare_resume(
        db, OWNER, run.id, "architect", overrides={"analysis": raw}
    )
    asyncio.run(
        resume_run(
            owner_id=OWNER,
            run_id=run.id,
            start_group_index=start_index,
            overrides={"analysis": raw},
        )
    )

    rows = _rows(db, run.id)
    assert "覆盖报告：一段特殊经历" in rows["architect"].prompt
    assert ANALYSIS["report"] not in rows["architect"].prompt

    # 非法 JSON 与未知变量名：prepare 阶段就拒绝。
    with pytest.raises(ValueError):
        prepare_resume(db, OWNER, run.id, "architect", overrides={"analysis": "{"})
    with pytest.raises(ValueError):
        prepare_resume(db, OWNER, run.id, "architect", overrides={"ghost": "x"})
    # 失败发生在重置之前：运行仍成功，步骤行未动。
    assert db.get(WorkflowRun, run.id).status == RUN_SUCCEEDED
    assert _rows(db, run.id)["architect"].status == STEP_SUCCEEDED


def test_resume_from_g1_reruns_all_groups(db, started_scene):
    run = started_scene.run
    calls = started_scene.calls

    _prepared, start_index = prepare_resume(
        db, OWNER, run.id, "analyze", overrides={"resume_text": "覆盖的简历正文"}
    )
    assert start_index == 0
    rows = _rows(db, run.id)
    assert all(rows[key].status == STEP_PENDING for key in KEYS)

    asyncio.run(
        resume_run(
            owner_id=OWNER,
            run_id=run.id,
            start_group_index=0,
            overrides={"resume_text": "覆盖的简历正文"},
        )
    )

    rows = _rows(db, run.id)
    assert "覆盖的简历正文" in rows["analyze"].prompt
    assert all(rows[key].status == STEP_SUCCEEDED for key in KEYS)
    # 三步全部重跑：g1 模型这是第二次被调。
    assert [slug for slug, _ in calls].count("resume-analyzer") == 2


def test_prepare_resume_gates(db, started_scene):
    run = started_scene.run
    run_id = run.id

    # 运行还在跑：不能续（两个执行体会抢行）。
    db.execute(update(WorkflowRun).where(WorkflowRun.id == run_id).values(status=RUN_RUNNING))
    db.commit()
    with pytest.raises(ValueError):
        prepare_resume(db, OWNER, run_id, "architect")

    db.execute(
        update(WorkflowRun).where(WorkflowRun.id == run_id).values(status=RUN_SUCCEEDED)
    )
    db.commit()
    # 定义里没有的步骤：没有模板可以继续。
    with pytest.raises(ValueError):
        prepare_resume(db, OWNER, run_id, "ghost")
    # 运行不存在：404 类。
    with pytest.raises(LookupError):
        prepare_resume(db, OWNER, run_id + 9999, "architect")


def test_rerun_single_step_with_override(db, started_scene):
    run = started_scene.run
    override = {"report": "单步覆盖报告", "suggestions": "单步建议"}
    raw = json.dumps(override, ensure_ascii=False)

    prepared = prepare_step_rerun(
        db, OWNER, run.id, "architect", overrides={"analysis": raw}
    )
    assert prepared.status == RUN_RUNNING
    asyncio.run(
        rerun_step(
            owner_id=OWNER, run_id=run.id, key="architect", overrides={"analysis": raw}
        )
    )

    db.rollback()
    assert db.get(WorkflowRun, run.id).status == RUN_SUCCEEDED
    rows = _rows(db, run.id)
    assert "单步覆盖报告" in rows["architect"].prompt
    # g3 不属于本次重跑，行保持成功。
    assert rows["answer"].status == STEP_SUCCEEDED

    # 非法 JSON / 未知变量名：prepare 报错且行不重置。
    with pytest.raises(ValueError):
        prepare_step_rerun(
            db, OWNER, run.id, "architect", overrides={"analysis": "{"}
        )
    with pytest.raises(ValueError):
        prepare_step_rerun(db, OWNER, run.id, "architect", overrides={"nope": "x"})
    assert _rows(db, run.id)["architect"].status == STEP_SUCCEEDED
