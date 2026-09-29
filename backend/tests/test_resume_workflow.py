"""简历分析与对比走工作流引擎的集成测试。

P0 的验收点：业务行上的 ``status`` / ``report`` 暂时没动，但模型调用已经改由工作
流执行——每次分析都要在 ``workflow_run`` 与 ``workflow_run_step`` 里留下渲染后的
prompt 与原始输出。这里就钉这一条：跑通、留下现场、失败时两边状态一致。

模型调用打桩（替换 ``app.services.workflows.run_agent``），不碰真实服务商。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entities import (
    ModelConfig,
    Resume,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
)
from app.models.schemas import ModelPurpose
from app.services import workflows as workflows_module
from app.services.resume import (
    STATUS_ERROR,
    STATUS_READY,
    ResumeService,
    _parse_analysis,
)
from app.services.workflows import RUN_SUCCEEDED, seed_builtin_workflows

OWNER = 1


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """文件库 + 可用的对话模型 + seed 过的内置工作流。

    用文件库而不是内存库：引擎里每个步骤自开一个 session，共享一条连接的内存库
    会让它们互相看不见对方写的行。``SessionLocal`` 必须一起换掉，否则步骤会去写
    真实部署库。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'resume.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    monkeypatch.setattr("app.database.SessionLocal", maker)

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
def model_call(monkeypatch):
    """把模型调用换成假的，并记下每次收到的 ``(agent_slug, prompt)``。"""
    calls: list[tuple[str, str]] = []

    def stub(reply_for):
        async def fake_run_agent(
            db, owner_id, agent_slug, user_prompt, *, agent=None, config_id=None, timeout=None
        ):
            calls.append((agent_slug, user_prompt))
            reply = reply_for() if callable(reply_for) else reply_for
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(workflows_module, "run_agent", fake_run_agent)
        return calls

    return stub


def _resume(db, content: str = "十年 Python，主导过三个后端项目", **kwargs) -> Resume:
    return ResumeService(db, OWNER).create_resume(
        title=kwargs.pop("title", "后端工程师简历"),
        description=kwargs.pop("description", None),
        filename="resume.pdf",
        mime="application/pdf",
        size=len(content),
        content=content,
    )


def _read_after_engine_writes(db):
    """结束本会话的读事务。

    步骤写在另一条连接上，而 SQLite 的读事务从第一次 SELECT 起固定快照；不重开
    事务的话这里读到的是运行开始前的旧状态。
    """
    db.rollback()


# ---- 纯函数 -----------------------------------------------------------------


def test_parse_analysis_reads_the_contracted_fields():
    raw = json.dumps(
        {"report": "整体扎实", "suggestions": ["补量化数据", "删旧技能"]}, ensure_ascii=False
    )
    report, suggestions = _parse_analysis(raw)
    assert report == "整体扎实"
    assert suggestions == ["补量化数据", "删旧技能"]


def test_parse_analysis_falls_back_to_raw_text():
    report, suggestions = _parse_analysis("模型这次直接给了段报告")
    assert report == "模型这次直接给了段报告"
    assert suggestions == []


# ---- 分析走工作流 ------------------------------------------------------------


def test_run_analysis_executes_through_the_workflow(db, model_call):
    calls = model_call(
        lambda: json.dumps({"report": "报告正文", "suggestions": ["加项目结果"]}, ensure_ascii=False)
    )
    service = ResumeService(db, OWNER)
    resume = _resume(db, description="希望转做平台方向")
    service.start_analysis(resume.id)

    asyncio.run(service.run_analysis(resume.id))
    _read_after_engine_writes(db)

    done = service.get_resume(resume.id)
    assert done.status == STATUS_READY
    assert done.report == "报告正文"
    assert done.suggestions == ["加项目结果"]
    assert calls[0][0] == "resume-analyzer"

    # 引擎里留下的现场：一次运行加一步的渲染结果，这才是这次迁移的意义。
    run = db.scalar(select(WorkflowRun))
    assert run.workflow_slug == "resume-analysis" and run.status == RUN_SUCCEEDED
    assert run.inputs["description_note"].startswith("上传者对这份简历的补充描述")
    step = db.scalar(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id))
    assert step.key == "analyze" and step.status == "succeeded"
    assert "希望转做平台方向" in step.prompt and "十年 Python" in step.prompt
    assert step.parsed == {"report": "报告正文", "suggestions": ["加项目结果"]}


def test_run_analysis_without_a_description_drops_that_section(db, model_call):
    model_call('{"report": "r", "suggestions": []}')
    service = ResumeService(db, OWNER)
    resume = _resume(db)
    service.start_analysis(resume.id)

    asyncio.run(service.run_analysis(resume.id))
    _read_after_engine_writes(db)

    step = db.scalar(select(WorkflowRunStep))
    assert step.prompt.startswith("简历全文："), "没有补充描述时那一段就是空串"


def test_run_analysis_marks_error_when_the_workflow_fails(db, model_call):
    model_call(RuntimeError("上游 502"))
    service = ResumeService(db, OWNER)
    resume = _resume(db)
    service.start_analysis(resume.id)

    asyncio.run(service.run_analysis(resume.id))
    _read_after_engine_writes(db)

    done = service.get_resume(resume.id)
    assert done.status == STATUS_ERROR and "上游 502" in done.error_msg
    # 业务行与运行记录得讲同一个故事，否则历史里会看到一次「成功」的分析。
    assert db.scalar(select(WorkflowRun)).status == "failed"
    assert db.scalar(select(WorkflowRunStep)).status == "failed"


def test_run_analysis_without_a_seeded_workflow_says_so(db, model_call):
    calls = model_call(lambda: "不该被调用")
    db.execute(Workflow.__table__.delete())
    db.commit()

    service = ResumeService(db, OWNER)
    resume = _resume(db)
    service.start_analysis(resume.id)
    asyncio.run(service.run_analysis(resume.id))

    assert "内置工作流" in service.get_resume(resume.id).error_msg
    assert calls == [], "缺定义时连模型都不该碰"


# ---- 对比走工作流 ------------------------------------------------------------


def test_run_comparison_executes_through_the_workflow(db, model_call):
    calls = model_call("对比结论：甲更合适")
    service = ResumeService(db, OWNER)
    first = _resume(db, title="甲", content="甲的正文")
    second = _resume(db, title="乙", content="乙的正文")
    comparison = service.create_comparison([first.id, second.id], title=None)

    asyncio.run(service.run_comparison(comparison.id))
    _read_after_engine_writes(db)

    assert service.get_comparison(comparison.id).report == "对比结论：甲更合适"
    assert calls[0][0] == "resume-compare"
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.workflow_slug == "resume-comparison"))
    step = db.scalar(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id))
    assert "共 2 份" in step.prompt
    assert "=== 简历 1：甲 ===" in step.prompt and "=== 简历 2：乙 ===" in step.prompt
