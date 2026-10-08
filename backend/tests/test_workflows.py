"""工作流引擎：定义 seed、输入校验、分步执行与运行现场。

跑真实的 SQLite（临时文件库）与假的模型调用：要验证的是编排、落库与状态收敛，
不是上游模型；每次调用被打桩成返回固定文本，快，而且能断言 prompt 到底是什么。

``execute_run`` 里每个步骤自开一个 session（并发写各自的行），所以这里必须把
``app.database.SessionLocal`` 换到同一个临时库上，否则步骤会去写真实部署库。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.errors import BusinessError
from app.models.entities import (
    Agent,
    McpServer,
    ModelConfig,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowStep,
)
from app.models.schemas import ModelPurpose, WorkflowCreate, WorkflowUpdate
from app.services import workflows as workflows_module
from app.services.workflows import (
    BUILTIN_WORKFLOWS,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STALE_RUN_ERROR,
    SUBJECT_ERROR,
    SUBJECT_LABEL_MAX,
    SUBJECT_READY,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    WorkflowService,
    _group_order,
    execute_run,
    follow_run,
    prepare_step_rerun,
    prune_runs,
    render_prompt,
    stream_run,
    rerun_step,
    reset_stale_runs,
    seed_builtin_workflows,
    subject_state,
)

OWNER = 1


# ---- fixtures 与小工具 -------------------------------------------------------


@pytest.fixture()
def engine(tmp_path):
    # 文件库而不是内存库：步骤们各开各的 session，共享一条连接的内存库会让它们
    # 互相看不见对方写的行。
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'workflows.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture()
def db(engine, monkeypatch):
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    # 引擎内部按 SessionLocal() 取会话，不替换掉它测试就会写到真实库里。
    monkeypatch.setattr("app.database.SessionLocal", maker)
    try:
        yield session
    finally:
        session.close()


def _chat_model(db, owner_id: int = OWNER) -> ModelConfig:
    config = ModelConfig(
        owner_id=owner_id,
        purpose=ModelPurpose.CHAT.value,
        title="测试对话模型",
        model_name="unit-test",
        base_url="http://localhost:1/v1",
        is_default=True,
        last_test_ok=True,
    )
    db.add(config)
    db.commit()
    return config


def _stub_model(monkeypatch, reply_for):
    """把一次模型调用换成假实现：``reply_for(agent_slug)`` 给文本，给异常就抛。

    返回的 list 记录 ``(agent_slug, 渲染后的 prompt)``，用来断言模板渲染与跨步
    传参真的发生了。
    """
    calls: list[tuple[str, str]] = []

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
    ):
        calls.append((agent_slug, user_prompt))
        reply = reply_for(agent_slug)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(workflows_module, "run_agent", fake_run_agent)
    return calls


def _run_all(db, owner_id: int, run_id: int) -> list[tuple[str, dict]]:
    """跑完一次运行并返回事件列表。

    结束前后各 rollback 一次：步骤写的是另一条连接，而 SQLite 的读事务从第一次
    SELECT 起就固定住快照，不重开事务的话这里读到的还是运行开始前的旧状态。
    """
    async def collect():
        return [
            (event, payload)
            async for event, payload in execute_run(owner_id=owner_id, run_id=run_id)
        ]

    events = asyncio.run(collect())
    db.rollback()
    return events


def _rerun(db, owner_id: int, run_id: int, key: str) -> None:
    """跑完 ``rerun_step`` 这个后台任务，事务处理与 :func:`_run_all` 同一套。"""

    async def drive():
        await rerun_step(owner_id=owner_id, run_id=run_id, key=key)

    db.rollback()
    asyncio.run(drive())
    db.rollback()


def _follow_all(db, run_id: int, mutate, *, owner_id: int = OWNER, poll_seconds: float = 0.02):
    """跟着一次运行读，同时让 ``mutate`` 在另一条连接上把现状往前推。

    ``follow_run`` 自己不执行任何东西，进度全靠读库，所以这里模拟的就是「另一个
    worker 正在写这些行」。写必须放在线程里：跟随的那条连接两次读之间是开着读事务
    的，SQLite 此时只让别人排队等锁，而同步的写会把事件循环一起堵住——谁也解不了
    这个锁，测试只会等到超时。
    """

    async def drive():
        polls = 0

        async def watchdog():
            nonlocal polls
            polls += 1
            return polls > 300  # 现状没推进也别让测试挂着不动

        return [
            (event, payload)
            async for event, payload in follow_run(
                owner_id=owner_id,
                run_id=run_id,
                is_disconnected=watchdog,
                poll_seconds=poll_seconds,
            )
        ]

    async def main():
        # gather 必须在运行中的事件循环里创建，否则它抓住的是别处的循环。
        return await asyncio.gather(drive(), asyncio.to_thread(mutate))

    db.rollback()
    events, _ = asyncio.run(main())
    db.rollback()
    return events


def _seeded(db) -> dict[str, Workflow]:
    seed_builtin_workflows(db)
    return {row.slug: row for row in db.scalars(select(Workflow)).all()}


def _builtin(db, slug: str) -> Workflow:
    return db.scalar(select(Workflow).where(Workflow.slug == slug))


def _two_step_workflow(
    db, *, first_group: str = "draft", second_group: str = "score", second_tpl: str = None
):
    """一条两步流程：第一步的输出作为变量喂给第二步。"""
    workflow = Workflow(
        owner_id=OWNER,
        slug="write-and-score",
        name="先写后评",
        input_schema=[{"name": "topic", "label": "主题", "required": True}],
    )
    db.add(workflow)
    db.flush()
    db.add_all(
        [
            WorkflowStep(
                workflow_id=workflow.id,
                key="draft",
                group_key=first_group,
                sort_order=10,
                agent_slug="writer",
                prompt_tpl="就{{topic}}写一段话",
                output_var="draft",
            ),
            WorkflowStep(
                workflow_id=workflow.id,
                key="score",
                group_key=second_group,
                sort_order=20,
                agent_slug="scorer",
                prompt_tpl=second_tpl or "给这篇打分：{{draft}}",
            ),
        ]
    )
    db.commit()
    return workflow


def _template_vars(prompt_tpl: str) -> set[str]:
    return {match.group(1) for match in workflows_module._PLACEHOLDER.finditer(prompt_tpl)}


def _scene(
    db, run_id: int, run_status: str, run_error: str | None = None, **steps: tuple[str, str | None]
) -> None:
    """直接把一次运行的现状写成测试要的样子：``步骤 key=(状态, 输出)``。

    跟随与重跑关心的都是「库里已经有什么」，跑一遍真实执行只会让这两个场景更难
    读；执行本身在上面的 ``execute_run`` 用例里已经钉过。
    """
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .values(status=run_status, error_msg=run_error)
    )
    for key, (status, output) in steps.items():
        db.execute(
            update(WorkflowRunStep)
            .where(WorkflowRunStep.run_id == run_id, WorkflowRunStep.key == key)
            .values(status=status, output=output)
        )
    db.commit()


def _write_run(run_id: int, **values) -> None:
    """在**另一条连接**上改运行行：模拟正在跑它的那个 worker。"""
    from app.database import SessionLocal

    with SessionLocal() as other:
        other.execute(update(WorkflowRun).where(WorkflowRun.id == run_id).values(**values))
        other.commit()


def _write_step(run_id: int, key: str, **values) -> None:
    from app.database import SessionLocal

    with SessionLocal() as other:
        other.execute(
            update(WorkflowRunStep)
            .where(WorkflowRunStep.run_id == run_id, WorkflowRunStep.key == key)
            .values(**values)
        )
        other.commit()


def _aged(db, run_id: int, days_ago: int = 40) -> None:
    """把一条运行的创建时间推到保留窗口之外（``created_at`` 没有 onupdate）。"""
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .values(created_at=datetime.now() - timedelta(days=days_ago))
    )
    db.commit()


# ---- 模板渲染 ---------------------------------------------------------------


def test_render_prompt_substitutes_values():
    assert render_prompt("共 {{count}} 份：{{block}}", {"count": "3", "block": "正文"}) == "共 3 份：正文"


def test_render_prompt_substitutes_a_json_product_as_json_text():
    """整段引用 JSON 步的产物：喂的是对象序列化出来的文本，不是 Python 的 repr。"""
    assert render_prompt("{{score}}", {"score": {"total": 8, "ok": True}}) == '{"total": 8, "ok": true}'


def test_render_prompt_reads_fields_out_of_a_parsed_product():
    """``{{x.字段}}``：从解析出来的对象里取值，支持嵌套对象与数组下标。"""
    context = {"draft": {"summary": "一段摘要", "tags": ["a", "b"], "meta": {"lang": "zh"}}}

    assert render_prompt(
        "{{draft.summary}}｜{{draft.tags.1}}｜{{draft.meta.lang}}", context
    ) == "一段摘要｜b｜zh"


def test_render_prompt_keeps_a_null_field_as_empty_text():
    """JSON 里的 null 是「有这个字段、没有值」，不是引用错了变量。"""
    assert render_prompt("[{{draft.summary}}]", {"draft": {"summary": None}}) == "[]"


def test_render_prompt_rejects_a_field_the_product_does_not_have():
    with pytest.raises(ValueError, match="draft.summary"):
        render_prompt("{{draft.summary}}", {"draft": {"other": "x"}})
    # 整段文本没有字段可取：说清是路径走不通，而不是变量不存在。
    with pytest.raises(ValueError, match="没有这个字段"):
        render_prompt("{{draft.summary}}", {"draft": "整段初稿"})


def test_render_prompt_rejects_unknown_variable():
    # 少喂一段简历全文，模型照样能编出一份看着完整的报告——所以必须报错而不是替换成空串。
    with pytest.raises(ValueError, match="未定义的变量 resume_content"):
        render_prompt("{{resume_content}}", {"other": "x"})
    with pytest.raises(ValueError, match="未定义的变量 other"):
        render_prompt("{{other.field}}", {"draft": "整段初稿"})


def test_group_order_splits_repeated_group_name():
    class Step:
        def __init__(self, key, group):
            self.key, self.group_key = key, group

    # 相邻且同名才算一组：被别的组隔开时就是两组，各自顺序执行——
    # 与界面「画出来几列就是几段」同一口径（保存那边也会拦这种命名）。
    groups = _group_order([Step("a", "g1"), Step("b", "g2"), Step("c", "g1")])
    assert [(name, [s.key for s in members]) for name, members in groups] == [
        ("g1", ["a"]),
        ("g2", ["b"]),
        ("g1", ["c"]),
    ]
    assert _group_order([]) == []


# ---- seed -------------------------------------------------------------------


def test_seed_creates_builtin_workflows_and_steps(db):
    workflows = _seeded(db)
    assert set(workflows) == {builtin.slug for builtin in BUILTIN_WORKFLOWS}
    for row in workflows.values():
        assert row.owner_id == 0 and row.is_builtin
        assert row.input_schema, "声明的输入变量要落库，接口才知道收哪些字段"
        declared = {field["name"] for field in row.input_schema}
        steps = db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_id == row.id)).all()
        assert steps
        for step in steps:
            # 模板里用到的变量必须都声明过，否则运行时必然渲染失败。
            assert _template_vars(step.prompt_tpl) <= declared


def test_seed_is_idempotent_and_refreshes_prompt(db):
    seed_builtin_workflows(db)
    first = db.scalar(select(WorkflowStep).where(WorkflowStep.key == "analyze"))
    step_id = first.id
    first.prompt_tpl = "旧的模板 {{resume_content}}"
    db.commit()

    seed_builtin_workflows(db)
    refreshed = db.scalar(select(WorkflowStep).where(WorkflowStep.key == "analyze"))
    assert refreshed.id == step_id, "按 key 对齐就地更新，不是删了重建"
    assert "简历全文" in refreshed.prompt_tpl
    assert db.scalar(select(func.count()).select_from(Workflow)) == len(BUILTIN_WORKFLOWS)


def test_seed_keeps_disable_choice(db):
    seed_builtin_workflows(db)
    row = _builtin(db, "resume-analysis")
    row.enabled = False
    db.commit()

    seed_builtin_workflows(db)
    db.refresh(row)
    assert row.enabled is False, "藏起来是用户自己的决定，不该被一次部署改回去"


def test_seed_removes_step_deleted_from_code(db):
    seed_builtin_workflows(db)
    row = _builtin(db, "resume-analysis")
    db.add(
        WorkflowStep(
            workflow_id=row.id,
            key="legacy",
            group_key="legacy",
            sort_order=99,
            agent_slug="resume-analyzer",
            prompt_tpl="旧的",
        )
    )
    db.commit()

    seed_builtin_workflows(db)
    keys = {
        step.key
        for step in db.scalars(select(WorkflowStep).where(WorkflowStep.workflow_id == row.id)).all()
    }
    assert keys == {"analyze"}


# ---- 定义存取与输入校验 -------------------------------------------------------


def test_require_by_slug_ignores_enabled_for_builtin(db):
    """内置流程是功能自己的流水线：用户在界面上停用它，功能照常得能跑。"""
    seed_builtin_workflows(db)
    row = _builtin(db, "resume-analysis")
    row.enabled = False
    db.commit()

    assert WorkflowService(db, OWNER).require_by_slug("resume-analysis").id == row.id


def test_require_by_slug_missing_is_initialisation_error(db):
    with pytest.raises(BusinessError):
        WorkflowService(db, OWNER).require_by_slug("resume-analysis")


def test_get_hides_other_owners_workflow(db):
    other = _two_step_workflow(db)
    # 别人的 id 当不存在，而不是拒绝：确认它存在本身就是泄露。
    with pytest.raises(LookupError):
        WorkflowService(db, OWNER + 1).get(other.id)


def test_create_run_requires_declared_inputs(db):
    seed_builtin_workflows(db)
    workflow = _builtin(db, "resume-analysis")
    with pytest.raises(ValueError, match="简历全文"):
        WorkflowService(db, OWNER).create_run(workflow.id, {"resume_content": "  "})


def test_create_run_rejects_unknown_input_name(db):
    seed_builtin_workflows(db)
    workflow = _builtin(db, "resume-analysis")
    with pytest.raises(ValueError, match="未知的输入变量"):
        WorkflowService(db, OWNER).create_run(
            workflow.id, {"resume_content": "正文", "resume_context": "拼错的变量名"}
        )


def test_create_run_creates_every_step_row_as_pending(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    rows = WorkflowService(db, OWNER).run_steps(run.id)
    assert [row.key for row in rows] == ["draft", "score"]
    assert all(row.status == STEP_PENDING for row in rows)
    assert all(row.owner_id == OWNER for row in rows)
    assert run.inputs == {"topic": "编排"}


def test_run_subject_label_is_a_truncated_snapshot(db):
    """归属名字是发起时抄下的快照：超长截到列宽，空串归一成「没有归属」。

    各功能的标题字段长度谁也没保证（面试题的题干拼在名字后面），集中在这儿截，
    调用方才不会漏一份而把插入撞成 MySQL 的长度错误。
    """
    workflow = _two_step_workflow(db)
    service = WorkflowService(db, OWNER)

    run = service.create_run(
        workflow.id,
        {"topic": "编排"},
        subject_kind="resume",
        subject_ref=7,
        subject_label=f"  {'长' * (SUBJECT_LABEL_MAX + 40)}  ",
    )
    assert (run.subject_kind, run.subject_ref) == ("resume", "7")
    assert len(run.subject_label) == SUBJECT_LABEL_MAX

    blank = service.create_run(workflow.id, {"topic": "编排"}, subject_label="   ")
    assert blank.subject_label is None


def test_list_runs_filters_by_subject_pair(db):
    """整数 id 在各张表上各自编号：只按 ref 筛会把两个对象混到一起。"""
    workflow = _two_step_workflow(db)
    service = WorkflowService(db, OWNER)
    resume_run = service.create_run(
        workflow.id, {"topic": "编排"}, subject_kind="resume", subject_ref=1
    )
    service.create_run(workflow.id, {"topic": "编排"}, subject_kind="slide-deck", subject_ref=1)
    service.create_run(workflow.id, {"topic": "编排"})

    runs, total = service.list_runs(subject=("resume", 1))
    assert [run.id for run in runs] == [resume_run.id]
    assert total == 1
    # 不传 subject 时仍是跨对象的流水，工作流页面用得上。
    _, total = service.list_runs()
    assert total == 3


def test_list_runs_pages_and_filters_by_status(db):
    """总数是符合条件的那批，翻页不改它——否则「共 3 次」翻一页就变成「共 2 次」。"""
    workflow = _two_step_workflow(db)
    service = WorkflowService(db, OWNER)
    ids = [service.create_run(workflow.id, {"topic": "编排"}).id for _ in range(3)]
    db.execute(update(WorkflowRun).where(WorkflowRun.id == ids[0]).values(status=RUN_SUCCEEDED))
    db.commit()

    first, total = service.list_runs(page=1, page_size=2)
    second, same_total = service.list_runs(page=2, page_size=2)
    assert total == same_total == 3
    assert [run.id for run in first] == list(reversed(ids))[:2]
    assert [run.id for run in second] == [ids[0]]

    done, done_total = service.list_runs(run_status=RUN_SUCCEEDED)
    assert [run.id for run in done] == [ids[0]] and done_total == 1
    _, pending_total = service.list_runs(run_status="pending")
    assert pending_total == 2


# ---- 定义编辑 ---------------------------------------------------------------


def _agents_named(db, *slugs: str) -> None:
    """建几个内置人设：步骤只能引用看得见的人设，这是编辑时的硬约束。"""
    for slug in slugs:
        db.add(Agent(owner_id=0, slug=slug, name=f"{slug} 人设", system_prompt="p"))
    db.commit()


def _mcp_server(db, *, owner_id: int = OWNER, enabled: bool = True, name: str = "时间服务") -> McpServer:
    server = McpServer(
        owner_id=owner_id,
        name=name,
        transport="stdio",
        command="python",
        args=[],
        enabled=enabled,
    )
    db.add(server)
    db.commit()
    return server


def _create_payload(**overrides) -> WorkflowCreate:
    base = {
        "slug": "write-and-score",
        "name": "先写后评",
        "inputs": [{"name": "topic", "label": "", "required": True}],
        "steps": [
            {
                "key": "draft",
                "group_key": "draft",
                "agent_slug": "writer",
                "prompt_tpl": "就{{topic}}写一段话",
                "output_var": "draft",
            }
        ],
    }
    base.update(overrides)
    return WorkflowCreate(**base)


def _update_payload(**overrides) -> WorkflowUpdate:
    base = _create_payload().model_dump()
    base.update(overrides)
    return WorkflowUpdate(**base)


def test_create_persists_definition_and_numbers_the_steps(db):
    _agents_named(db, "writer")

    workflow = WorkflowService(db, OWNER).create(_create_payload())

    assert workflow.owner_id == OWNER
    assert workflow.is_builtin is False
    # 空标签用变量名兜底，别在运行历史里留一个没有名字的变量。
    assert workflow.input_schema == [{"name": "topic", "label": "topic", "required": True}]
    steps = WorkflowService(db, OWNER).steps_of(workflow.id)
    assert [step.key for step in steps] == ["draft"]
    assert steps[0].sort_order == 10


def test_create_rejects_a_taken_slug(db):
    _agents_named(db, "writer")
    service = WorkflowService(db, OWNER)
    service.create(_create_payload())

    with pytest.raises(ValueError, match="标识已存在"):
        service.create(_create_payload())


def test_create_rejects_undeclared_template_variable(db):
    _agents_named(db, "writer")
    payload = _create_payload(
        steps=[
            {
                "key": "draft",
                "group_key": "draft",
                "agent_slug": "writer",
                "prompt_tpl": "就{{subject}}写一段话",
            }
        ]
    )

    with pytest.raises(ValueError, match="未定义的变量 subject"):
        WorkflowService(db, OWNER).create(payload)
    # 校验不过不该留下半成品。
    assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_create_rejects_referencing_a_parallel_sibling(db):
    _agents_named(db, "writer", "scorer")
    # 同组并行：引用同组产物在运行时必然拿不到，保存时就得挡住。
    payload = _create_payload(
        steps=[
            {
                "key": "draft",
                "group_key": "all",
                "agent_slug": "writer",
                "prompt_tpl": "写{{topic}}",
                "output_var": "draft",
            },
            {
                "key": "score",
                "group_key": "all",
                "agent_slug": "scorer",
                "prompt_tpl": "评{{draft}}",
            },
        ]
    )

    with pytest.raises(ValueError, match="同组"):
        WorkflowService(db, OWNER).create(payload)


def test_create_rejects_an_interleaved_group(db):
    _agents_named(db, "writer", "scorer")
    # 引擎按首次出现归组，所以 a/c 会和 b 抢执行顺序：编辑器里的列表顺序就不是
    # 真实顺序了，这种写法直接拒掉。
    payload = _create_payload(
        steps=[
            {"key": "a", "group_key": "g1", "agent_slug": "writer", "prompt_tpl": "写{{topic}}"},
            {"key": "b", "group_key": "g2", "agent_slug": "scorer", "prompt_tpl": "评{{topic}}"},
            {"key": "c", "group_key": "g1", "agent_slug": "writer", "prompt_tpl": "再写{{topic}}"},
        ]
    )

    with pytest.raises(ValueError, match="不连续"):
        WorkflowService(db, OWNER).create(payload)


def test_create_allows_referencing_an_earlier_group_output(db):
    _agents_named(db, "writer", "scorer")
    payload = _create_payload(
        steps=[
            {
                "key": "draft",
                "group_key": "draft",
                "agent_slug": "writer",
                "prompt_tpl": "写{{topic}}",
                "output_var": "draft",
            },
            {
                "key": "score",
                "group_key": "score",
                "agent_slug": "scorer",
                "prompt_tpl": "评{{draft}}",
            },
        ]
    )

    workflow = WorkflowService(db, OWNER).create(payload)

    assert [step.sort_order for step in WorkflowService(db, OWNER).steps_of(workflow.id)] == [
        10,
        20,
    ]


def test_create_rejects_field_access_on_a_text_product(db):
    _agents_named(db, "writer", "scorer")
    payload = _create_payload(
        steps=[
            {
                "key": "draft",
                "group_key": "draft",
                "agent_slug": "writer",
                "prompt_tpl": "写{{topic}}",
                "output_var": "draft",
            },
            {
                "key": "score",
                "group_key": "score",
                "agent_slug": "scorer",
                "prompt_tpl": "评{{draft.summary}}",
            },
        ]
    )

    # 整段文本里没有字段可取：与其等运行时报「取不到」，保存时就把话说清。
    with pytest.raises(ValueError, match="没有字段可取"):
        WorkflowService(db, OWNER).create(payload)


def test_create_accepts_field_access_on_a_json_product(db):
    _agents_named(db, "writer", "scorer")
    payload = _create_payload(
        steps=[
            {
                "key": "draft",
                "group_key": "draft",
                "agent_slug": "writer",
                "prompt_tpl": "写{{topic}}",
                "output_kind": "json",
                "output_keys": ["summary"],
                "output_var": "draft",
            },
            {
                "key": "score",
                "group_key": "score",
                "agent_slug": "scorer",
                "prompt_tpl": "评{{draft.summary}}，也可以整段看：{{draft}}",
            },
        ]
    )

    assert WorkflowService(db, OWNER).create(payload).slug == "write-and-score"


def test_create_rejects_parallel_and_malformed_field_paths(db):
    _agents_named(db, "writer", "scorer")
    service = WorkflowService(db, OWNER)

    def payload(tpl: str, *, draft_group: str = "all") -> WorkflowCreate:
        return _create_payload(
            steps=[
                {
                    "key": "draft",
                    "group_key": draft_group,
                    "agent_slug": "writer",
                    "prompt_tpl": "写{{topic}}",
                    "output_kind": "json",
                    "output_var": "draft",
                },
                {
                    "key": "score",
                    "group_key": "all" if draft_group == "all" else "score",
                    "agent_slug": "scorer",
                    "prompt_tpl": tpl,
                },
            ]
        )

    # 同组是并行的：字段引用同样要挡住，只看根变量。
    with pytest.raises(ValueError, match="同一组是并行执行的"):
        service.create(payload("评{{draft.summary}}"))
    # ``{{draft.}}`` 这种残缺路径：占位符的正则吃得下它，得在保存时就说不合法。
    with pytest.raises(ValueError, match="字段名"):
        service.create(payload("评{{draft.}}", draft_group="draft"))


def test_create_rejects_unknown_agent_and_bad_names(db):
    payload = _create_payload(
        steps=[
            {
                "key": "draft",
                "group_key": "draft",
                "agent_slug": "nobody",
                "prompt_tpl": "写{{topic}}",
            }
        ]
    )
    with pytest.raises(ValueError, match="不存在的智能体"):
        WorkflowService(db, OWNER).create(payload)

    with pytest.raises(ValueError, match="不合法"):
        WorkflowService(db, OWNER).create(_create_payload(slug="ok", inputs=[{"name": "我的 变量", "label": "x"}]))


def test_create_rejects_mismatched_output_contract(db):
    _agents_named(db, "writer")
    payload = _create_payload(
        steps=[
            {
                "key": "draft",
                "group_key": "draft",
                "agent_slug": "writer",
                "prompt_tpl": "写{{topic}}",
                "output_keys": ["report"],
            }
        ]
    )
    with pytest.raises(ValueError, match="不是 json"):
        WorkflowService(db, OWNER).create(payload)


def test_create_validates_a_per_step_model_config(db):
    _agents_named(db, "writer")
    config = _chat_model(db)
    config.last_test_ok = False
    db.commit()

    # 未通过连通性测试的配置：与运行同一套门槛，按 4004 引导去配模型。
    with pytest.raises(BusinessError):
        WorkflowService(db, OWNER).create(
            _create_payload(
                steps=[
                    {
                        "key": "draft",
                        "group_key": "draft",
                        "agent_slug": "writer",
                        "prompt_tpl": "写{{topic}}",
                        "model_config_id": config.id,
                    }
                ]
            )
        )

    # 别人的配置：说「不存在」，不确认它存在。
    with pytest.raises(ValueError, match="模型配置不存在"):
        WorkflowService(db, OWNER).create(
            _create_payload(
                steps=[
                    {
                        "key": "draft",
                        "group_key": "draft",
                        "agent_slug": "writer",
                        "prompt_tpl": "写{{topic}}",
                        "model_config_id": 999999,
                    }
                ]
            )
        )


def test_create_persists_owned_mcp_server_ids(db):
    _agents_named(db, "writer")
    server = _mcp_server(db)

    workflow = WorkflowService(db, OWNER).create(
        _create_payload(mcp_server_ids=[server.id])
    )

    assert workflow.mcp_server_ids == [server.id]
    # 默认不挂 MCP：空列表。
    assert WorkflowService(db, OWNER).create(
        _create_payload(slug="plain", name="空白流程")
    ).mcp_server_ids == []


def test_create_rejects_foreign_or_missing_mcp_server_id(db):
    _agents_named(db, "writer")
    foreign = _mcp_server(db, owner_id=OWNER + 1)

    with pytest.raises(ValueError, match="不存在或不属于本人"):
        WorkflowService(db, OWNER).create(
            _create_payload(mcp_server_ids=[foreign.id])
        )
    with pytest.raises(ValueError, match="不存在或不属于本人"):
        WorkflowService(db, OWNER).create(
            _create_payload(mcp_server_ids=[999999])
        )
    # 校验不过不留半成品。
    assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_create_allows_a_disabled_mcp_server_at_save_time(db):
    """允许先挂载后启用：停用项保存时放行，运行时再由 resolve_servers 挡。"""
    _agents_named(db, "writer")
    disabled = _mcp_server(db, enabled=False)

    workflow = WorkflowService(db, OWNER).create(
        _create_payload(mcp_server_ids=[disabled.id])
    )

    assert workflow.mcp_server_ids == [disabled.id]


def test_update_replaces_mcp_server_ids(db):
    _agents_named(db, "writer")
    first = _mcp_server(db, name="第一个")
    second = _mcp_server(db, name="第二个")
    service = WorkflowService(db, OWNER)
    workflow = service.create(_create_payload(mcp_server_ids=[first.id]))

    service.update(workflow.id, _update_payload(mcp_server_ids=[second.id]))
    assert service.get(workflow.id).mcp_server_ids == [second.id]

    service.update(workflow.id, _update_payload(mcp_server_ids=[]))
    assert service.get(workflow.id).mcp_server_ids == []


def test_duplicate_copies_mcp_server_ids(db):
    _agents_named(db, "writer")
    server = _mcp_server(db)
    service = WorkflowService(db, OWNER)
    workflow = service.create(_create_payload(mcp_server_ids=[server.id]))

    clone = service.duplicate(workflow.id)

    assert clone.mcp_server_ids == [server.id]
    assert clone.mcp_server_ids is not workflow.mcp_server_ids


def test_update_replaces_the_steps_whole(db, monkeypatch):
    _agents_named(db, "writer", "scorer")
    service = WorkflowService(db, OWNER)
    workflow = service.create(
        _create_payload(
            steps=[
                {
                    "key": "draft",
                    "group_key": "draft",
                    "agent_slug": "writer",
                    "prompt_tpl": "写{{topic}}",
                    "output_var": "draft",
                },
                {
                    "key": "score",
                    "group_key": "score",
                    "agent_slug": "scorer",
                    "prompt_tpl": "评{{draft}}",
                },
            ]
        )
    )

    updated = service.update(
        workflow.id,
        _update_payload(
            name="只写",
            steps=[
                {
                    "key": "draft",
                    "group_key": "draft",
                    "agent_slug": "writer",
                    "prompt_tpl": "写{{topic}}",
                }
            ],
        ),
    )

    assert updated.name == "只写"
    # 同 key 的旧行被删掉又插入，不该撞 (workflow_id, key) 唯一索引。
    steps = service.steps_of(workflow.id)
    assert [step.key for step in steps] == ["draft"]
    assert steps[0].output_var is None


def test_update_keeps_the_history_of_a_past_run(db, monkeypatch):
    _agents_named(db, "writer")
    _chat_model(db)
    _stub_model(monkeypatch, lambda slug: "初稿")
    service = WorkflowService(db, OWNER)
    workflow = service.create(_create_payload())
    run = service.create_run(workflow.id, {"topic": "编排"})
    assert _run_all(db, OWNER, run.id)[-1][1]["status"] == RUN_SUCCEEDED

    service.update(
        workflow.id,
        _update_payload(
            steps=[
                {
                    "key": "draft",
                    "group_key": "draft",
                    "agent_slug": "writer",
                    "prompt_tpl": "换个说法：{{topic}}",
                }
            ]
        ),
    )

    # 改定义不该动已经发生过的那次运行：它存的是当时的 prompt。
    detail = service.to_run_detail(service.get_run(run.id))
    assert detail.status == RUN_SUCCEEDED
    assert [item.key for item in detail.steps] == ["draft"]
    assert detail.steps[0].prompt == "就编排写一段话"


def test_editing_a_builtin_is_refused(db):
    _seeded(db)
    workflow = _builtin(db, "resume-analysis")
    service = WorkflowService(db, OWNER)

    with pytest.raises(PermissionError, match="内置工作流不可修改"):
        service.delete(workflow.id)
    with pytest.raises(PermissionError, match="内置工作流不可修改"):
        service.update(workflow.id, _update_payload())


def test_duplicate_copies_the_definition_for_editing(db):
    _seeded(db)
    service = WorkflowService(db, OWNER)
    source = _builtin(db, "resume-analysis")

    clone = service.duplicate(source.id)
    again = service.duplicate(source.id)

    assert clone.owner_id == OWNER
    assert clone.is_builtin is False
    assert clone.slug == "resume-analysis-copy"
    assert clone.name.endswith(" 副本")
    assert [step.key for step in service.steps_of(clone.id)] == [
        step.key for step in service.steps_of(source.id)
    ]
    assert again.slug == "resume-analysis-copy2"
    # 复制出来的 JSON 列不能与源行共用同一个 list 实例。
    assert clone.input_schema is not source.input_schema


def test_delete_keeps_the_runs_that_already_happened(db):
    _agents_named(db, "writer")
    _chat_model(db)
    service = WorkflowService(db, OWNER)
    workflow = service.create(_create_payload())
    run = service.create_run(workflow.id, {"topic": "编排"})

    service.delete(workflow.id)

    assert db.scalar(select(Workflow).where(Workflow.id == workflow.id)) is None
    assert service.steps_of(workflow.id) == []
    kept = service.get_run(run.id)
    assert kept.workflow_slug == "write-and-score"
    assert [item.key for item in service.to_run_detail(kept).steps] == ["draft"]


# ---- 执行 -------------------------------------------------------------------


def test_execute_run_streams_and_persists_each_step(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    calls = _stub_model(monkeypatch, {"writer": "一段初稿", "scorer": "8 分"}.get)

    events = _run_all(db, OWNER, run.id)
    assert [name for name, _ in events] == [
        "run_start",
        "step_start",
        "step_end",
        "step_start",
        "step_end",
        "run_end",
    ]
    assert events[-1][1]["status"] == RUN_SUCCEEDED

    # 第二步的模板里出现的是第一步的输出：变量确实是跨步传递的。
    assert calls == [("writer", "就编排写一段话"), ("scorer", "给这篇打分：一段初稿")]

    # 事件当场就说得出这一步用了谁：不带上就得等整条跑完展开现场才知道。
    ends = [data for name, data in events if name == "step_end"]
    assert [(data["key"], data["model_name"]) for data in ends] == [
        ("draft", "unit-test"),
        ("score", "unit-test"),
    ]

    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run.id)}
    assert rows["draft"].output == "一段初稿"
    assert rows["draft"].prompt == "就编排写一段话"
    assert rows["score"].status == STEP_SUCCEEDED
    assert rows["score"].elapsed_ms is not None
    finished = WorkflowService(db, OWNER).get_run(run.id)
    assert finished.status == RUN_SUCCEEDED and finished.error_msg is None
    assert finished.elapsed_ms is not None


def test_run_model_names_reports_the_models_each_step_used(db, monkeypatch):
    """运行记录行上的模型来自这一步**实际**用的那份配置。

    定义里的 ``model_config_id`` 只写按步骤的覆盖，空着的是运行时的默认对话模型；
    历史行照抄定义的话，几乎每一步都是空的，界面上就成了「没用什么模型」。
    """
    _chat_model(db)
    override = ModelConfig(
        owner_id=OWNER,
        purpose=ModelPurpose.CHAT.value,
        title="按步骤指定的模型",
        model_name="second-unit-test",
        base_url="http://localhost:1/v1",
        last_test_ok=True,
    )
    db.add(override)
    db.commit()

    workflow = _two_step_workflow(db)
    service = WorkflowService(db, OWNER)
    _stub_model(monkeypatch, {"writer": "一段初稿", "scorer": "8 分"}.get)

    both_default = service.create_run(workflow.id, {"topic": "编排"})
    _run_all(db, OWNER, both_default.id)
    # 两步都用默认模型：报一个名字就够，两步各写一遍只是噪音。
    assert service.run_model_names([both_default.id]) == {both_default.id: ["unit-test"]}

    db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow.id, WorkflowStep.key == "score")
        .values(model_config_id=override.id)
    )
    db.commit()
    mixed = service.create_run(workflow.id, {"topic": "编排"})
    _run_all(db, OWNER, mixed.id)
    assert service.run_model_names([mixed.id]) == {mixed.id: ["unit-test", "second-unit-test"]}
    assert service.to_run_item(mixed, service.run_model_names([mixed.id])[mixed.id]).model_names == [
        "unit-test",
        "second-unit-test",
    ]
    # 现场按步给各自用的模型：混着跑的流程只有聚合说不清是哪一步换的模型。
    detail = service.to_run_detail(service.get_run(mixed.id))
    assert [(step.key, step.model_name) for step in detail.steps] == [
        ("draft", "unit-test"),
        ("score", "second-unit-test"),
    ]


def test_run_model_names_skips_the_steps_that_never_ran(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": RuntimeError("上游 502")}.get)

    _run_all(db, OWNER, run.id)

    service = WorkflowService(db, OWNER)
    # 跑挂的那步也知道自己用的是谁（模型在调用前就定下来了），没轮到的那步没有。
    assert service.run_model_names([run.id]) == {run.id: ["unit-test"]}
    rows = {row.key: row for row in service.run_steps(run.id)}
    assert rows["score"].status == STEP_SKIPPED and rows["score"].model_config_id is None
    assert service.run_model_names([]) == {}


def test_execute_run_marks_later_groups_skipped_after_failure(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": RuntimeError("上游 502")}.get)

    events = _run_all(db, OWNER, run.id)
    assert events[-1][0] == "run_end"
    assert events[-1][1]["status"] == RUN_FAILED
    assert "上游 502" in events[-1][1]["error"]

    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run.id)}
    assert rows["draft"].status == STEP_FAILED
    # 没轮到的步骤不是失败：历史里得区分「跑挂了」和「没跑」。
    assert rows["score"].status == STEP_SKIPPED


def test_execute_run_keeps_going_past_optional_step_failure(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow.id, WorkflowStep.key == "draft")
        .values(optional=True)
    )
    db.commit()
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": RuntimeError("可选步骤挂了"), "scorer": "7 分"}.get)

    events = _run_all(db, OWNER, run.id)
    assert events[-1][1]["status"] == RUN_SUCCEEDED
    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run.id)}
    assert rows["draft"].status == STEP_FAILED
    assert rows["score"].status == STEP_SUCCEEDED
    # 可选步骤没有产物，后续步骤拿到的是没替换过的空串占位——这里确认它照常跑了。
    assert rows["score"].prompt == "给这篇打分："


def test_execute_run_runs_a_parallel_group(db, monkeypatch):
    _chat_model(db)
    # 同组互相引用是定义错误（那一步的产物还没进上下文），所以这里第二步只用
    # 发起时的变量。
    workflow = _two_step_workflow(
        db, first_group="all", second_group="all", second_tpl="换个角度评：{{topic}}"
    )
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    calls = _stub_model(monkeypatch, {"writer": "初稿", "scorer": "终稿"}.get)

    events = _run_all(db, OWNER, run.id)
    assert events[-1][1]["status"] == RUN_SUCCEEDED
    assert {slug for slug, _ in calls} == {"writer", "scorer"}
    assert [name for name, _ in events].count("step_end") == 2


def test_json_step_products_are_field_addressable_downstream(db, monkeypatch):
    """JSON 步的产物按解析结果进上下文：整段引用喂对象，``{{x.字段}}`` 也取得到。"""
    _chat_model(db)
    workflow = _two_step_workflow(db, second_tpl="只用摘要评：{{draft.summary}}")
    db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow.id, WorkflowStep.key == "draft")
        .values(output_kind="json", output_keys=["summary"])
    )
    db.commit()
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    # 模型爱在 JSON 前后夹一句解释：进上下文的是解析出来的对象，那段话不会跟着走。
    calls = _stub_model(
        monkeypatch, {"writer": '好的：{"summary": "一段摘要", "extra": "忽略"}', "scorer": "8 分"}.get
    )

    events = _run_all(db, OWNER, run.id)

    assert events[-1][1]["status"] == RUN_SUCCEEDED
    assert ("scorer", "只用摘要评：一段摘要") in calls


def test_rerun_step_rebuilds_json_fields_from_the_stored_scene(db, monkeypatch):
    """重跑重建上下文时同一口径：字段取自落库的解析结果，不是当时那段文本。"""
    _chat_model(db)
    workflow = _two_step_workflow(db, second_tpl="只用摘要评：{{draft.summary}}")
    db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow.id, WorkflowStep.key == "draft")
        .values(output_kind="json", output_keys=["summary"])
    )
    db.commit()
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": '{"summary": "一段摘要"}', "scorer": "8 分"}.get)
    _run_all(db, OWNER, run.id)
    run_id = run.id

    prepare_step_rerun(db, OWNER, run_id, "score")
    calls = _stub_model(monkeypatch, {"scorer": "9 分"}.get)
    _rerun(db, OWNER, run_id, "score")

    assert calls == [("scorer", "只用摘要评：一段摘要")]


def test_same_group_step_cannot_reference_its_siblings_output(db, monkeypatch):
    """同组并行，所以彼此的产物还没进上下文：引用它必须响亮地失败。

    静默替换成空串的话，第二步会拿着一段缺失的中间产物跑完，产出一份看着完整、
    实际依据不足的结果——而这条流程的定义本来就是错的。
    """
    _chat_model(db)
    workflow = _two_step_workflow(db, first_group="all", second_group="all")
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "终稿"}.get)

    events = _run_all(db, OWNER, run.id)
    assert events[-1][1]["status"] == RUN_FAILED
    assert "未定义的变量" in events[-1][1]["error"]


def test_execute_run_parses_json_step(db, monkeypatch):
    seed_builtin_workflows(db)
    _chat_model(db)
    workflow = _builtin(db, "resume-analysis")
    run = WorkflowService(db, OWNER).create_run(
        workflow.id, {"resume_content": "十年经验", "description_note": ""}
    )
    payload = json.dumps({"report": "整体不错", "suggestions": ["补量化"]}, ensure_ascii=False)
    calls = _stub_model(monkeypatch, {"resume-analyzer": payload}.get)

    assert _run_all(db, OWNER, run.id)[-1][1]["status"] == RUN_SUCCEEDED
    assert calls[0][1] == '简历全文：\n"""\n十年经验\n"""'
    step = WorkflowService(db, OWNER).run_steps(run.id)[0]
    assert step.parsed == {"report": "整体不错", "suggestions": ["补量化"]}
    assert step.output == payload


def test_execute_run_fails_json_step_on_missing_contract_field(db, monkeypatch):
    seed_builtin_workflows(db)
    _chat_model(db)
    workflow = _builtin(db, "resume-analysis")
    run = WorkflowService(db, OWNER).create_run(
        workflow.id, {"resume_content": "十年经验", "description_note": ""}
    )
    _stub_model(monkeypatch, {"resume-analyzer": '{"report": "只有报告"}'}.get)

    events = _run_all(db, OWNER, run.id)
    assert events[-1][1]["status"] == RUN_FAILED
    assert "suggestions" in events[-1][1]["error"]


def test_stream_run_finishes_the_run_when_the_consumer_leaves(db, monkeypatch):
    """断流只关转发流：执行在自己的任务上跑完，运行行必须进终态。

    旧实现把执行生成器直接挂在 SSE 响应上，客户端一断，取消打在建到一半的
    生成器上，步骤各自跑完了、运行的终态却没人写——界面上永远「执行中」。
    """
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)

    async def drive():
        stream = stream_run(OWNER, run.id)
        assert (await stream.__anext__())[0] == "run_start"
        await stream.aclose()  # 相当于页面被关掉：这条流不接了
        for _ in range(500):
            await asyncio.sleep(0.01)
            db.rollback()
            status = WorkflowService(db, OWNER).get_run(run.id).status
            if status not in (RUN_PENDING, RUN_RUNNING):
                return status
        return status

    db.rollback()
    assert asyncio.run(drive()) == RUN_SUCCEEDED


def test_stream_run_fails_the_run_when_executor_crashes(db, monkeypatch):
    """执行体在第一步之前就炸（没配对话模型）：运行落成失败，不能停在 pending 空挂。"""
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "x", "scorer": "y"}.get)

    async def collect():
        return [(event, payload) async for event, payload in stream_run(OWNER, run.id)]

    db.rollback()
    events = asyncio.run(collect())
    db.rollback()
    assert events[-1][0] == "error"
    finished = WorkflowService(db, OWNER).get_run(run.id)
    assert finished.status == RUN_FAILED
    assert finished.error_msg


def test_execute_run_requires_a_chat_model_before_any_event(db, monkeypatch):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "x", "scorer": "y"}.get)

    # 门槛在第一步之前：没配好模型时该立刻给出明确的「去配置」，而不是让每步各自
    # 失败一遍。运行停在 pending，不会留下一串失败步骤。
    with pytest.raises(BusinessError):
        _run_all(db, OWNER, run.id)
    assert WorkflowService(db, OWNER).get_run(run.id).status == STEP_PENDING


def test_execute_run_opens_mcp_bundle_and_passes_tools_to_each_step(db, monkeypatch):
    """MCP 连接束在第一步之前打开，两个步骤拿到同一组工具，结束后关闭。"""
    _chat_model(db)
    server = _mcp_server(db)
    workflow = _two_step_workflow(db)
    workflow.mcp_server_ids = [server.id]
    db.commit()
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})

    seen_tools: list[list] = []

    async def fake_run_agent(db, owner_id, agent_slug, user_prompt, *, tools=None, **kwargs):
        seen_tools.append(list(tools or []))
        return "初稿"

    monkeypatch.setattr(workflows_module, "run_agent", fake_run_agent)

    resolved: dict = {}

    def fake_resolve(db, owner_id, ids):
        resolved["ids"] = list(ids)
        return ["server-row"]

    monkeypatch.setattr(workflows_module, "resolve_servers", fake_resolve)

    class _FakeBundle:
        def __init__(self):
            self.tools = ["mcp-tool"]
            self.closed = False

        async def __aexit__(self, *exc_info):
            self.closed = True

    bundle = _FakeBundle()

    async def fake_open(servers):
        assert servers == ["server-row"]
        return bundle

    monkeypatch.setattr(workflows_module, "open_bundle", fake_open)

    events = _run_all(db, OWNER, run.id)

    assert resolved["ids"] == [server.id]
    assert seen_tools == [["mcp-tool"], ["mcp-tool"]]
    assert events[-1][1]["status"] == RUN_SUCCEEDED
    assert bundle.closed, "运行结束必须关连接束，stdio 子进程靠它回收"


def test_execute_run_fails_when_mcp_bundle_cannot_open(db, monkeypatch):
    """挂载的服务在第一步之前解析/连接失败：run 落 failed，不产生任何步骤事件。"""
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})

    def fake_resolve(db, owner_id, ids):
        raise LookupError("挂载的 MCP 服务不存在或无权访问：9，")

    monkeypatch.setattr(workflows_module, "resolve_servers", fake_resolve)
    _stub_model(monkeypatch, {"writer": "x", "scorer": "y"}.get)

    # 解析失败发生在首个事件之前：由 stream_run 的既有异常路径落终态，
    # execute_run 本身则是直接抛出。
    async def collect():
        return [
            (event, payload)
            async for event, payload in stream_run(owner_id=OWNER, run_id=run.id)
        ]

    db.rollback()
    events = asyncio.run(collect())
    db.rollback()

    assert [name for name, _ in events] == ["error"]
    finished = WorkflowService(db, OWNER).get_run(run.id)
    assert finished.status == RUN_FAILED
    assert "MCP 服务不存在" in finished.error_msg


def test_execute_run_with_empty_mcp_mount_skips_the_bundle(db, monkeypatch):
    """不挂 MCP：仍会走 resolve/open（空操作），行为与接线前一致。"""
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})

    opened: list = []

    def fake_resolve(db, owner_id, ids):
        assert ids == []
        return []

    class _FakeEmptyBundle:
        tools: list = []

        async def __aexit__(self, *exc_info):
            return None

    async def fake_open(servers):
        opened.append(servers)
        return _FakeEmptyBundle()

    monkeypatch.setattr(workflows_module, "resolve_servers", fake_resolve)
    monkeypatch.setattr(workflows_module, "open_bundle", fake_open)
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)

    events = _run_all(db, OWNER, run.id)

    assert opened == [[]]
    assert events[-1][1]["status"] == RUN_SUCCEEDED


def test_run_timestamps_are_naive_local_not_utc(db, monkeypatch):
    """运行与步骤的时间戳要跟 ``created_at`` 同一口径：本地 naive。

    线上 ``created_at`` 由 MySQL 的 ``NOW()`` 写，是数据库所在时区的墙上时间；
    显式写 UTC 的话，一次运行会「比它自己的创建时间早 8 小时」。测试库是 SQLite
    （UTC），两种写法在库里看不出差别，所以这里钉的是约定本身：不带 tzinfo，且贴着
    本地此刻。
    """
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)

    before = datetime.now()
    _run_all(db, OWNER, run.id)
    window = (before, datetime.now() + timedelta(seconds=5))

    service = WorkflowService(db, OWNER)
    finished = service.get_run(run.id)
    moments = [finished.started_at, finished.finished_at]
    moments += [m for row in service.run_steps(run.id) for m in (row.started_at, row.finished_at)]
    assert moments and all(m is not None for m in moments)
    for moment in moments:
        assert moment.tzinfo is None
        assert window[0] <= moment <= window[1], f"{moment} 不在本地此刻附近，写的是别的时区"


# ---- 中途跟随 ---------------------------------------------------------------


def test_follow_run_replays_the_scene_and_closes_a_settled_run(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _scene(
        db,
        run.id,
        RUN_FAILED,
        "上游 502",
        draft=(STEP_SUCCEEDED, "初稿"),
        score=(STEP_FAILED, None),
    )

    events = _follow_all(db, run.id, lambda: None)

    assert [name for name, _ in events] == ["run_start", "run_end"], "终态不该进轮询循环"
    start = events[0][1]
    assert start["run_id"] == run.id and start["status"] == RUN_FAILED
    assert start["workflow_slug"] == "write-and-score"
    assert [(row["key"], row["status"], row["agent_slug"]) for row in start["steps"]] == [
        ("draft", STEP_SUCCEEDED, "writer"),
        ("score", STEP_FAILED, "scorer"),
    ]
    # 定义的元信息跟着走：界面要能说清这一步是不是可选的。
    assert start["steps"][0]["optional"] is False
    assert events[-1][1] == {
        "run_id": run.id,
        "status": RUN_FAILED,
        "error": "上游 502",
        "elapsed_ms": None,
    }


def test_follow_run_heals_a_run_stuck_at_running(db):
    """僵尸修复：每一步都定了、运行行还停在跑。

    旧实现把执行挂在发起的 SSE 上，断流时终态落库那段没跑到，留下的就是这种行。
    跟随要先按步骤行重算再收流——不修的话它会永远转圈，界面永远「执行中」。
    """
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _scene(
        db,
        run.id,
        RUN_RUNNING,
        draft=(STEP_SUCCEEDED, "初稿"),
        score=(STEP_SUCCEEDED, "8 分"),
    )

    events = _follow_all(db, run.id, lambda: None)

    assert events[-1][0] == "run_end"
    assert events[-1][1]["status"] == RUN_SUCCEEDED
    assert WorkflowService(db, OWNER).get_run(run.id).status == RUN_SUCCEEDED


def test_follow_run_streams_only_what_the_client_missed(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    run_id = run.id
    model = _chat_model(db)
    _scene(
        db,
        run_id,
        RUN_RUNNING,
        draft=(STEP_SUCCEEDED, "初稿"),
        score=(STEP_RUNNING, None),
    )

    def worker() -> None:
        time.sleep(0.05)
        _write_step(
            run_id,
            "score",
            status=STEP_SUCCEEDED,
            output="8 分",
            elapsed_ms=12,
            model_config_id=model.id,
        )
        _write_run(run_id, status=RUN_SUCCEEDED, elapsed_ms=40)

    events = _follow_all(db, run_id, worker)
    names = [name for name, _ in events]

    assert names[0] == "run_start" and names[-1] == "run_end"
    # 已经落库的第一步只出现在回放里，之后只推变化的那一步。
    assert {row["status"] for row in events[0][1]["steps"] if row["key"] == "draft"} == {
        STEP_SUCCEEDED
    }
    # 接上时还没写下模型的步骤没有名字可报。
    assert [row["model_name"] for row in events[0][1]["steps"]] == [None, None]
    assert [payload for name, payload in events if name == "step_end"] == [
        {
            "key": "score",
            "status": STEP_SUCCEEDED,
            "error": None,
            "output_chars": 3,
            "elapsed_ms": 12,
            # 跟随流的载荷与发起那条同形：客户端不用为「中途接上」少显示一栏。
            "model_name": "unit-test",
        }
    ]
    assert events[-1][1]["status"] == RUN_SUCCEEDED


def test_follow_run_reopens_a_step_that_was_put_back_to_pending(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    run_id = run.id
    _scene(
        db,
        run_id,
        RUN_RUNNING,
        draft=(STEP_SUCCEEDED, "初稿"),
        score=(STEP_SUCCEEDED, "8 分"),
    )

    def worker() -> None:
        # 单步重跑的样子：整条运行回到执行中，那一步从「成功」被打回待运行。
        _write_step(run_id, "score", status=STEP_PENDING, output=None)
        time.sleep(0.3)
        _write_step(run_id, "score", status=STEP_SUCCEEDED, output="9 分")
        _write_run(run_id, status=RUN_SUCCEEDED)

    events = _follow_all(db, run_id, worker)
    names = [name for name, _ in events]

    # 没有这条 step_start，客户端会拿着上一次的「成功」继续显示一个正在重跑的产物。
    assert "step_start" in names and names.index("step_start") < names.index("step_end")
    start = next(payload for name, payload in events if name == "step_start")
    assert (start["key"], start["status"], start["group_key"]) == ("score", STEP_PENDING, "score")


def test_follow_run_stops_when_the_record_is_pruned_away(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    run_id = run.id
    _scene(
        db,
        run_id,
        RUN_RUNNING,
        draft=(STEP_RUNNING, None),
        score=(STEP_PENDING, None),
    )

    def worker() -> None:
        time.sleep(0.05)
        from app.database import SessionLocal

        with SessionLocal() as other:
            other.execute(delete(WorkflowRun).where(WorkflowRun.id == run_id))
            other.commit()

    # 记录没了就报错收流：接着转圈只会让界面卡在「执行中」。
    with pytest.raises(LookupError):
        _follow_all(db, run_id, worker)


def test_follow_run_detaches_when_the_client_leaves(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    run_id = run.id
    _scene(
        db,
        run_id,
        RUN_RUNNING,
        draft=(STEP_RUNNING, None),
        score=(STEP_PENDING, None),
    )

    async def drive():
        async def gone():
            return True

        return [
            (event, payload)
            async for event, payload in follow_run(
                owner_id=OWNER, run_id=run_id, is_disconnected=gone, poll_seconds=0.005
            )
        ]

    db.rollback()
    events = asyncio.run(drive())
    db.rollback()

    assert [name for name, _ in events] == ["run_start"]
    assert WorkflowService(db, OWNER).get_run(run_id).status == RUN_RUNNING, "跟随不执行，断开不该改现状"


def test_follow_run_requires_the_run_to_exist(db):
    with pytest.raises(LookupError):
        _follow_all(db, 999999, lambda: None)


# ---- 单步重跑 ---------------------------------------------------------------


def test_prepare_step_rerun_clears_the_scene_and_reopens_the_run(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)
    _run_all(db, OWNER, run.id)
    run_id = run.id

    prepare_step_rerun(db, OWNER, run_id, "score")

    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run_id)}
    assert rows["score"].status == STEP_PENDING
    assert (rows["score"].prompt, rows["score"].output, rows["score"].error_msg) == (
        None,
        None,
        None,
    )
    assert rows["draft"].status == STEP_SUCCEEDED, "上游的产物留着，重跑只用它"
    # 整条运行回到执行中：这一步定下来之前，业务行上显示的就是「分析中」。
    reopened = WorkflowService(db, OWNER).get_run(run_id)
    assert reopened.status == RUN_RUNNING
    assert reopened.error_msg is None and reopened.finished_at is None


def test_prepare_step_rerun_refuses_a_run_that_is_still_going(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _scene(db, run.id, RUN_RUNNING, draft=(STEP_RUNNING, None), score=(STEP_PENDING, None))

    # 两个执行体抢同一行写，最后留下的现场没人知道是谁的。
    with pytest.raises(ValueError, match="还没结束"):
        prepare_step_rerun(db, OWNER, run.id, "draft")


def test_prepare_step_rerun_refuses_a_step_that_is_not_there(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _scene(db, run.id, RUN_SUCCEEDED, draft=(STEP_SUCCEEDED, "初稿"), score=(STEP_SUCCEEDED, "8 分"))
    db.execute(
        delete(WorkflowStep).where(
            WorkflowStep.workflow_id == workflow.id, WorkflowStep.key == "score"
        )
    )
    db.commit()

    with pytest.raises(ValueError, match="定义里已经没有这一步"):
        prepare_step_rerun(db, OWNER, run.id, "score")
    with pytest.raises(ValueError, match="没有这一步"):
        prepare_step_rerun(db, OWNER, run.id, "ghost")
    with pytest.raises(LookupError):
        prepare_step_rerun(db, OWNER, 999999, "draft")


def test_rerun_step_rebuilds_context_from_the_stored_products(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)
    _run_all(db, OWNER, run.id)
    run_id = run.id

    prepare_step_rerun(db, OWNER, run_id, "score")
    calls = _stub_model(
        monkeypatch, {"scorer": "9 分", "writer": RuntimeError("重跑不该再碰上游")}.get
    )
    _rerun(db, OWNER, run_id, "score")

    assert calls == [("scorer", "给这篇打分：初稿")]
    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run_id)}
    assert (rows["score"].status, rows["score"].output) == (STEP_SUCCEEDED, "9 分")
    assert rows["draft"].output == "初稿", "只有那一步被换过"
    assert WorkflowService(db, OWNER).get_run(run_id).status == RUN_SUCCEEDED


def test_rerun_step_uses_the_current_definition(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)
    _run_all(db, OWNER, run.id)
    run_id = run.id
    db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow.id, WorkflowStep.key == "score")
        .values(prompt_tpl="换个标准重评：{{draft}}")
    )
    db.commit()

    prepare_step_rerun(db, OWNER, run_id, "score")
    _rerun(db, OWNER, run_id, "score")

    # 步骤行只存渲染出来的那一份，模板只存在于定义里——重跑用的就是现在这份定义。
    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run_id)}
    assert rows["score"].prompt == "换个标准重评：初稿"


def test_rerun_of_a_parallel_step_still_cannot_see_its_sibling(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(
        db, first_group="all", second_group="all", second_tpl="换个角度评：{{topic}}"
    )
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)
    _run_all(db, OWNER, run.id)
    run_id = run.id
    db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow.id, WorkflowStep.key == "score")
        .values(prompt_tpl="补一段点评：{{draft}}")
    )
    db.commit()

    prepare_step_rerun(db, OWNER, run_id, "score")
    calls = _stub_model(monkeypatch, {"scorer": "9 分"}.get)
    _rerun(db, OWNER, run_id, "score")

    # 同组第一次跑的时候也看不见兄弟的产物；重跑不能因为跑得晚就突然多喂一段。
    assert calls == []
    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run_id)}
    assert rows["score"].status == STEP_FAILED
    assert "未定义的变量" in rows["score"].error_msg
    assert WorkflowService(db, OWNER).get_run(run_id).status == RUN_FAILED


def test_rerun_reports_the_steps_that_never_ran(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    run_id = run.id
    _stub_model(monkeypatch, {"writer": RuntimeError("上游 502"), "scorer": "8 分"}.get)
    _run_all(db, OWNER, run.id)

    prepare_step_rerun(db, OWNER, run_id, "draft")
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)
    _rerun(db, OWNER, run_id, "draft")

    finished = WorkflowService(db, OWNER).get_run(run_id)
    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run_id)}
    assert rows["draft"].status == STEP_SUCCEEDED
    assert rows["score"].status == STEP_SKIPPED, "重跑只换这一步，不替用户把后面补上"
    # 这一步通了不等于整条流程通了：产物是后处理写的，后处理只认跑完的流程。
    assert finished.status == RUN_FAILED
    assert "还有 1 步没有跑" in finished.error_msg


def test_rerun_settles_the_run_on_a_model_failure(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)
    _run_all(db, OWNER, run.id)
    run_id = run.id

    prepare_step_rerun(db, OWNER, run_id, "score")
    _stub_model(monkeypatch, {"scorer": RuntimeError("评分模型挂了")}.get)
    _rerun(db, OWNER, run_id, "score")

    finished = WorkflowService(db, OWNER).get_run(run_id)
    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run_id)}
    assert rows["score"].status == STEP_FAILED
    assert "评分模型挂了" in rows["score"].error_msg
    assert finished.status == RUN_FAILED and "评分模型挂了" in finished.error_msg


# ---- 重启收尾 ---------------------------------------------------------------


def test_reset_stale_runs_finishes_last_process_runs(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    run.status = RUN_RUNNING
    steps = WorkflowService(db, OWNER).run_steps(run.id)
    steps[0].status = STEP_SUCCEEDED
    db.commit()

    reset_stale_runs(db)

    finished = WorkflowService(db, OWNER).get_run(run.id)
    assert finished.status == RUN_FAILED and finished.error_msg == STALE_RUN_ERROR
    rows = WorkflowService(db, OWNER).run_steps(run.id)
    assert rows[0].status == STEP_SUCCEEDED, "已完成的步骤不动"
    assert rows[1].status == STEP_FAILED and rows[1].error_msg == STALE_RUN_ERROR


def test_reset_stale_runs_leaves_finished_rows_alone(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    run.status = RUN_SUCCEEDED
    db.execute(
        update(WorkflowRun).where(WorkflowRun.id == run.id).values(status=RUN_SUCCEEDED)
    )
    db.execute(
        update(WorkflowRunStep)
        .where(WorkflowRunStep.run_id == run.id)
        .values(status=STEP_SUCCEEDED)
    )
    db.commit()

    reset_stale_runs(db)
    assert WorkflowService(db, OWNER).get_run(run.id).status == RUN_SUCCEEDED


# ---- 保留与清理 -------------------------------------------------------------


def test_prune_runs_keeps_the_latest_run_of_each_subject(db):
    workflow = _two_step_workflow(db)
    service = WorkflowService(db, OWNER)
    old = service.create_run(
        workflow.id, {"topic": "旧"}, subject_kind="resume", subject_ref=7
    )
    fresh = service.create_run(
        workflow.id, {"topic": "新"}, subject_kind="resume", subject_ref=7
    )
    for row in (old, fresh):
        _scene(
            db,
            row.id,
            RUN_SUCCEEDED,
            draft=(STEP_SUCCEEDED, "初稿"),
            score=(STEP_SUCCEEDED, "8 分"),
        )
        _aged(db, row.id)

    assert prune_runs(db) == (1, 2), "同一次认领里只有最近一次要留着"
    assert {row.id for row in db.scalars(select(WorkflowRun)).all()} == {fresh.id}
    assert db.scalar(select(func.count()).select_from(WorkflowRunStep)) == 2


def test_prune_runs_keeps_in_flight_and_recent_runs(db):
    workflow = _two_step_workflow(db)
    service = WorkflowService(db, OWNER)
    going = service.create_run(workflow.id, {"topic": "在跑"})
    recent = service.create_run(workflow.id, {"topic": "刚跑完"})
    old = service.create_run(workflow.id, {"topic": "早跑完了"})
    _scene(db, going.id, RUN_RUNNING, draft=(STEP_RUNNING, None), score=(STEP_PENDING, None))
    for row in (recent, old):
        _scene(
            db,
            row.id,
            RUN_SUCCEEDED,
            draft=(STEP_SUCCEEDED, "初稿"),
            score=(STEP_SUCCEEDED, "8 分"),
        )
    _aged(db, old.id)

    # 进行中的删掉等于留下一堆没人跑完的失败；窗口内的还是历史。
    assert prune_runs(db) == (1, 2)
    assert {row.id for row in db.scalars(select(WorkflowRun)).all()} == {going.id, recent.id}


def test_prune_runs_is_scoped_by_owner(db):
    workflows = _seeded(db)
    workflow = workflows["resume-analysis"]
    mine = WorkflowService(db, OWNER).create_run(workflow.id, {"resume_content": "x"})
    theirs = WorkflowService(db, 2).create_run(workflow.id, {"resume_content": "x"})
    for row in (mine, theirs):
        _scene(db, row.id, RUN_SUCCEEDED, analyze=(STEP_SUCCEEDED, "报告"))
        _aged(db, row.id)

    assert prune_runs(db, owner_id=OWNER) == (1, 1), "手动入口只清自己的记录"
    assert {row.id for row in db.scalars(select(WorkflowRun)).all()} == {theirs.id}
    # 启动时那一次不带 owner：全站一起收。
    assert prune_runs(db) == (1, 1)
    assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 0


def test_prune_runs_respects_the_window(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _scene(db, run.id, RUN_FAILED, draft=(STEP_FAILED, None), score=(STEP_SKIPPED, None))
    _aged(db, run.id, days_ago=5)

    assert prune_runs(db, keep_days=30) == (0, 0)
    assert prune_runs(db, keep_days=3) == (1, 2)


def test_prune_runs_drops_unowned_runs_past_the_window(db):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _scene(db, run.id, RUN_FAILED, draft=(STEP_FAILED, None), score=(STEP_SKIPPED, None))
    _aged(db, run.id)

    # 在工作流页面上手动跑的那几次没认领任何业务对象，也就没有状态要担——过窗口就删。
    assert prune_runs(db) == (1, 2)


# ---- 业务状态翻译 -----------------------------------------------------------


def test_subject_state_refuses_ready_when_no_product_was_written(db):
    run = WorkflowRun(
        owner_id=OWNER,
        workflow_id=1,
        workflow_slug="resume-analysis",
        inputs={},
        status=RUN_SUCCEEDED,
    )

    # 单步重跑就能造出这种组合：那一步通了，产物却没人写回业务行。
    status, error = subject_state(run, has_product=False)
    assert status == SUBJECT_ERROR and "没有写回" in error
    assert subject_state(run, has_product=True) == (SUBJECT_READY, None)


# ---- 序列化 -----------------------------------------------------------------


def test_to_item_exposes_steps_and_agent_names(db):
    seed_builtin_workflows(db)
    db.add(Agent(owner_id=0, slug="resume-analyzer", name="简历分析师", system_prompt="p"))
    db.commit()
    workflow = _builtin(db, "resume-analysis")

    item = WorkflowService(db, OWNER).to_item(workflow)
    assert item.slug == "resume-analysis"
    assert [field.name for field in item.inputs] == ["resume_content", "description_note"]
    assert item.steps[0].agent_name == "简历分析师"
    assert item.steps[0].output_keys == ["report", "suggestions"]


def test_last_runs_takes_the_newest_run_of_each_workflow(db):
    """清单页卡片上那行「最近一次运行」：每条流程 id 最大的那次，没跑过的没有。

    口径与业务对象那边的 :func:`latest_runs` 一致——重新发起是新起一行，旧行只做历史。
    """
    _agents_named(db, "writer")
    service = WorkflowService(db, OWNER)
    used = service.create(_create_payload())
    never = service.create(_create_payload(slug="another", name="另一条"))
    first = service.create_run(used.id, {"topic": "编排"})
    second = service.create_run(used.id, {"topic": "编排"})
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == second.id)
        .values(status=RUN_FAILED, error_msg="上游 502", elapsed_ms=74_000)
    )
    db.commit()

    assert set(service.last_runs([used.id, never.id])) == {used.id}
    assert service.last_runs([]) == {}

    item = service.to_item(used, service.last_runs([used.id])[used.id])
    assert item.last_run.id == second.id > first.id
    assert (item.last_run.status, item.last_run.elapsed_ms) == (RUN_FAILED, 74_000)
    # 结束 / 开始 / 创建依次取：pending 的行前两个都还是空的。
    assert item.last_run.at == second.created_at
    # 从没跑过的流程不在结果里，卡片上那一行整行不渲染。
    assert service.to_item(never).last_run is None


def test_to_item_looks_up_the_last_run_when_not_given(db):
    """单对象端点不必各自记得先查：``to_item`` 不传就自己取一条。"""
    _agents_named(db, "writer")
    service = WorkflowService(db, OWNER)
    workflow = service.create(_create_payload())
    run = service.create_run(workflow.id, {"topic": "编排"})

    assert service.to_item(workflow).last_run.id == run.id


def test_to_run_detail_labels_steps_from_the_current_definition(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    db.add_all(
        [
            Agent(owner_id=OWNER, slug="writer", name="写手", system_prompt="p"),
            Agent(owner_id=OWNER, slug="scorer", name="评审", system_prompt="p"),
        ]
    )
    db.execute(
        update(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow.id, WorkflowStep.key == "score")
        .values(output_kind="json", output_keys=["score"], optional=True)
    )
    db.commit()
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": '{"score": "8"}'}.get)
    _run_all(db, OWNER, run.id)

    service = WorkflowService(db, OWNER)
    detail = service.to_run_detail(service.get_run(run.id))
    # 「这一步是谁、可选吗、要什么格式」长在定义上，详情得把它对上：现场只剩一串
    # key 的话，看不出来当时编排的是什么。
    assert [(row.agent_name, row.optional, row.output_kind) for row in detail.steps] == [
        ("写手", False, "markdown"),
        ("评审", True, "json"),
    ]


def test_to_run_detail_carries_the_scene_of_each_step(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)
    _run_all(db, OWNER, run.id)

    service = WorkflowService(db, OWNER)
    detail = service.to_run_detail(service.get_run(run.id))
    assert detail.inputs == {"topic": "编排"}
    assert [row.prompt for row in detail.steps] == ["就编排写一段话", "给这篇打分：初稿"]
    assert [row.status for row in detail.steps] == [STEP_SUCCEEDED, STEP_SUCCEEDED]
