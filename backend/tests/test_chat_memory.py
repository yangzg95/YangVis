"""长期记忆与会话摘要（``services/chat/memory.py``）。

这个域此前一条用例都没有，而它的正确性全压在「界面上看不见」的地方：摘要到底
是不是全量重算（存了 checkpoint 就会在历史被改写后越算越歪）、记忆去重是不是
既比存量又比本批、上限到了是丢弃而不是悄悄覆盖、模型吐出不像 JSON 的东西时
必须跳过而不是炸掉后台任务。

跑文件型 SQLite；模型调用在 ``build_chat_model`` 那一层打桩，不连服务商。
异步入口统一用 ``asyncio.run``（本仓库没装 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from itertools import count
from types import SimpleNamespace
from typing import List

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entities import (
    Agent,
    ChatConversation,
    ChatMessage,
    ChatSummary,
    ModelConfig,
    UserMemory,
)
from app.models.schemas import ModelPurpose
from app.services.chat import memory as mem

OWNER = 1
OTHER = 2

wait = asyncio.run

_AGENT_SEQ = count()


# ---- 打桩：一次性非流式的模型调用 -------------------------------------------


class FakeChatModel:
    """``build_chat_model`` 的替身，记下每次调用的消息与返回内容。"""

    def __init__(self, state: dict) -> None:
        self._state = state

    async def ainvoke(self, messages):
        self._state["calls"].append(messages)
        return SimpleNamespace(content=self._state["reply"])


@pytest.fixture()
def model(monkeypatch) -> dict:
    state: dict = {"reply": "摘要正文", "calls": [], "kwargs": []}

    def fake_build(config, **kwargs):
        state["kwargs"].append(kwargs)
        return FakeChatModel(state)

    monkeypatch.setattr(mem, "build_chat_model", fake_build)
    return state


# ---- 造数据 -----------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'memory.db'}", connect_args={"check_same_thread": False}
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


def chat_config(db) -> ModelConfig:
    return db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))


def make_conversation(db, *, owner_id: int = OWNER) -> ChatConversation:
    row = ChatConversation(owner_id=owner_id, title="记忆用例会话")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_messages(db, conversation_id: int, pairs: int, *, owner_id: int = OWNER, content: str = "") -> List[ChatMessage]:
    """造 ``pairs`` 轮问答（user + assistant 各一条），按时间顺序返回。

    正文只写「第 N 轮」，角色前缀留给被测代码的渲染去加——这样断言能同时盯住
    内容和标签两处。
    """
    rows: List[ChatMessage] = []
    for turn in range(pairs):
        for role in ("user", "assistant"):
            row = ChatMessage(
                owner_id=owner_id,
                conversation_id=conversation_id,
                role=role,
                content=content or f"第 {turn} 轮",
            )
            db.add(row)
            rows.append(row)
    db.commit()
    return rows


def make_agent(db, *, use_memory: bool = True, owner_id: int = OWNER) -> Agent:
    row = Agent(
        owner_id=owner_id,
        # 一条用例里会连造两个人设（开了记忆 / 没开），slug 上有唯一约束。
        slug=f"memory-agent-{next(_AGENT_SEQ)}",
        name="记忆测试人设",
        system_prompt="你是一个助手。",
        use_knowledge=False,
        use_memory=use_memory,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def svc(db, owner_id: int = OWNER) -> mem.MemoryService:
    return mem.MemoryService(db, owner_id)


def memory_row(db, content: str, *, owner_id: int = OWNER, updated_at=None) -> UserMemory:
    row = UserMemory(owner_id=owner_id, content=content)
    if updated_at is not None:
        row.updated_at = updated_at
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- 一、记忆 CRUD 与 owner 隔离 ---------------------------------------------


def test_create_strips_and_truncates_to_column_width(db):
    """入库前收边：首尾空白去掉，长度截到 String(512)——超了 SQLAlchemy 不报错，是 MySQL 才截。"""
    row = svc(db).create(f"  {'事实' * 400}  ")
    assert len(row.content) == 512
    assert svc(db).create("  用户在杭州工作  ").content == "用户在杭州工作"


def test_create_records_source_conversation(db):
    conv = make_conversation(db)
    row = svc(db).create("用户偏好简体中文", source_conversation_id=conv.id)
    assert row.source_conversation_id == conv.id


def test_list_only_returns_this_owner_and_orders_by_recency(db):
    base = datetime(2026, 10, 12, 10, 0, 0)
    old = memory_row(db, "很久以前", updated_at=base)
    new = memory_row(db, "最近更新", updated_at=base + timedelta(days=3))
    memory_row(db, "别人的记忆", owner_id=OTHER, updated_at=base + timedelta(days=9))

    rows = svc(db).list()
    assert [row.id for row in rows] == [new.id, old.id]


def test_list_limit_takes_newest_items(db):
    base = datetime(2026, 10, 12, 10, 0, 0)
    for index in range(5):
        memory_row(db, f"记忆 {index}", updated_at=base + timedelta(hours=index))
    rows = svc(db).list(limit=2)
    assert [row.content for row in rows] == ["记忆 4", "记忆 3"]


def test_get_and_update_reject_other_owner_rows(db):
    row = memory_row(db, "别人的", owner_id=OTHER)
    with pytest.raises(LookupError):
        svc(db).get(row.id)
    with pytest.raises(LookupError):
        svc(db).update(row.id, "改写别人的记忆")

    mine = memory_row(db, "我的")
    assert svc(db).update(mine.id, "  改过的记忆  ").content == "改过的记忆"


def test_delete_removes_only_the_owner_row(db):
    mine = memory_row(db, "我的")
    theirs = memory_row(db, "别人的", owner_id=OTHER)
    with pytest.raises(LookupError):
        svc(db).delete(theirs.id)
    svc(db).delete(mine.id)
    assert svc(db).list() == []


def test_clear_returns_count_and_leaves_other_owners(db):
    memory_row(db, "我的 A")
    memory_row(db, "我的 B")
    theirs = memory_row(db, "别人的", owner_id=OTHER)
    assert svc(db).clear() == 2
    assert svc(db).list() == []
    assert svc(db, OTHER).list()[0].id == theirs.id


def test_clear_on_empty_returns_zero_without_error(db):
    assert svc(db).clear() == 0


# ---- 二、注入 system prompt 的清单 -------------------------------------------


def test_prompt_section_is_blank_without_memories(db):
    """没有记忆时返回空串——调用方靠「空串」跳过整段注入，不能是 None 或换行。"""
    assert svc(db).prompt_section() == ""


def test_prompt_section_renders_bullets_newest_first(db):
    base = datetime(2026, 10, 12, 10, 0, 0)
    memory_row(db, "在 Hangzhou 工作", updated_at=base)
    memory_row(db, "偏好简体中文", updated_at=base + timedelta(days=1))
    assert svc(db).prompt_section() == "- 偏好简体中文\n- 在 Hangzhou 工作"


def test_prompt_section_caps_at_the_budget_with_the_newest_items(db):
    """注入上限是 30 条：清单再长也只会挤爆 system prompt，超出的按最旧丢弃。"""
    base = datetime(2026, 1, 1, 0, 0, 0)
    for index in range(mem.MEMORY_PROMPT_LIMIT + 5):
        memory_row(db, f"记忆 {index}", updated_at=base + timedelta(minutes=index))
    section = svc(db).prompt_section()
    lines = section.splitlines()
    assert len(lines) == mem.MEMORY_PROMPT_LIMIT
    assert lines[0] == f"- 记忆 {mem.MEMORY_PROMPT_LIMIT + 4}"
    assert "- 记忆 0" not in section and "- 记忆 4" not in section


# ---- 三、摘要：窗口、全量重算与退化到合并 --------------------------------------


def test_summary_for_missing_returns_blank_and_delete_is_scoped(db):
    conv = make_conversation(db)
    other = make_conversation(db, owner_id=OTHER)
    db.add(ChatSummary(conversation_id=conv.id, owner_id=OWNER, summary="本会话摘要"))
    db.add(ChatSummary(conversation_id=other.id, owner_id=OTHER, summary="别人的摘要"))
    db.commit()

    assert svc(db).summary_for(conv.id) == "本会话摘要"
    assert svc(db).summary_for(other.id) == ""  # 读不到别人的会话摘要

    svc(db).delete_summary(other.id)  # 不许越权删
    assert svc(db).summary_for(conv.id) == "本会话摘要"
    assert db.get(ChatSummary, other.id).summary == "别人的摘要"


def test_render_labels_user_rows_and_treats_everything_else_as_assistant(db):
    rows = [
        ChatMessage(owner_id=OWNER, conversation_id=1, role="user", content="提问"),
        ChatMessage(owner_id=OWNER, conversation_id=1, role="assistant", content="回答"),
        ChatMessage(owner_id=OWNER, conversation_id=1, role="system", content="别的角色"),
    ]
    assert mem.MemoryService._render(rows) == "用户：提问\n\n助手：回答\n\n助手：别的角色"


def test_refresh_within_window_drops_summary_without_calling_the_model(db, model):
    """历史还在回放窗口内就没有摘要可言——旧摘要要删掉，而且不值得花一次模型调用。"""
    conv = make_conversation(db)
    add_messages(db, conv.id, 2)  # 4 条 < HISTORY_TURNS*2
    db.add(ChatSummary(conversation_id=conv.id, owner_id=OWNER, summary="曾经长过"))
    db.commit()

    wait(svc(db).refresh_summary(conv, chat_config(db), history_turns=6))

    assert svc(db).summary_for(conv.id) == ""
    assert model["calls"] == []


def test_refresh_sends_overflow_history_and_upserts_one_row(db, model):
    """窗口之前的消息全部参与重算（不存 checkpoint），一个会话始终只有一行摘要。"""
    conv = make_conversation(db)
    add_messages(db, conv.id, 5)  # 10 条，窗口 12 之内先造短；下面用 history_turns=1 逼出溢出
    service = svc(db)

    model["reply"] = "  用户给出了三份配置数字  "
    wait(service.refresh_summary(conv, chat_config(db), history_turns=1))
    assert service.summary_for(conv.id) == "用户给出了三份配置数字"

    system, human = model["calls"][0]
    assert system.content == mem._SUMMARY_SYSTEM_FULL
    assert "用户：第 0 轮" in human.content and "助手：第 0 轮" in human.content
    # 窗口 = history_turns*2 = 2 条，最后两条不进摘要
    assert "第 4 轮" not in human.content

    model["reply"] = "第二次重算"
    wait(service.refresh_summary(conv, chat_config(db), history_turns=1))
    assert service.summary_for(conv.id) == "第二次重算"
    assert len(db.scalars(select(ChatSummary)).all()) == 1


def test_refresh_passes_the_background_task_budget(db, model):
    conv = make_conversation(db)
    add_messages(db, conv.id, 4)
    wait(svc(db).refresh_summary(conv, chat_config(db), history_turns=1))
    assert model["kwargs"][0] == {"temperature": 0.2, "timeout": mem.MEMORY_TASK_TIMEOUT}


def test_refresh_empty_model_output_keeps_previous_summary(db, model):
    """模型回空串就当这次没发生：既写入「空摘要」也没意义，留着旧的更可用。"""
    conv = make_conversation(db)
    add_messages(db, conv.id, 4)
    service = svc(db)
    db.add(ChatSummary(conversation_id=conv.id, owner_id=OWNER, summary="旧的还在"))
    db.commit()

    model["reply"] = "   "
    wait(service.refresh_summary(conv, chat_config(db), history_turns=1))

    assert service.summary_for(conv.id) == "旧的还在"


def test_refresh_clamps_model_output_to_the_hard_limit(db, model):
    """prompt 里要求 800 字，这是模型不听话时的兜底。"""
    conv = make_conversation(db)
    add_messages(db, conv.id, 4)
    model["reply"] = "字" * (mem.SUMMARY_MAX_CHARS + 300)
    wait(svc(db).refresh_summary(conv, chat_config(db), history_turns=1))
    assert svc(db).summary_for(conv.id) == "字" * mem.SUMMARY_MAX_CHARS


def test_refresh_degrades_to_merge_mode_with_prior_summary(db, model):
    """溢出部分渲染后超过喂给模型的上限，就退化成「旧摘要 + 尾部一段」的合并。"""
    conv = make_conversation(db)
    # 溢出段渲染后要撑过 SUMMARY_INPUT_LIMIT：8 轮里 14 条进溢出，每条 1320 字。
    filler = "很长的一段历史内容，用来把渲染文本撑过上限。" * 60
    add_messages(db, conv.id, 8, content=filler)
    db.add(ChatSummary(conversation_id=conv.id, owner_id=OWNER, summary="已知的旧摘要"))
    db.commit()

    model["reply"] = "合并后的摘要"
    wait(svc(db).refresh_summary(conv, chat_config(db), history_turns=1))

    system, human = model["calls"][0]
    overflow = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.owner_id == OWNER, ChatMessage.conversation_id == conv.id)
            .order_by(ChatMessage.id)
        ).all()
    )[:-2]
    assert len(mem.MemoryService._render(overflow)) > mem.SUMMARY_INPUT_LIMIT
    assert system.content == mem._SUMMARY_SYSTEM_MERGE
    assert "已知的旧摘要" in human.content
    assert human.content.endswith("很长的一段历史内容，用来把渲染文本撑过上限。")


def test_refresh_ignores_other_owner_messages_in_the_same_conversation(db, model):
    """消息表按 owner 限定：别人的消息哪怕挂着同一个 conversation_id 也不进摘要。"""
    conv = make_conversation(db)
    add_messages(db, conv.id, 3)
    add_messages(db, conv.id, 3, owner_id=OTHER)
    wait(svc(db).refresh_summary(conv, chat_config(db), history_turns=1))

    assert len(model["calls"]) == 1
    human = model["calls"][0][1].content
    # 本 owner 6 条，窗口占掉末尾 2 条，溢出只剩 4 条；owner 条件一漏就变 10 条
    assert human.count("第 0 轮") == 2 and human.count("第 1 轮") == 2
    assert "第 2 轮" not in human


# ---- 四、记忆提取：门槛、去重、上限与容错 --------------------------------------


def test_extract_skips_when_agent_missing_or_memory_disabled(db, model):
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    config = chat_config(db)

    wait(svc(db).extract_memories(conv, config, None))
    wait(svc(db).extract_memories(conv, config, make_agent(db, use_memory=False)))

    assert model["calls"] == []
    assert svc(db).list() == []


def test_extract_needs_a_whole_turn(db, model):
    """只有提问没有回答时不提取：答话里才藏着「用户是谁、在做什么」。"""
    conv = make_conversation(db)
    db.add(ChatMessage(owner_id=OWNER, conversation_id=conv.id, role="user", content="只有一句"))
    db.commit()
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))
    assert model["calls"] == []


def test_extract_feeds_only_the_last_two_messages_and_caps_each(db, model):
    conv = make_conversation(db)
    long_content = "长" * (mem.EXTRACT_MESSAGE_LIMIT + 500)
    add_messages(db, conv.id, 3, content="早期对话")
    db.add(ChatMessage(owner_id=OWNER, conversation_id=conv.id, role="user", content=long_content))
    db.add(ChatMessage(owner_id=OWNER, conversation_id=conv.id, role="assistant", content="收到"))
    db.commit()

    model["reply"] = '{"operations": []}'
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))

    human = model["calls"][0][1].content
    assert "早期对话" not in human
    assert "长" * mem.EXTRACT_MESSAGE_LIMIT in human
    assert "长" * (mem.EXTRACT_MESSAGE_LIMIT + 1) not in human
    assert human.index("用户：") < human.index("助手：收到")


def test_extract_lists_existing_memories_with_ids(db, model):
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    mine = memory_row(db, "在 Hangzhou 工作")

    model["reply"] = '{"operations": []}'
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))
    assert f"- id={mine.id} 在 Hangzhou 工作" in model["calls"][0][1].content

    svc(db).clear()
    model["calls"].clear()
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))
    assert "已有记忆清单：\n（空）" in model["calls"][0][1].content


def test_extract_add_persists_with_source_conversation(db, model):
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    model["reply"] = '{"operations": [{"action": "add", "content": "  用户是一名后端工程师  "}]}'
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))

    rows = svc(db).list()
    assert [row.content for row in rows] == ["用户是一名后端工程师"]
    assert rows[0].source_conversation_id == conv.id


def test_extract_dedupes_against_existing_and_within_the_batch(db, model):
    """去重比的是「收边 + casefold 后全等」：既比存量，也比本批刚加进去的。

    近义、多一个空格都不算重复——那是提取模型该用 update 合并的事，不是这里猜的。
    """
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    memory_row(db, "用户在杭州工作")
    model["reply"] = (
        '{"operations": ['
        '{"action": "add", "content": "用户在杭州工作"},'
        '{"action": "add", "content": "  用户负责一个内部平台  "},'
        '{"action": "add", "content": "用户负责一个内部平台"},'
        '{"action": "add", "content": "用户偏好简体中文"}]}'
    )
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))

    rows = svc(db).list()
    assert len(rows) == 3
    assert {row.content for row in rows} == {
        "用户在杭州工作",
        "用户负责一个内部平台",
        "用户偏好简体中文",
    }


def test_extract_drops_new_items_once_at_the_cap(db, model):
    """到上限就丢弃并记日志：无上限增长的清单最终会把 system prompt 淹没。"""
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    for index in range(mem.MEMORY_MAX_ITEMS):
        memory_row(db, f"已有记忆 {index}")

    model["reply"] = '{"operations": [{"action": "add", "content": "再多一条"}]}'
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))

    assert svc(db).clear() == mem.MEMORY_MAX_ITEMS


def test_extract_update_and_delete_only_touch_known_ids(db, model):
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    known = memory_row(db, "旧的事实")
    other = memory_row(db, "别人的记忆", owner_id=OTHER)

    model["reply"] = (
        '{"operations": ['
        f'{{"action": "update", "id": {known.id}, "content": "更新后的事实"}},'
        '{"action": "update", "id": 4242, "content": "凭空的 id"},'
        '{"action": "update", "id": 4242, "content": ""},'
        f'{{"action": "delete", "id": {other.id}}}]}}'
    )
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))

    assert [row.content for row in svc(db).list()] == ["更新后的事实"]
    assert svc(db, OTHER).list()[0].content == "别人的记忆"


def test_extract_add_without_content_is_ignored(db, model):
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    model["reply"] = '{"operations": [{"action": "add"}, {"action": "add", "content": "   "}]}'
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))
    assert svc(db).list() == []


def test_extract_survives_bad_id_values(db, model):
    """``int(op["id"])`` 抛的不是 LookupError：id 形态乱七八糟时跳过该条而不是崩任务。"""
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    known = memory_row(db, "旧的事实")
    model["reply"] = (
        '{"operations": ['
        '{"action": "delete", "id": "abc"},'
        f'{{"action": "update", "id": "{known.id}", "content": "字符串 id 也能认"}}]}}'
    )
    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))
    assert [row.content for row in svc(db).list()] == ["字符串 id 也能认"]


# ---- 五、_parse_operations：跳过不算错误 -------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "这一轮没有值得记住的内容。",  # 一个花括号都没有
        '{"operations": [',  # 半截 JSON
        '{"operations": {"action": "add"}}',  # 不是 list
        '{"note": "没有 operations 这个键"}',
        '{"operations": ["不是对象", null, 3]}',  # 全是非 dict
    ],
)
def test_parse_operations_leaves_memories_untouched(db, model, reply):
    """后台任务对模型的输出完全没有信任：这一轮跳过，一条记忆也不许多动。"""
    conv = make_conversation(db)
    add_messages(db, conv.id, 1)
    keep = memory_row(db, "已经存在的记忆")
    model["reply"] = reply

    wait(svc(db).extract_memories(conv, chat_config(db), make_agent(db)))

    assert [row.content for row in svc(db).list()] == ["已经存在的记忆"]
    assert svc(db).get(keep.id).content == "已经存在的记忆"


def test_parse_operations_grabs_the_braces_out_of_prose(db):
    """模型爱在 JSON 前后加解释：正则取首尾花括号之间的内容，脏输出也能用。"""
    conv = make_conversation(db)
    parsed = svc(db)._parse_operations(
        '好的，我分析一下。\n```json\n{"operations": [{"action": "add", "content": "事实"}]}\n```\n以上。',
        conv.id,
    )
    assert parsed == [{"action": "add", "content": "事实"}]


def test_parse_operations_keeps_dict_items_and_drops_the_rest(db):
    conv = make_conversation(db)
    parsed = svc(db)._parse_operations(
        '{"operations": ["跳过", {"action": "add", "content": "留下"}, 7]}', conv.id
    )
    assert parsed == [{"action": "add", "content": "留下"}]


# ---- 六、_message_text：provider 返回形态不一 ---------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("纯文本", "纯文本"),
        ([{"type": "text", "text": "一段"}, {"text": "两段"}], "一段两段"),
        (["裸字符串块"], "裸字符串块"),
        (123, "123"),
        (None, "None"),
    ],
)
def test_message_text_flattens_content_blocks(raw, expected):
    assert mem._message_text(raw) == expected


def test_ask_text_reads_content_from_the_model_result(db, monkeypatch):
    """``ainvoke`` 返回的对象没有 content 属性时退回对象本身——两种 provider 形态都要能取到文本。"""
    seen: dict = {}

    class NoContent:
        pass

    class WeirdModel:
        async def ainvoke(self, messages):
            seen["messages"] = messages
            return NoContent()

    monkeypatch.setattr(mem, "build_chat_model", lambda config, **kw: WeirdModel())
    text = wait(mem._ask_text(chat_config(db), "系统", "用户", temperature=0.0))
    assert "NoContent object" in text  # getattr(result, "content", result) 退回对象本身
    assert [type(m).__name__ for m in seen["messages"]] == ["SystemMessage", "HumanMessage"]