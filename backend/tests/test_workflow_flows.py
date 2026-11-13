"""幻灯片、面试、求职助手、短视频四条功能链路走工作流引擎的集成测试。

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
from app.config import get_settings
from app.database import Base
from app.models.entities import (
    MaterialAsset,
    ModelConfig,
    VideoProject,
    WorkflowRun,
    WorkflowRunStep,
)
from app.models.schemas import (
    InterviewCreate,
    InterviewQuestionIn,
    ModelPurpose,
    ResumeToolkitCreate,
)
from app.services.materials import KIND_IMAGE
from app.services.workflows import exec as workflow_exec
from app.services.interview import InterviewService
from app.services.resume import ResumeService
from app.services.slides import SlideService
from app.services.videos.service import VideoService
from app.services.workflows.builtin import seed_builtin_workflows
from app.services.workflows.runs import STALE_RUN_ERROR, latest_run, reset_stale_runs
from app.services.workflows import (
    RUN_FAILED,
    RUN_SUCCEEDED,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    SUBJECT_ANALYZING,
    SUBJECT_ERROR,
    SUBJECT_INTERVIEW_QUESTION,
    SUBJECT_READY,
    SUBJECT_RESUME_TOOLKIT,
    SUBJECT_SLIDE_DECK,
    SUBJECT_VIDEO_PROJECT,
)

OWNER = 1
settings = get_settings()

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
    """把模型调用换成假的，并记下每次收到的 ``(agent_slug, prompt)``。

    ``reply_for`` 给字典时按人设 slug 取回复：短视频那条流程两步问的是两个人设，
    一份回复打到所有步骤那种写法对它没用。
    """
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
            reply = reply_for
            if isinstance(reply, dict):
                reply = reply.get(agent_slug, "")
            if isinstance(reply, Exception):
                raise reply
            return reply() if callable(reply) else reply

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


# ---- 短视频 -----------------------------------------------------------------


# 两步各答一份：创作卡七项与分镜五项都是步骤声明的输出契约，少一项都进不了下一步。
# ``structure`` 是人设里那个数组，别写成字符串——类型对不上是后处理挂，不是模型挂。
BRIEF_OUTPUT = (
    '{"hook_type":"悬念","hook":"三步做完季度复盘","audience":"普通打工人",'
    '"goal":"涨粉","structure":["问题","方案","结果"],"duration_s":30,'
    '"risk_notes":"别写公司名"}'
)
SCRIPT_OUTPUT = (
    '{"title":"季度复盘三步","narration":"整条口播文案",'
    '"tags":["复盘","职场"],"cover_prompt":"竖屏封面一张",'
    '"shots":[{"image_prompt":"第一镜画面","video_prompt":"镜头缓慢推近",'
    '"duration_s":6,"scene":"工位","on_screen_text":"三步"},'
    '{"image_prompt":"第二镜画面","duration_s":99}]}'
)
EMPTY_OUTPUT = '{"title":"空片","narration":"文案","tags":[],"cover_prompt":"封面","shots":[]}'


def _project(db, **overrides) -> VideoProject:
    fields = {
        "title": "季度复盘三步",
        "platform": "douyin",
        "idea": "教普通打工人三步做出老板看得懂的季度复盘",
        "duration_s": None,
        "requirement": None,
        "style": None,
        "params": None,
    }
    fields.update(overrides)
    return VideoService(db, OWNER).create_project(**fields)


def _steps_of(db, run) -> dict:
    """一条运行按步骤 key 取回现场；成功的那条才值得看步骤细节。"""
    assert run.status == RUN_SUCCEEDED, f"运行没跑成功：{run.error_msg}"
    return {
        row.key: row
        for row in db.scalars(
            select(WorkflowRunStep)
            .where(WorkflowRunStep.run_id == run.id)
            .order_by(WorkflowRunStep.id)
        ).all()
    }


def _latest(db, project_id: int):
    db.expire_all()
    return latest_run(db, OWNER, SUBJECT_VIDEO_PROJECT, project_id)


def test_video_script_runs_both_steps_through_the_workflow(db, model_call):
    """两步各留一份现场，前一步的产物按**对象**喂给后一步。"""
    calls = model_call({"video-briefer": BRIEF_OUTPUT, "video-storyboarder": SCRIPT_OUTPUT})
    service = VideoService(db, OWNER)
    project = _project(db)

    asyncio.run(service.run_script(project.id))
    _read_after_engine_writes(db)

    assert [slug for slug, _ in calls] == ["video-briefer", "video-storyboarder"]
    steps = _steps_of(db, _latest(db, project.id))
    assert list(steps) == ["brief", "script"]
    assert steps["brief"].prompt == calls[0][1]
    # 跨步传的是按契约筛过的对象，不是模型原文：下游 prompt 里应当看得见结构本身。
    assert '"hook_type"' in steps["script"].prompt
    assert '"structure"' in steps["script"].prompt
    assert steps["script"].prompt == calls[1][1]

    db.expire_all()
    done = service.get_project(project.id)
    assert done.brief["hook"] == "三步做完季度复盘"
    assert done.brief["structure"] == ["问题", "方案", "结果"]
    assert done.script == "整条口播文案"
    assert done.publish["tags"] == ["复盘", "职场"]
    shots = service.shots_of(project.id)
    assert [row.image_prompt for row in shots] == ["第一镜画面", "第二镜画面"]
    # 模型给 99 秒就得夹回渲染档的上限：那条镜头根本发不出去。
    assert [row.duration_s for row in shots] == [6, 15]


def test_video_script_prompt_only_says_what_was_filled_in(db, model_call):
    """没填的段落整段不进 prompt：模板只管替换，拼装还是代码的事。"""
    model_call({"video-briefer": BRIEF_OUTPUT, "video-storyboarder": SCRIPT_OUTPUT})
    service = VideoService(db, OWNER)
    project = _project(db, title="只有想法", duration_s=None, requirement=None, style=None)

    asyncio.run(service.run_script(project.id))
    _read_after_engine_writes(db)

    steps = _steps_of(db, _latest(db, project.id))
    assert "想法：" in steps["brief"].prompt
    assert "发布平台：抖音" in steps["brief"].prompt
    assert "用户的补充要求" not in steps["brief"].prompt, "空段落不该留在 prompt 里"
    assert "全片风格" not in steps["brief"].prompt
    assert "目标时长" not in steps["brief"].prompt
    assert "可用参考图" not in steps["script"].prompt


def test_video_script_never_sees_the_material_library(db, model_call):
    """库是账号级的，模型没有依据替人挑素材：重新生成脚本时也不给素材清单。

    旧的「项目参考图清单」会进分镜那一步的 prompt，模型于是能抄到 id；现在素材由
    人在工作台上按镜头挑，所以库里明明有这条图，prompt 里也不该出现它。
    """
    model_call({"video-briefer": BRIEF_OUTPUT, "video-storyboarder": SCRIPT_OUTPUT})
    service = VideoService(db, OWNER)
    project = _project(db, requirement="只要前 3 秒")
    # 直接落一行而不是走 store_material：这一步断言的是 prompt 里有什么，没有一处会读那个文件。
    db.add(
        MaterialAsset(
            owner_id=OWNER, kind=KIND_IMAGE, name="主角形象.png", mime="image/png",
            size=32, rel_path=f"materials/{OWNER}/0123456789abcdef.png",
        )
    )
    db.commit()
    asyncio.run(service.run_script(project.id))
    # 重新生成才是「库里已经有素材」的那一刻：走 start_script 那次重新快照的输入。
    service.start_script(project.id)
    asyncio.run(service.run_script(project.id))
    _read_after_engine_writes(db)

    steps = _steps_of(db, _latest(db, project.id))
    assert "可用参考图" not in steps["script"].prompt
    assert "主角形象.png" not in steps["script"].prompt
    # 重新生成用的是这一刻的输入：上一轮里那条补充要求还在。
    assert "用户的补充要求：只要前 3 秒" in steps["brief"].prompt


def test_video_script_skips_the_second_step_when_the_brief_is_broken(db, model_call):
    """创作卡缺字段：流程停在这一步，后一步是「没跑到」而不是「跑挂了」。"""
    calls = model_call({"video-briefer": '{"hook":"只给一个字段"}'})
    service = VideoService(db, OWNER)
    project = _project(db)

    asyncio.run(service.run_script(project.id))
    _read_after_engine_writes(db)

    assert [slug for slug, _ in calls] == ["video-briefer"]
    run = _latest(db, project.id)
    assert run.status == RUN_FAILED and "缺少字段" in run.error_msg
    assert {row.key: row.status for row in db.scalars(
        select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
    ).all()} == {"brief": STEP_FAILED, "script": STEP_SKIPPED}


def test_video_script_without_any_shot_is_downgraded(db, model_call):
    """两步都成功却一镜都没落下来：运行得跟着降级，不然界面就是「已完成」没有分镜。"""
    model_call({"video-briefer": BRIEF_OUTPUT, "video-storyboarder": EMPTY_OUTPUT})
    service = VideoService(db, OWNER)
    project = _project(db)

    asyncio.run(service.run_script(project.id))
    _read_after_engine_writes(db)

    run = _latest(db, project.id)
    assert run.status == RUN_FAILED and "合法镜头" in run.error_msg
    assert service.shots_of(project.id) == []
    item = service.to_item(service.get_project(project.id))
    assert item.status == SUBJECT_ERROR and "合法镜头" in item.error_msg
