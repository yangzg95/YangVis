"""创作模块大纲生成走工作流引擎的集成测试（M2）。

验收点与幻灯片链路同形：模型调用由引擎执行（``exec.run_agent`` 打桩，不碰真实
服务商），现场落进 ``workflow_run`` / ``workflow_run_step``，作品行的状态由认领
它的最近一次运行翻译；后处理（大纲清洗、part 对账、空产物降级）在引擎之外。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entities import ModelConfig, WorkflowRun, WorkflowRunStep
from app.models.schemas import ModelPurpose, WritingWorkCreate
from app.services.workflows import exec as workflow_exec
from app.services.writing import WritingService
from app.services.workflows.builtin import seed_builtin_workflows
from app.services.workflows.runs import latest_run
from app.services.workflows import (
    RUN_FAILED,
    RUN_SUCCEEDED,
    STEP_SUCCEEDED,
    SUBJECT_ANALYZING,
    SUBJECT_ERROR,
    SUBJECT_READY,
    SUBJECT_UPLOADED,
    SUBJECT_WRITING_WORK,
)

OWNER = 1

NOVEL_OUTPUT = (
    "```json\n"
    '{"outline":[{"title":"第一卷 出山","chapters":['
    '{"title":"第一章 雨夜下山","synopsis":"少年奉师命下山入红尘","target_words":3000},'
    '{"title":"第二章 初入州城","synopsis":"城里的第一夜与冲突","target_words":2500}]},'
    '{"title":"第三章 独行","synopsis":"没有卷包裹的顶层章","target_words":2000}]}\n'
    "```"
)

ARTICLE_OUTPUT = (
    '{"title_candidates":["标题甲","标题乙","标题丙"],'
    '"sections":['
    '{"title":"开篇：问题是什么","point":"先立住论点","target_words":800},'
    '{"title":"收尾：怎么做","point":"给行动建议","target_words":600}]}'
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """文件库 + 可用对话模型 + seed 过的内置工作流（同 test_workflow_flows 的理由）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'writing_flows.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    monkeypatch.setattr("app.database.SessionLocal", maker)
    # 站内信的 per-owner 锁缓存在模块级，会绑到上一个用例 asyncio.run 的事件循环上；
    # 每条用例一个新循环，这里清掉让锁重新绑定（dispatch 自己吞异常，但日志要干净）。
    from app.services.notify import app_inbox

    app_inbox._owner_locks.clear()

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
    """把模型调用换成假的；reply 可以是字符串、异常或按 agent_slug 取回复的字典。"""
    calls: list[tuple[str, str]] = []

    def stub(reply):
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
            value = reply.get(agent_slug, "") if isinstance(reply, dict) else reply
            if isinstance(value, Exception):
                raise value
            return value() if callable(value) else value

        monkeypatch.setattr(workflow_exec, "run_agent", fake_run_agent)
        return calls

    return stub


def _read_after_engine_writes(db):
    """步骤写在另一条连接上：结束本会话事务再读，否则拿到旧快照。"""
    db.rollback()


def _service(db) -> WritingService:
    return WritingService(db, OWNER)


def _work(db, kind: str = "novel"):
    return _service(db).create_work(
        WritingWorkCreate(
            kind=kind,
            title="雨夜书" if kind == "novel" else "公众号一篇",
            brief="少年下山的故事" if kind == "novel" else "聊效率工具的主题素材",
            params={"genre": "玄幻", "tone": "热血"} if kind == "novel" else {"audience": "产品经理"},
        )
    )


def _run(db) -> WorkflowRun:
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.subject_kind == SUBJECT_WRITING_WORK))
    assert run is not None
    return run


# ---- 小说 ---------------------------------------------------------------------


def test_novel_outline_runs_through_the_workflow(db, model_call):
    calls = model_call(NOVEL_OUTPUT)
    service = _service(db)
    work = _work(db)

    started = service.start_outline_generation(work.id)
    assert service.to_item(started).status == SUBJECT_ANALYZING

    asyncio.run(service.run_outline_generation(work.id))
    _read_after_engine_writes(db)

    item = service.to_item(service.get_work(work.id))
    assert item.status == SUBJECT_READY and item.error_msg is None

    detail = service.to_detail(service.get_work(work.id))
    assert detail.part_count == 3, "三个叶子各建一个空正文块"
    volume = detail.outline[0]
    assert volume.kind == "volume" and volume.title == "第一卷 出山"
    assert [c.title for c in volume.children] == ["第一章 雨夜下山", "第二章 初入州城"]
    assert volume.children[0].target_words == 3000
    assert detail.outline[1].kind == "chapter" and detail.outline[1].title == "第三章 独行"

    run = _run(db)
    assert run.status == RUN_SUCCEEDED and run.subject_ref == str(work.id)
    step = db.scalar(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id))
    assert step.agent_slug == "novel-architect" and step.status == STEP_SUCCEEDED
    assert calls[0][0] == "novel-architect"
    assert "作品名：雨夜书" in step.prompt
    assert "题材：玄幻" in step.prompt and "基调：热血" in step.prompt
    assert "节点总数不要超过 120" in step.prompt


def test_novel_outline_failure_marks_error(db, model_call):
    model_call(RuntimeError("上游 502"))
    service = _service(db)
    work = _work(db)
    service.start_outline_generation(work.id)

    asyncio.run(service.run_outline_generation(work.id))
    _read_after_engine_writes(db)

    item = service.to_item(service.get_work(work.id))
    assert item.status == SUBJECT_ERROR and "上游 502" in (item.error_msg or "")
    assert _run(db).status == RUN_FAILED


def test_novel_outline_without_any_leaf_is_downgraded(db, model_call):
    """后处理失败：模型那步成功但一个合法章节都没有，运行必须降级，不能报「已完成」。"""
    model_call('{"outline": []}')
    service = _service(db)
    work = _work(db)
    service.start_outline_generation(work.id)

    asyncio.run(service.run_outline_generation(work.id))
    _read_after_engine_writes(db)

    item = service.to_item(service.get_work(work.id))
    assert item.status == SUBJECT_ERROR and "合法" in (item.error_msg or "")
    run = _run(db)
    assert run.status == RUN_FAILED
    step = db.scalar(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id))
    assert step.status == STEP_SUCCEEDED, "模型那一步是成功的，现场要留着"


def test_unparseable_output_is_downgraded(db, model_call):
    model_call("今天天气不错，没有 JSON。")
    service = _service(db)
    work = _work(db)
    service.start_outline_generation(work.id)

    asyncio.run(service.run_outline_generation(work.id))
    _read_after_engine_writes(db)

    item = service.to_item(service.get_work(work.id))
    assert item.status == SUBJECT_ERROR and "可解析" in (item.error_msg or "")


# ---- 文章 ---------------------------------------------------------------------


def test_article_outline_saves_sections_and_title_candidates(db, model_call):
    model_call(ARTICLE_OUTPUT)
    service = _service(db)
    work = _work(db, kind="article")
    service.start_outline_generation(work.id)

    asyncio.run(service.run_outline_generation(work.id))
    _read_after_engine_writes(db)

    saved = service.get_work(work.id)
    detail = service.to_detail(saved)
    assert service.to_item(saved).status == SUBJECT_READY
    assert [s.title for s in detail.outline] == ["开篇：问题是什么", "收尾：怎么做"]
    assert detail.outline[0].synopsis == "先立住论点"
    assert all(node.kind == "section" for node in detail.outline)
    assert saved.meta["title_candidates"] == ["标题甲", "标题乙", "标题丙"]
    assert _run(db).workflow_slug == "article-outline"


# ---- 运行闸门与模型选择 ---------------------------------------------------------


def test_start_blocks_double_trigger_and_delete(db):
    service = _service(db)
    work = _work(db)
    service.start_outline_generation(work.id)
    with pytest.raises(ValueError, match="重复触发"):
        service.start_outline_generation(work.id)
    with pytest.raises(ValueError, match="正在生成"):
        service.delete_work(work.id)


def test_start_requires_chat_model(db):
    from app.errors import BusinessError

    # 把唯一的默认模型改成未测通：门槛直接拒绝，不留运行。
    config = db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))
    config.last_test_ok = False
    db.commit()

    service = _service(db)
    work = _work(db)
    with pytest.raises(BusinessError):
        service.start_outline_generation(work.id)
    assert db.scalar(select(WorkflowRun)) is None


def test_chosen_model_is_stamped_on_run_steps(db):
    service = _service(db)
    work = _work(db)
    chosen = ModelConfig(
        owner_id=OWNER,
        purpose=ModelPurpose.CHAT.value,
        title="另一个对话模型",
        model_name="unit-test-pro",
        base_url="http://localhost:2/v1",
        is_default=False,
        last_test_ok=True,
    )
    db.add(chosen)
    db.commit()

    service.start_outline_generation(work.id, model_config_id=chosen.id)
    run = latest_run(db, OWNER, SUBJECT_WRITING_WORK, work.id)
    steps = list(db.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)).all())
    assert steps and all(step.model_config_id == chosen.id for step in steps)


def test_default_run_leaves_step_model_blank(db):
    service = _service(db)
    work = _work(db)
    service.start_outline_generation(work.id)
    run = latest_run(db, OWNER, SUBJECT_WRITING_WORK, work.id)
    steps = list(db.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)).all())
    # 空着 = 引擎执行时回落默认对话模型。
    assert steps and all(step.model_config_id is None for step in steps)


def test_chosen_model_must_belong_to_owner(db):
    service = _service(db)
    work = _work(db)
    with pytest.raises(LookupError):
        service.start_outline_generation(work.id, model_config_id=999999)


def test_requirement_override_is_snapshotted(db):
    service = _service(db)
    work = _work(db)
    service.start_outline_generation(work.id, requirement="  别写感情戏  ")
    db.refresh(work)
    assert work.requirement == "别写感情戏"
    run = latest_run(db, OWNER, SUBJECT_WRITING_WORK, work.id)
    assert "别写感情戏" in run.inputs["requirement_note"]


def test_brand_new_work_idle_state_is_uploaded(db):
    service = _service(db)
    work = _work(db)
    # 没跑过又没有大纲叶子：待生成，而不是 ready/error。
    assert service.to_item(work).status == SUBJECT_UPLOADED


def test_manual_edits_are_blocked_while_outline_is_running(db):
    """运行期间整棵树随时可能被产物覆盖，所有改大纲/正文的入口统一冻结。"""
    service = _service(db)
    work = _work(db)
    # 先有一个现成章节，update/delete 闸门才有对象可挡。
    work, cid = service.add_part(work.id, "已有章节", None)
    service.start_outline_generation(work.id)

    with pytest.raises(ValueError, match="稍后再编辑"):
        service.save_outline(work.id, [])
    with pytest.raises(ValueError, match="稍后再编辑"):
        service.add_part(work.id, "新章节", None)
    with pytest.raises(ValueError, match="稍后再编辑"):
        service.update_part(work.id, cid, title=None, content="正在写的正文")
    with pytest.raises(ValueError, match="稍后再编辑"):
        service.delete_part(work.id, cid)


def test_manually_emptied_outline_after_success_shows_uploaded_not_error(
    db, model_call
):
    """succeeded 运行 + 空大纲只可能是生成后手工清空：回到待生成，不亮红色失败。"""
    model_call(NOVEL_OUTPUT)
    service = _service(db)
    work = _work(db)
    service.start_outline_generation(work.id)
    asyncio.run(service.run_outline_generation(work.id))
    _read_after_engine_writes(db)
    assert service.to_item(service.get_work(work.id)).status == SUBJECT_READY

    for part in service.to_detail(service.get_work(work.id)).parts:
        service.delete_part(work.id, part.cid)

    item = service.to_item(service.get_work(work.id))
    assert item.status == SUBJECT_UPLOADED and item.error_msg is None
