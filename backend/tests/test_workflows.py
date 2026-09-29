"""工作流引擎：定义 seed、输入校验、分步执行与运行现场。

跑真实的 SQLite（临时文件库）与假的模型调用：要验证的是编排、落库与状态收敛，
不是上游模型；每次调用被打桩成返回固定文本，快，而且能断言 prompt 到底是什么。

``execute_run`` 里每个步骤自开一个 session（并发写各自的行），所以这里必须把
``app.database.SessionLocal`` 换到同一个临时库上，否则步骤会去写真实部署库。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.errors import BusinessError
from app.models.entities import (
    Agent,
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
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STALE_RUN_ERROR,
    STEP_FAILED,
    STEP_PENDING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    WorkflowService,
    _group_order,
    execute_run,
    render_prompt,
    reset_stale_runs,
    seed_builtin_workflows,
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
        db, owner_id, agent_slug, user_prompt, *, agent=None, config_id=None, timeout=None
    ):
        calls.append((agent_slug, user_prompt))
        reply = reply_for(agent_slug)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(workflows_module, "run_agent", fake_run_agent)
    return calls


def _run_all(db, owner_id: int, run_id: int, **kwargs) -> list[tuple[str, dict]]:
    """跑完一次运行并返回事件列表。

    结束前后各 rollback 一次：步骤写的是另一条连接，而 SQLite 的读事务从第一次
    SELECT 起就固定住快照，不重开事务的话这里读到的还是运行开始前的旧状态。
    """
    async def collect():
        return [
            (event, payload)
            async for event, payload in execute_run(owner_id=owner_id, run_id=run_id, **kwargs)
        ]

    events = asyncio.run(collect())
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


# ---- 模板渲染 ---------------------------------------------------------------


def test_render_prompt_substitutes_values():
    assert render_prompt("共 {{count}} 份：{{block}}", {"count": "3", "block": "正文"}) == "共 3 份：正文"


def test_render_prompt_rejects_unknown_variable():
    # 少喂一段简历全文，模型照样能编出一份看着完整的报告——所以必须报错而不是替换成空串。
    with pytest.raises(ValueError, match="未定义的变量"):
        render_prompt("{{resume_content}}", {"other": "x"})


def test_group_order_keeps_first_appearance_sequence():
    class Step:
        def __init__(self, key, group):
            self.key, self.group_key = key, group

    groups = _group_order([Step("a", "g1"), Step("b", "g2"), Step("c", "g1")])
    assert [(name, [s.key for s in members]) for name, members in groups] == [
        ("g1", ["a", "c"]),
        ("g2", ["b"]),
    ]


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


# ---- 定义编辑 ---------------------------------------------------------------


def _agents_named(db, *slugs: str) -> None:
    """建几个内置人设：步骤只能引用看得见的人设，这是编辑时的硬约束。"""
    for slug in slugs:
        db.add(Agent(owner_id=0, slug=slug, name=f"{slug} 人设", system_prompt="p"))
    db.commit()


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


def test_create_rejects_dotted_field_access_with_a_hint(db):
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

    # 占位符的正则允许点号，所以这里必须自己说清楚：现在只能引用整段产物。
    with pytest.raises(ValueError, match="暂不支持按字段取值"):
        WorkflowService(db, OWNER).create(payload)


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

    rows = {row.key: row for row in WorkflowService(db, OWNER).run_steps(run.id)}
    assert rows["draft"].output == "一段初稿"
    assert rows["draft"].prompt == "就编排写一段话"
    assert rows["score"].status == STEP_SUCCEEDED
    assert rows["score"].elapsed_ms is not None
    finished = WorkflowService(db, OWNER).get_run(run.id)
    assert finished.status == RUN_SUCCEEDED and finished.error_msg is None
    assert finished.elapsed_ms is not None


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


def test_execute_run_cancels_when_client_leaves(db, monkeypatch):
    _chat_model(db)
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "初稿", "scorer": "8 分"}.get)

    async def disconnected():
        return True

    events = _run_all(db, OWNER, run.id, is_disconnected=disconnected)

    assert [name for name, _ in events] == ["run_start"]
    assert WorkflowService(db, OWNER).get_run(run.id).status == RUN_CANCELLED
    assert all(
        row.status == STEP_SKIPPED for row in WorkflowService(db, OWNER).run_steps(run.id)
    )


def test_execute_run_requires_a_chat_model_before_any_event(db, monkeypatch):
    workflow = _two_step_workflow(db)
    run = WorkflowService(db, OWNER).create_run(workflow.id, {"topic": "编排"})
    _stub_model(monkeypatch, {"writer": "x", "scorer": "y"}.get)

    # 门槛在第一步之前：没配好模型时该立刻给出明确的「去配置」，而不是让每步各自
    # 失败一遍。运行停在 pending，不会留下一串失败步骤。
    with pytest.raises(BusinessError):
        _run_all(db, OWNER, run.id)
    assert WorkflowService(db, OWNER).get_run(run.id).status == STEP_PENDING


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
