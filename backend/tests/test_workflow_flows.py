"""幻灯片、面试、求职助手三条功能链路走工作流引擎的集成测试。

迁移的验收点始终是同一句话：模型调用由引擎执行，现场（渲染后的 prompt、原始
输出、每步状态）落进 ``workflow_run`` / ``workflow_run_step``，而业务行上的进度
与它讲同一个故事。区别在于这个故事现在**只有运行记录一份**：业务行只留产物，
``status`` / ``error_msg`` 是序列化时从认领了该对象的那次运行翻译出来的（见
``services/workflows/runs.py`` 的 ``subject_state``）。模型调用打桩（替换
``app.services.workflows.exec.run_agent``），不碰真实服务商。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.bootstrap import RETIRED_STATUS_COLUMNS
from app.database import Base
from app.models.entities import ModelConfig, WorkflowRun, WorkflowRunStep
from app.models.schemas import (
    InterviewCreate,
    InterviewQuestionIn,
    ModelPurpose,
    ResumeToolkitCreate,
)
from app.services.workflows import exec as workflow_exec
from app.services.interview import InterviewService
from app.services.resume import ResumeService
from app.services.slides import SlideService
from app.services.workflows.builtin import seed_builtin_workflows
from app.services.workflows.runs import STALE_RUN_ERROR, reset_stale_runs
from app.services.workflows import (
    RUN_FAILED,
    RUN_SUCCEEDED,
    STEP_SUCCEEDED,
    SUBJECT_ANALYZING,
    SUBJECT_ERROR,
    SUBJECT_INTERVIEW_QUESTION,
    SUBJECT_READY,
    SUBJECT_RESUME_TOOLKIT,
    SUBJECT_SLIDE_DECK,
)

OWNER = 1

DECK_OUTPUT = (
    '```json\n{"theme":{"preset":"paper","ratio":"16x9","accent":"#123456"},'
    '"slides":[{"layout":"cover","title":"复盘封面"},'
    '{"layout":"bullets","title":"结论","bullets":["增长 12%"]}]}\n```'
)

# 每类求职助手任务一份最小可用输入：够过各自的必填校验就行。
TOOLKIT_CASES = {
    "optimize": ResumeToolkitCreate(kind="optimize", resume_text="十年 Python", position="平台方向"),
    "career-match": ResumeToolkitCreate(kind="career-match", background="会 SQL 与运维"),
    "jd-match": ResumeToolkitCreate(
        kind="jd-match", resume_text="十年 Python", job_description="招后端一名"
    ),
    "interview-prep": ResumeToolkitCreate(
        kind="interview-prep", position="高级后端", job_description="招后端一名"
    ),
    "portfolio-plan": ResumeToolkitCreate(
        kind="portfolio-plan", position="高级后端", background="两个开源项目"
    ),
    "salary-negotiation": ResumeToolkitCreate(
        kind="salary-negotiation", offer_amount="40 万", notes="另一家给得更高"
    ),
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """文件库 + 可用的对话模型 + seed 过的内置工作流。

    用文件库而不是内存库：引擎里每个步骤自开一个 session，共享一条连接的内存库
    会让它们互相看不见对方写的行。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'flows.db'}", connect_args={"check_same_thread": False}
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


def _read_after_engine_writes(db):
    """结束本会话的读事务：步骤写在另一条连接上，不重开会读到旧快照。"""
    db.rollback()


def _run(db, slug: str) -> WorkflowRun:
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.workflow_slug == slug))
    assert run is not None, f"{slug} 没有留下运行记录"
    return run


def _only_step(db, slug: str) -> WorkflowRunStep:
    run = _run(db, slug)
    assert run.status == RUN_SUCCEEDED and run.error_msg is None, f"{slug} 没有留下成功的运行"
    return db.scalar(
        select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id).order_by(WorkflowRunStep.id)
    )


# ---- 幻灯片 -------------------------------------------------------------------


def test_business_tables_declare_no_status_column():
    """ORM 里再长出这些列就是写回去了：启动时那条删列 DDL 会把功能直接打断。"""
    for table, retired in RETIRED_STATUS_COLUMNS.items():
        columns = {column.name for column in Base.metadata.tables[table].columns}
        assert not set(retired) & columns, f"{table} 又有了 {retired}"


def _deck(db):
    return SlideService(db, OWNER).create_deck(
        title="季度复盘",
        description="给管理层的季度复盘",
        requirement="只要三页",
        source_text="材料正文：营收增长 12%",
    )


def test_slide_generation_runs_through_the_workflow(db, model_call):
    calls = model_call(DECK_OUTPUT)
    service = SlideService(db, OWNER)
    deck = _deck(db)

    asyncio.run(service.run_generation(deck.id))
    _read_after_engine_writes(db)

    done = service.to_item(service.get_deck(deck.id))
    assert done.status == SUBJECT_READY and done.error_msg is None
    detail = service.to_detail(service.get_deck(deck.id))
    assert [page.layout for page in detail.slides] == ["cover", "bullets"]
    assert detail.theme.accent == "#123456"
    assert "增长 12%" in service.get_deck(deck.id).html
    assert calls[0][0] == "slide-architect"

    step = _only_step(db, "slide-generation")
    assert step.key == "compose" and step.prompt == calls[0][1]
    assert "主题：季度复盘" in step.prompt
    assert "补充说明：给管理层的季度复盘" in step.prompt
    assert "用户对这份幻灯片的要求：只要三页" in step.prompt
    assert "材料正文：营收增长 12%" in step.prompt
    assert "请依据以上材料产出整份幻灯片的 JSON 结构。" in step.prompt


def test_slide_generation_drops_empty_content_pages(db, model_call):
    """模型给过一个 steps 数组为空、只剩标题和结论句的页：那种页收进 deck 就是白页。"""
    model_call(
        '{"theme":{"preset":"amber"},'
        '"slides":[{"layout":"cover","title":"复盘封面"},'
        '{"layout":"steps","title":"空壳","steps":[],"takeaway":"一句话"},'
        '{"layout":"steps","title":"有内容","steps":[{"title":"第一步"}]}]}'
    )
    service = SlideService(db, OWNER)
    deck = _deck(db)

    asyncio.run(service.run_generation(deck.id))
    _read_after_engine_writes(db)

    detail = service.to_detail(service.get_deck(deck.id))
    assert [page.title for page in detail.slides] == ["复盘封面", "有内容"]
    assert detail.theme.preset == "amber"


def test_created_deck_belongs_to_its_run(db, model_call):
    """创建时就把运行建出来：接口一返回，界面上的「生成中」已经有了事实源。"""
    model_call(DECK_OUTPUT)
    service = SlideService(db, OWNER)
    deck = _deck(db)

    run = _run(db, "slide-generation")
    assert (run.subject_kind, run.subject_ref) == (SUBJECT_SLIDE_DECK, str(deck.id))
    # 归属带着名字：运行流水里也得说得出「这次是给哪份 deck 跑的」。
    assert run.subject_label == "季度复盘"
    assert service.to_item(deck).status == SUBJECT_ANALYZING
    with pytest.raises(ValueError):
        service.delete_deck(deck.id)


def test_restart_does_not_leave_a_deck_generating(db, model_call):
    """重启遗留的运行收尾之后，业务行的状态跟着变，不再需要另一套 stale 清理。"""
    model_call(DECK_OUTPUT)
    service = SlideService(db, OWNER)
    deck = _deck(db)

    reset_stale_runs(db)
    db.expire_all()

    item = service.to_item(service.get_deck(deck.id))
    assert item.status == SUBJECT_ERROR and STALE_RUN_ERROR in item.error_msg
    service.delete_deck(deck.id)  # 已经不在跑，删得掉


def test_slide_generation_without_optional_sections_keps_them_out(db, model_call):
    model_call(DECK_OUTPUT)
    service = SlideService(db, OWNER)
    deck = service.create_deck(
        title="只有材料", description=None, requirement=None, source_text="正文"
    )

    asyncio.run(service.run_generation(deck.id))
    _read_after_engine_writes(db)

    step = _only_step(db, "slide-generation")
    assert "补充说明" not in step.prompt and "要求" not in step.prompt
    assert "可用配图" not in step.prompt, "没有配图时那段清单不该出现"


def test_slide_generation_marks_error_when_the_workflow_fails(db, model_call):
    model_call(RuntimeError("上游 502"))
    service = SlideService(db, OWNER)
    deck = _deck(db)

    asyncio.run(service.run_generation(deck.id))
    _read_after_engine_writes(db)

    done = service.to_item(service.get_deck(deck.id))
    assert done.status == SUBJECT_ERROR and "上游 502" in done.error_msg
    run = _run(db, "slide-generation")
    # 业务行与运行记录得讲同一个故事，别在历史里留下一次「成功」的生成。
    assert run.status == RUN_FAILED


def test_slide_generation_rejects_output_without_pages(db, model_call):
    """后处理挂了：运行本身每一步都成功，仍要被降级，否则就是「已完成但什么都没有」。"""
    model_call('{"slides": []}')
    service = SlideService(db, OWNER)
    deck = _deck(db)

    asyncio.run(service.run_generation(deck.id))
    _read_after_engine_writes(db)

    done = service.to_item(service.get_deck(deck.id))
    assert done.status == SUBJECT_ERROR and "合法页面" in done.error_msg
    run = _run(db, "slide-generation")
    assert run.status == RUN_FAILED and "合法页面" in run.error_msg
    step = db.scalar(
        select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
    )
    assert step.status == STEP_SUCCEEDED, "模型那一步是成功的，现场要留着"


# ---- 面试 ---------------------------------------------------------------------


def _interview(db):
    return InterviewService(db, OWNER).create_record(
        InterviewCreate(
            company="示例科技",
            position="高级后端工程师",
            round="二面",
            questions=[
                InterviewQuestionIn(
                    question="讲讲你做过的高并发优化", my_answer="我当时答了缓存预热"
                )
            ],
        )
    )


def test_interview_answer_runs_through_the_workflow(db, model_call):
    calls = model_call("参考答案：先从缓存预热说起")
    service = InterviewService(db, OWNER)
    record = _interview(db)
    qid = record.questions[0]["qid"]
    service.start_answer(record.id, qid)

    asyncio.run(service.run_answer(record.id, qid))
    _read_after_engine_writes(db)

    question = service.to_detail(service.get_record(record.id)).questions[0]
    assert question.ref_status == SUBJECT_READY
    assert question.ref_answer == "参考答案：先从缓存预热说起"
    assert calls[0][0] == "interview-answerer"

    # 一道题没有自己的行，认领它靠的是 qid 字符串。
    run = _run(db, "interview-answer")
    assert (run.subject_kind, run.subject_ref) == (SUBJECT_INTERVIEW_QUESTION, qid)
    # 题干在记录行的 JSON 里，反查不到，所以发起时把它抄成归属名字。
    assert run.subject_label == "示例科技 · 高级后端工程师 · 讲讲你做过的高并发优化"

    step = _only_step(db, "interview-answer")
    assert step.key == "answer" and step.prompt == calls[0][1]
    assert "应聘公司：示例科技" in step.prompt
    assert "面试轮次：二面" in step.prompt
    assert "讲讲你做过的高并发优化" in step.prompt
    assert "求职者当时的回答" in step.prompt


def test_generated_question_blocks_deleting_the_record(db, model_call):
    model_call("参考答案")
    service = InterviewService(db, OWNER)
    record = _interview(db)
    qid = record.questions[0]["qid"]
    service.start_answer(record.id, qid)

    with pytest.raises(ValueError):
        service.delete_question(record.id, qid)
    with pytest.raises(ValueError):
        service.delete_record(record.id)
    with pytest.raises(ValueError):
        service.start_answer(record.id, qid)


def test_interview_answer_without_my_answer_drops_that_section(db, model_call):
    model_call("参考答案")
    service = InterviewService(db, OWNER)
    record = service.create_record(
        InterviewCreate(
            company="示例科技",
            position="后端工程师",
            questions=[InterviewQuestionIn(question="你的技术栈是什么？")],
        )
    )
    service.start_answer(record.id, record.questions[0]["qid"])

    asyncio.run(service.run_answer(record.id, record.questions[0]["qid"]))
    _read_after_engine_writes(db)

    step = _only_step(db, "interview-answer")
    assert "面试轮次" not in step.prompt
    assert "求职者当时的回答" not in step.prompt


def test_interview_answer_records_model_failure(db, model_call):
    model_call(RuntimeError("上游 502"))
    service = InterviewService(db, OWNER)
    record = _interview(db)
    qid = record.questions[0]["qid"]
    service.start_answer(record.id, qid)

    asyncio.run(service.run_answer(record.id, qid))
    _read_after_engine_writes(db)

    question = service.to_detail(service.get_record(record.id)).questions[0]
    assert question.ref_status == SUBJECT_ERROR and "上游 502" in question.ref_error
    assert _run(db, "interview-answer").status == RUN_FAILED


def test_interview_answer_empty_reply_is_a_failure(db, model_call):
    """模型点头却不作答：后处理判空，运行同样得降级成失败。"""
    model_call("   ")
    service = InterviewService(db, OWNER)
    record = _interview(db)
    qid = record.questions[0]["qid"]
    service.start_answer(record.id, qid)

    asyncio.run(service.run_answer(record.id, qid))
    _read_after_engine_writes(db)

    question = service.to_detail(service.get_record(record.id)).questions[0]
    assert question.ref_status == SUBJECT_ERROR and "空回答" in question.ref_error
    assert service.get_record(record.id).questions[0]["ref_answer"] is None


# ---- 求职助手 -----------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(TOOLKIT_CASES))
def test_toolkit_task_runs_through_its_workflow(db, model_call, kind):
    calls = model_call("一份可用的报告")
    service = ResumeService(db, OWNER)
    task = service.create_toolkit_task(TOOLKIT_CASES[kind])

    asyncio.run(service.run_toolkit_task(task.id))
    _read_after_engine_writes(db)

    done = service.to_toolkit_detail(service.get_toolkit_task(task.id))
    assert done.status == SUBJECT_READY and done.report == "一份可用的报告"
    assert calls[0][0]  # 步骤引用的人设真的解析出来了

    run = _run(db, f"toolkit-{kind}")
    assert (run.subject_kind, run.subject_ref) == (SUBJECT_RESUME_TOOLKIT, str(task.id))
    assert run.subject_label == task.title

    step = _only_step(db, f"toolkit-{kind}")
    assert step.key == "report" and step.prompt == calls[0][1]


def test_toolkit_optimize_without_position_drops_that_line(db, model_call):
    model_call("报告")
    service = ResumeService(db, OWNER)
    task = service.create_toolkit_task(
        ResumeToolkitCreate(kind="optimize", resume_text="十年 Python")
    )

    asyncio.run(service.run_toolkit_task(task.id))
    _read_after_engine_writes(db)

    step = _only_step(db, "toolkit-optimize")
    assert step.prompt.startswith("简历全文："), "没有目标岗位就不该留那一句"


def test_toolkit_career_match_joins_resume_and_background(db, model_call):
    model_call("报告")
    service = ResumeService(db, OWNER)
    task = service.create_toolkit_task(
        ResumeToolkitCreate(
            kind="career-match", resume_text="十年 Python", background="业余做运维"
        )
    )

    asyncio.run(service.run_toolkit_task(task.id))
    _read_after_engine_writes(db)

    step = _only_step(db, "toolkit-career-match")
    assert "简历全文：" in step.prompt and "个人背景补充：" in step.prompt


def test_toolkit_task_marks_error_when_the_workflow_fails(db, model_call):
    model_call(RuntimeError("上游 502"))
    service = ResumeService(db, OWNER)
    task = service.create_toolkit_task(TOOLKIT_CASES["salary-negotiation"])

    asyncio.run(service.run_toolkit_task(task.id))
    _read_after_engine_writes(db)

    done = service.to_toolkit_item(service.get_toolkit_task(task.id))
    assert done.status == SUBJECT_ERROR and "上游 502" in done.error_msg
    assert _run(db, "toolkit-salary-negotiation").status == RUN_FAILED
