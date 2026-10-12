"""模型配置的删除语义（``services/model_config.py:ModelConfigService.delete``）。

这个套件目前只钉删除这一件事：引用它的会话与个人工作流步骤要跟着清空——不清就
留着悬空 id，之后按 id 取配置撞的是 ``LookupError``（主链路变成用户看不懂的 404，
后台收尾任务里那个异常根本没人接）；``workflow_run_step`` 里那一列是「这一步当时
实际用了哪份」的留痕，不许跟着清；删掉默认那份要顶上另一份验证过的，免得功能悄无
声息地不可用。模块其余部分（列表、掩码、凭据轮换）仍是测试缺口。

跑文件型 SQLite；不连服务商，也不碰共享 MySQL。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entities import (
    ChatConversation,
    ModelConfig,
    Workflow,
    WorkflowRunStep,
    WorkflowStep,
)
from app.models.schemas import ModelPurpose
from app.services.chat import ChatService
from app.services.model_config import ModelConfigService

OWNER = 1
OTHER = 2


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'model_config.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_config(
    db, *, owner_id: int = OWNER, title: str = "配置", is_default: bool = False,
    last_test_ok: bool = True, purpose: ModelPurpose = ModelPurpose.CHAT,
) -> ModelConfig:
    row = ModelConfig(
        owner_id=owner_id,
        purpose=purpose.value,
        title=title,
        model_name="unit-test",
        base_url="http://localhost:1/v1",
        is_default=is_default,
        last_test_ok=last_test_ok,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_conversation(db, config: ModelConfig, *, owner_id: int = OWNER) -> ChatConversation:
    row = ChatConversation(owner_id=owner_id, title="引用了配置的会话", model_config_id=config.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_step(db, config: ModelConfig, *, key: str = "s1") -> WorkflowStep:
    workflow = Workflow(owner_id=OWNER, slug=f"wf-{key}", name="删除用例会跑的流程")
    db.add(workflow)
    db.commit()
    step = WorkflowStep(
        workflow_id=workflow.id,
        key=key,
        group_key=key,
        sort_order=0,
        agent_slug="general-chat",
        prompt_tpl="{{input}}",
        model_config_id=config.id,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return step


def svc(db) -> ModelConfigService:
    return ModelConfigService(db, OWNER)


# ---- 一、引用清理 -------------------------------------------------------------


def test_delete_clears_the_conversation_reference_and_the_chat_gate_survives(db):
    """删掉会话引用的配置后要退回默认配置，而不是留下悬空 id 让下一次回答 404。"""
    chosen = make_config(db, title="被删的那份")
    keep = make_config(db, title="默认那份", is_default=True)
    conversation = make_conversation(db, chosen)
    db.refresh(conversation)

    svc(db).delete(chosen.id)

    db.expire_all()
    assert conversation.model_config_id is None
    # 真正要坐实的是读路径：以前这一句抛 LookupError，收尾任务不接那个异常。
    assert svc(db).get_default(ModelPurpose.CHAT).id == keep.id
    assert ChatService(db, OWNER).require_chat_config(conversation.model_config_id).id == keep.id


def test_delete_clears_workflow_step_but_not_the_run_record(db):
    """步骤定义是「以后用哪份」，要清；运行留痕是「当时用了哪份」，留下。"""
    chosen = make_config(db, title="步骤指定那份")
    step = make_step(db, chosen)
    trace = WorkflowRunStep(
        owner_id=OWNER,
        run_id=1,
        key=step.key,
        group_key=step.group_key,
        agent_slug=step.agent_slug,
        model_config_id=chosen.id,
    )
    db.add(trace)
    db.commit()

    svc(db).delete(chosen.id)

    db.expire_all()
    assert step.model_config_id is None
    assert trace.model_config_id == chosen.id


def test_delete_rejects_another_owner_config_and_leaves_other_rows(db):
    """别人的配置删不掉（404），指向别的配置的会话也不该被顺手抹掉。"""
    other_owner = make_config(db, owner_id=OTHER, title="别人的配置")
    chosen = make_config(db, title="被删的那份")
    keep = make_config(db, title="留下那份")
    doomed = make_conversation(db, chosen)
    survivor = make_conversation(db, keep)
    theirs = make_conversation(db, other_owner, owner_id=OTHER)

    with pytest.raises(LookupError):
        svc(db).delete(other_owner.id)

    svc(db).delete(chosen.id)

    db.expire_all()
    assert doomed.model_config_id is None
    assert survivor.model_config_id == keep.id
    assert theirs.model_config_id == other_owner.id


# ---- 二、默认顶上 -------------------------------------------------------------


def test_delete_of_default_promotes_a_verified_replacement(db):
    keep = make_config(db, title="验证过的另一份")
    doomed = make_config(db, title="默认这份", is_default=True)

    svc(db).delete(doomed.id)

    db.expire_all()
    assert svc(db).get_default(ModelPurpose.CHAT).id == keep.id
    assert keep.is_default is True


def test_delete_of_default_without_a_verified_fallback_leaves_none(db):
    """没验证过的不能顶：宁可可读地「还没配模型」，也不要悄悄拿一份坏配置去调用。"""
    make_config(db, title="没测过", last_test_ok=False)
    doomed = make_config(db, title="默认这份", is_default=True)

    svc(db).delete(doomed.id)

    assert svc(db).get_default(ModelPurpose.CHAT) is None
    assert db.scalar(select(ModelConfig).where(ModelConfig.is_default.is_(True))) is None
