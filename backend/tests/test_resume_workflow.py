"""简历分析与对比走工作流引擎的集成测试。

P0 钉的是「模型调用改由工作流执行」：每次分析都要在 ``workflow_run`` 与
``workflow_run_step`` 里留下渲染后的 prompt 与原始输出。P2 之后再加一句：业务行
上不再有 ``status`` / ``error_msg``，界面上的状态是从认领了这份简历的那次运行翻译
出来的，所以这里的断言全部走 ``service.to_item(...)``，而不是读实体。

模型调用打桩（替换 ``app.services.workflows.exec.run_agent``），不碰真实服务商。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.errors import BusinessError
from app.models.entities import (
    ModelConfig,
    Resume,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
)
from app.models.schemas import ModelPurpose
from app.services.workflows import exec as workflow_exec
from app.services.resume import ResumeService, _analysis_product
from app.services.workflows.builtin import seed_builtin_workflows
from app.services.workflows.runs import STALE_RUN_ERROR, latest_run, reset_stale_runs
from app.services.workflows import (
    RUN_SUCCEEDED,
    SUBJECT_ANALYZING,
    SUBJECT_ERROR,
    SUBJECT_READY,
    SUBJECT_RESUME,
    SUBJECT_RESUME_COMPARISON,
    SUBJECT_UPLOADED,
)

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
            reply = reply_for() if callable(reply_for) else reply_for
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(workflow_exec, "run_agent", fake_run_agent)
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


def test_analysis_product_reads_the_contracted_fields():
    """后处理取的是引擎按契约解析好的对象，不是再把原文 parse 一遍。"""
    report, suggestions = _analysis_product(
        {"report": "整体扎实", "suggestions": ["补量化数据", "删旧技能"]}, "整段输出"
    )
    assert report == "整体扎实"
    assert suggestions == ["补量化数据", "删旧技能"]


def test_analysis_product_falls_back_to_raw_text():
    report, suggestions = _analysis_product(None, "模型这次直接给了段报告")
    assert report == "模型这次直接给了段报告"
    assert suggestions == []

    # report 字段在但没内容：整段输出仍然是一份能看的报告。
    assert _analysis_product({"report": "  ", "suggestions": []}, "整段输出") == ("整段输出", [])


def test_analysis_product_drops_a_suggestions_field_that_is_not_a_list():
    # 遍历字符串会把建议拆成一个个字，所以非列表形态一律丢弃。
    report, suggestions = _analysis_product({"report": "整体扎实", "suggestions": "补量化数据"}, "")
    assert (report, suggestions) == ("整体扎实", [])


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
    assert service.to_item(done).status == SUBJECT_READY
    assert done.report == "报告正文"
    assert done.suggestions == ["加项目结果"]
    assert calls[0][0] == "resume-analyzer"

    # 引擎里留下的现场：一次运行加一步的渲染结果，这才是这次迁移的意义。
    run = db.scalar(select(WorkflowRun))
    assert run.workflow_slug == "resume-analysis" and run.status == RUN_SUCCEEDED
    assert (run.subject_kind, run.subject_ref) == (SUBJECT_RESUME, str(resume.id))
    assert run.inputs["description_note"].startswith("上传者对这份简历的补充描述")
    step = db.scalar(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id))
    assert step.key == "analyze" and step.status == "succeeded"
    assert "希望转做平台方向" in step.prompt and "十年 Python" in step.prompt
    assert step.parsed == {"report": "报告正文", "suggestions": ["加项目结果"]}


def test_run_analysis_writes_the_parsed_fields_not_the_raw_text(db, model_call):
    """模型裹了代码栅栏与寒暄时，报告里只该有契约里那几个字段的内容。

    这一步声明的是 JSON 输出，剥栅栏与核对字段由引擎做完；后处理再去 parse 一遍
    只会得到同一个对象，还会把「跑偏的格式」变成一条看不见的第二套解析逻辑。
    """
    model_call(
        '```json\n{"report": "报告正文", "suggestions": ["加项目结果"]}\n```\n以上就是我的分析。'
    )
    service = ResumeService(db, OWNER)
    resume = _resume(db)
    service.start_analysis(resume.id)

    asyncio.run(service.run_analysis(resume.id))
    _read_after_engine_writes(db)

    done = service.get_resume(resume.id)
    assert done.report == "报告正文"
    assert done.suggestions == ["加项目结果"]
    step = db.scalar(select(WorkflowRunStep))
    assert "以上就是我的分析" in step.output


def test_status_comes_from_the_run_and_nothing_else(db, model_call):
    """上传后没跑过 = uploaded；发起后 = analyzing；跑完 = ready。三个状态同一份事实源。"""
    model_call('{"report": "r", "suggestions": []}')
    service = ResumeService(db, OWNER)
    resume = _resume(db)
    assert service.to_item(resume).status == SUBJECT_UPLOADED

    service.start_analysis(resume.id)
    assert service.to_item(service.get_resume(resume.id)).status == SUBJECT_ANALYZING

    asyncio.run(service.run_analysis(resume.id))
    _read_after_engine_writes(db)
    assert service.to_item(service.get_resume(resume.id)).status == SUBJECT_READY


def test_list_and_detail_carry_the_derived_status(db, model_call):
    """列表一次查回各自最近的运行：状态在 Item 和 Detail 上是同一个来源。"""
    model_call('{"report": "r", "suggestions": []}')
    service = ResumeService(db, OWNER)
    first = _resume(db, title="甲")
    second = _resume(db, title="乙")
    service.start_analysis(first.id)

    items = {item.title: item for item in service.to_items(service.list_resumes())}
    assert items["甲"].status == SUBJECT_ANALYZING
    assert items["乙"].status == SUBJECT_UPLOADED
    assert service.to_detail(service.get_resume(first.id)).status == SUBJECT_ANALYZING


def test_analysis_in_flight_blocks_delete_and_retrigger(db, model_call):
    model_call('{"report": "r", "suggestions": []}')
    service = ResumeService(db, OWNER)
    resume = _resume(db)
    service.start_analysis(resume.id)

    with pytest.raises(ValueError):
        service.delete_resume(resume.id)
    with pytest.raises(ValueError):
        service.start_analysis(resume.id)


def test_restart_does_not_leave_a_resume_analyzing(db, model_call):
    """以前靠 reset_stale_analysis 单独收尾，现在运行的收尾就是业务状态的收尾。"""
    model_call('{"report": "r", "suggestions": []}')
    service = ResumeService(db, OWNER)
    resume = _resume(db)
    service.start_analysis(resume.id)

    reset_stale_runs(db)
    db.expire_all()

    done = service.to_item(service.get_resume(resume.id))
    assert done.status == SUBJECT_ERROR and STALE_RUN_ERROR in done.error_msg
    service.delete_resume(resume.id)  # 不再「分析中」，删得掉


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

    done = service.to_item(service.get_resume(resume.id))
    assert done.status == SUBJECT_ERROR and "上游 502" in done.error_msg
    # 业务行与运行记录得讲同一个故事，否则历史里会看到一次「成功」的分析。
    assert db.scalar(select(WorkflowRun)).status == "failed"
    assert db.scalar(select(WorkflowRunStep)).status == "failed"


def test_missing_builtin_workflow_fails_when_it_is_triggered(db, model_call):
    """缺定义是初始化坏了，在发起的那一刻就报错：以前是先落一行 analyzing、后台
    任务里才发现，留下一行永远转圈也删不掉的记录。"""
    calls = model_call(lambda: "不该被调用")
    db.execute(Workflow.__table__.delete())
    db.commit()

    service = ResumeService(db, OWNER)
    resume = _resume(db)
    with pytest.raises(BusinessError):
        service.start_analysis(resume.id)

    assert latest_run(db, OWNER, SUBJECT_RESUME, resume.id) is None
    assert service.to_item(service.get_resume(resume.id)).status == SUBJECT_UPLOADED
    assert calls == [], "缺定义时连模型都不该碰"


# ---- 对比走工作流 ------------------------------------------------------------


def test_run_comparison_executes_through_the_workflow(db, model_call):
    calls = model_call("对比结论：甲更合适")
    service = ResumeService(db, OWNER)
    first = _resume(db, title="甲", content="甲的正文")
    second = _resume(db, title="乙", content="乙的正文")
    comparison = service.create_comparison([first.id, second.id], title=None)

    assert service.to_comparison_item(comparison).status == SUBJECT_ANALYZING

    asyncio.run(service.run_comparison(comparison.id))
    _read_after_engine_writes(db)

    done = service.get_comparison(comparison.id)
    assert done.report == "对比结论：甲更合适"
    assert service.to_comparison_detail(done).status == SUBJECT_READY
    assert calls[0][0] == "resume-compare"
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.workflow_slug == "resume-comparison"))
    assert (run.subject_kind, run.subject_ref) == (SUBJECT_RESUME_COMPARISON, str(comparison.id))
    step = db.scalar(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id))
    assert "共 2 份" in step.prompt
    assert "=== 简历 1：甲 ===" in step.prompt and "=== 简历 2：乙 ===" in step.prompt
