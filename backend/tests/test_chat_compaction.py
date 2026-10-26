"""手动对话压缩（compaction）的服务层语义。

自动滚动摘要那半边在 ``test_chat_memory.py`` 已钉死，这里只钉新增的东西：

- 压缩锚点：锚点及之前的消息永不回放，锚点之后的窗口切点仍不拆问答对；
- 切点计划：没有可压缩内容 / 压缩点没有前移都要被拒；
- 预览不落库，应用才落库；再次压缩是「旧锚点 + 新老化部分」的渐进式；
- 自动重算在有锚点时把锚点前历史永远算作摘要段，并沿用用户的压缩要求；
- 取消压缩后回放恢复自动窗口口径。

跑文件型 SQLite；模型调用在 ``build_chat_model`` 那一层打桩，不连服务商。
异步入口统一用 ``asyncio.run``（本仓库没装 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
from itertools import count
from types import SimpleNamespace
from typing import List

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.errors import BusinessError
from app.models.entities import ChatConversation, ChatMessage, ChatSummary, ModelConfig
from app.models.schemas import ModelPurpose
from app.services.chat import memory as mem
from app.services.chat.service import HISTORY_TURNS, ChatService

OWNER = 1
wait = asyncio.run
_SEQ = count()


# ---- 打桩：流式模型 ----------------------------------------------------------


class FakeStreamingModel:
    """``build_chat_model`` 的替身：流式逐字吐、非流式整段回，入参都记下。

    ``astream`` 是异步生成器函数（与 langchain 同形：调用即得异步生成器，
    不是协程）；底层流是否被 aclose 靠生成器的 finally 观察。
    """

    def __init__(self, state: dict) -> None:
        self.state = state

    async def astream(self, messages):
        self.state["calls"].append(messages)
        try:
            for piece in self.state["reply"]:
                yield SimpleNamespace(content=piece)
        finally:
            self.state["closed"] = True

    async def ainvoke(self, messages):
        self.state["calls"].append(messages)
        return SimpleNamespace(content=self.state["reply"])


@pytest.fixture()
def model(monkeypatch) -> dict:
    state: dict = {"reply": "压缩后的摘要", "calls": [], "closed": False}
    monkeypatch.setattr(mem, "build_chat_model", lambda config, **kw: FakeStreamingModel(state))
    return state


# ---- 造数据 -----------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'compaction.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", maker)
    session = maker()
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


def make_conversation(db) -> ChatConversation:
    row = ChatConversation(owner_id=OWNER, title=f"压缩用例会话-{next(_SEQ)}")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_pairs(db, conversation_id: int, pairs: int) -> List[ChatMessage]:
    rows: List[ChatMessage] = []
    for turn in range(pairs):
        for role in ("user", "assistant"):
            row = ChatMessage(
                owner_id=OWNER,
                conversation_id=conversation_id,
                role=role,
                content=f"第 {turn} 轮{'提问' if role == 'user' else '回答'}",
            )
            db.add(row)
            rows.append(row)
    db.commit()
    return rows


def service(db) -> ChatService:
    return ChatService(db, OWNER)


async def _collect(agen) -> list:
    return [item async for item in agen]


def summary_row(db, conversation_id: int) -> ChatSummary:
    return db.scalar(
        select(ChatSummary).where(ChatSummary.conversation_id == conversation_id)
    )


# ---- 一、split_with_anchor ---------------------------------------------------


def test_split_without_anchor_matches_window_split():
    rows = [SimpleNamespace(id=i, role="user" if i % 2 else "assistant") for i in range(1, 9)]
    overflow, replay = mem.split_with_anchor(rows, 4, None)
    plain_overflow, plain_replay = mem.split_replay_window(rows, 4)
    assert [r.id for r in overflow] == [r.id for r in plain_overflow]
    assert [r.id for r in replay] == [r.id for r in plain_replay]


def test_split_with_anchor_keeps_head_in_overflow_forever():
    rows = [SimpleNamespace(id=i, role="user" if i % 2 else "assistant") for i in range(1, 13)]
    # 锚点压在第 8 条：1..8 永远属于摘要段，窗口只在 9..12 上切。
    overflow, replay = mem.split_with_anchor(rows, 4, 8)
    assert [r.id for r in overflow] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [r.id for r in replay] == [9, 10, 11, 12]

    # 锚点之后又老化出窗口的消息：并进摘要段，锚点头原样保留。
    rows2 = rows + [
        SimpleNamespace(id=13, role="user"),
        SimpleNamespace(id=14, role="assistant"),
    ]
    overflow, replay = mem.split_with_anchor(rows2, 4, 8)
    assert [r.id for r in overflow] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert [r.id for r in replay] == [11, 12, 13, 14]


# ---- 二、plan_compaction ------------------------------------------------------


def test_plan_rejects_when_everything_is_within_the_keep_window(db):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 2)
    with pytest.raises(BusinessError):
        mem.MemoryService(db, OWNER).plan_compaction(conv.id, keep_turns=6)


def test_plan_cuts_on_pair_boundary_and_reports_counts(db):
    conv = make_conversation(db)
    rows = add_pairs(db, conv.id, 5)  # 10 条，保留 2 轮（4 条）
    cut, overflow, replay = mem.MemoryService(db, OWNER).plan_compaction(conv.id, keep_turns=2)
    assert cut == rows[5].id  # 溢出段 6 条，切点是第 6 条（第 3 轮的回答）
    assert [r.id for r in replay] == [r.id for r in rows[6:]]
    # 切点绝不落在一条用户提问之后（否则它的回答留在回放段，提问被摘成假待办）。
    assert overflow[-1].role == "assistant"


def test_plan_refuses_to_move_the_anchor_backward(db):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 5)
    memory = mem.MemoryService(db, OWNER)
    cut, _, _ = memory.plan_compaction(conv.id, keep_turns=1)
    memory.apply_compaction(conv.id, cut, "先压一版", None)

    # 保留 2 轮算出的切点比现有锚点更早：拒绝。
    with pytest.raises(BusinessError):
        memory.plan_compaction(conv.id, keep_turns=2)
    # 同样的切点再来一次也不行：压缩点没有扩大。
    with pytest.raises(BusinessError):
        memory.plan_compaction(conv.id, keep_turns=1)


# ---- 三、apply / clear --------------------------------------------------------


def test_apply_persists_anchor_instruction_and_clamps_summary(db):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 3)
    memory = mem.MemoryService(db, OWNER)
    cut, _, _ = memory.plan_compaction(conv.id, keep_turns=1)
    row = memory.apply_compaction(
        conv.id, cut, "  摘要正文  ", "  保留具体文件路径  "
    )
    assert row.cut_message_id == cut
    assert row.instruction == "保留具体文件路径"
    assert row.summary == "摘要正文"

    # 渐进式：再长两轮后用前移的新切点应用，顺带验证摘要被截到硬上限。
    add_pairs(db, conv.id, 2)
    next_cut, _, _ = memory.plan_compaction(conv.id, keep_turns=1)
    long_summary = "摘" * (mem.SUMMARY_MAX_CHARS + 99)
    row = memory.apply_compaction(conv.id, next_cut, long_summary, None)
    assert len(row.summary) == mem.SUMMARY_MAX_CHARS
    assert row.instruction is None
    assert row.cut_message_id == next_cut


def test_apply_validates_cut_and_text(db):
    conv = make_conversation(db)
    rows = add_pairs(db, conv.id, 3)
    memory = mem.MemoryService(db, OWNER)

    with pytest.raises(LookupError):
        memory.apply_compaction(conv.id, 999999, "摘要", None)
    # 切点压到最后一条：回放段空了。
    with pytest.raises(BusinessError):
        memory.apply_compaction(conv.id, rows[-1].id, "摘要", None)
    # 空摘要。
    with pytest.raises(ValueError):
        memory.apply_compaction(conv.id, rows[1].id, "   ", None)


def test_clear_restores_full_window_replay(db):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 5)
    memory = mem.MemoryService(db, OWNER)
    cut, _, _ = memory.plan_compaction(conv.id, keep_turns=1)
    memory.apply_compaction(conv.id, cut, "压掉前 8 条", None)
    assert [r.id for r in service(db)._history(conv.id)] == [9, 10]

    service(db).clear_compaction(conv.id)
    assert summary_row(db, conv.id) is None
    # 全部 10 条都在 6 轮窗口内，恢复全量回放。
    assert [r.id for r in service(db)._history(conv.id)] == list(range(1, 11))


# ---- 四、回放窗口与自动重算在锚点下的行为 -------------------------------------


def test_history_never_replays_messages_at_or_before_the_anchor(db):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 5)
    memory = mem.MemoryService(db, OWNER)
    cut, _, _ = memory.plan_compaction(conv.id, keep_turns=1)
    memory.apply_compaction(conv.id, cut, "锚点摘要", None)
    # 没锚点时 10 条全回放（都在 6 轮窗内）；有锚点只剩锚点之后 2 条。
    assert [r.id for r in service(db)._history(conv.id)] == [9, 10]
    assert mem.MemoryService(db, OWNER).anchor_id(conv.id) == cut


def test_refresh_keeps_the_anchor_and_folds_new_overflow_into_the_summary(db, model):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 5)
    memory = mem.MemoryService(db, OWNER)
    cut, _, _ = memory.plan_compaction(conv.id, keep_turns=1)
    memory.apply_compaction(conv.id, cut, "旧锚点摘要", "保留具体路径")

    # 再多两轮：锚点之后老化出窗口的消息（第 5 轮）必须并入摘要段。
    add_pairs(db, conv.id, 2)
    config = db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))
    model["reply"] = "重算后的摘要"
    wait(memory.refresh_summary(conv, config, history_turns=1))

    human = model["calls"][0][1].content
    assert "第 0 轮提问" in human and "第 4 轮提问" in human  # 锚点头 + 新老化部分
    assert "保留具体路径" in human  # 用户的压缩要求被自动重算沿用
    assert "第 6 轮提问" not in human  # 最后一轮留在回放窗口

    row = summary_row(db, conv.id)
    assert row.summary == "重算后的摘要"
    assert row.cut_message_id == cut  # 锚点不被自动重算挪动
    assert row.instruction == "保留具体路径"


def test_refresh_recomputes_head_only_when_nothing_aged_out(db, model):
    """锚点之后还没有消息老化时，自动重算仍只重算锚点头：摘要不丢。"""
    conv = make_conversation(db)
    add_pairs(db, conv.id, 5)
    memory = mem.MemoryService(db, OWNER)
    cut, _, _ = memory.plan_compaction(conv.id, keep_turns=2)
    memory.apply_compaction(conv.id, cut, "压掉 6 条", None)

    config = db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))
    wait(memory.refresh_summary(conv, config, history_turns=HISTORY_TURNS))
    assert len(model["calls"]) == 1
    assert summary_row(db, conv.id).cut_message_id == cut


# ---- 五、预览流（ChatService 门面）-------------------------------------------


def test_preview_streams_but_persists_nothing_until_apply(db, model):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 4)
    events = wait(_collect(
        service(db).compaction_preview(conversation_id=conv.id, keep_turns=1)
    ))

    names = [name for name, _ in events]
    assert names[0] == "compact_meta"
    assert names[-1] == "compact_done"
    assert "".join(data for name, data in events if name == "token") == "压缩后的摘要"
    meta = dict(events[0][1])
    assert meta["covered_count"] == 6 and meta["kept_count"] == 2
    assert events[-1][1]["cut_message_id"] == meta["cut_message_id"]
    # 预览不写库。
    assert summary_row(db, conv.id) is None
    assert model["closed"] is True


def test_preview_passes_instruction_and_apply_then_advances_the_anchor(db, model):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 5)
    svc = service(db)
    events = wait(_collect(
        svc.compaction_preview(
            conversation_id=conv.id, keep_turns=1, instruction="保留未决错误"
        )
    ))
    meta = events[0][1]
    tokens = "".join(data for name, data in events if name == "token")
    assert "保留未决错误" in model["calls"][0][1].content

    state = svc.apply_compaction(conv.id, meta["cut_message_id"], tokens, "保留未决错误")
    assert state.cut_message_id == meta["cut_message_id"]
    assert state.covered_count == 8
    assert state.instruction == "保留未决错误"

    # 渐进式：再长两轮后压一次，切点前移，旧摘要进入合并提示词。
    model["reply"] = "二版摘要，合并了旧锚点"
    add_pairs(db, conv.id, 2)
    events = wait(_collect(
        svc.compaction_preview(conversation_id=conv.id, keep_turns=1)
    ))
    meta_two = events[0][1]
    assert meta_two["cut_message_id"] > state.cut_message_id
    tokens_two = "".join(data for name, data in events if name == "token")
    svc.apply_compaction(conv.id, meta_two["cut_message_id"], tokens_two, "保留未决错误")
    assert summary_row(db, conv.id).cut_message_id == meta_two["cut_message_id"]


def test_preview_with_empty_model_output_raises_in_stream(db, monkeypatch):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 4)

    class EmptyModel:
        async def astream(self, messages):
            if False:  # pragma: no cover - 让它成为 async generator
                yield SimpleNamespace(content="")

        async def aclose(self):
            pass

    monkeypatch.setattr(mem, "build_chat_model", lambda config, **kw: EmptyModel())
    with pytest.raises(BusinessError):
        wait(_collect(
            service(db).compaction_preview(conversation_id=conv.id, keep_turns=1)
        ))


def test_preview_requires_usable_chat_model(db, monkeypatch):
    conv = make_conversation(db)
    add_pairs(db, conv.id, 4)
    config = db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))
    config.last_test_ok = False
    db.commit()
    with pytest.raises(BusinessError):
        wait(_collect(
            service(db).compaction_preview(conversation_id=conv.id, keep_turns=1)
        ))


# ---- 六、compaction_state -----------------------------------------------------


def test_state_is_none_without_summary_and_counts_covered(db, model):
    conv = make_conversation(db)
    svc = service(db)
    assert svc.compaction_state(conv.id) is None

    add_pairs(db, conv.id, HISTORY_TURNS + 2)  # 触发自动摘要的量级
    config = db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))
    wait(mem.MemoryService(db, OWNER).refresh_summary(conv, config, history_turns=HISTORY_TURNS))
    state = svc.compaction_state(conv.id)
    assert state is not None
    assert state.cut_message_id is None
    assert state.covered_count == 4  # 16 条 - 12 条窗口


def test_state_for_other_owner_is_404(db):
    conv = make_conversation(db)
    with pytest.raises(LookupError):
        ChatService(db, OWNER + 999).compaction_state(conv.id)
