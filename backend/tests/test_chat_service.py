"""对话主链路（``services/chat/service.py``）。

``test_ops_confirm*.py`` 已经把 stream_answer 的运维事件协议（step/exec/confirm/done、
步数上限、中断保存）钉住了，这个文件补的是剩下那半边：会话与消息的读写边界
（哪些行属于你、删会话时什么跟着走、改写只许动助手消息）、检索引用编号的稳定性
（[2] 一旦写下就不能换资料）、进 ReAct 循环之前那一串前置校验（项目、向量索引、
模型配置、人设可用性），以及问答落库后那个自己开 session 的收尾任务在坏消息面前
的分工。

跑文件型 SQLite；知识库检索、agent 图与模型调用全部打桩，不碰 Qdrant、也不连服务商。
异步入口统一用 ``asyncio.run``（本仓库没装 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from itertools import count
from types import SimpleNamespace
from typing import Awaitable, Callable, List, Optional

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.errors import CODE_CHAT_NOT_READY, BusinessError
from app.models.entities import (
    Agent,
    ChatConversation,
    ChatMessage,
    ChatSummary,
    ModelConfig,
    OpsPendingAction,
    UserMemory,
)
from app.models.schemas import Citation, ModelPurpose, SearchHit
from app.services.agents import AgentService, seed_builtin_agents
from app.services.chat import ChatService, ROLE_ASSISTANT, ROLE_USER
from app.services.chat import service as chat_module

OWNER = 1
OTHER = 2
BAD_PROJECT = 9999  # StubKnowledge 认不出这个项目 id

wait = asyncio.run
_SEQ = count()


async def drain(agen) -> list:
    return [item async for item in agen]


# ---- 打桩：知识库 ------------------------------------------------------------


class StubKnowledge:
    """顶替 ``KnowledgeService``：检索回内存命中，索引校验只记次数。

    真的 ``KnowledgeService`` 要 Qdrant 和 embedding 服务商，这两样都不该在
    这条链路的验收点里。范围参数（project_id / type_ids）必须由闭包钉死、
    不暴露给模型，所以这里把每次检索收到的参数原样记下来。
    """

    def __init__(self, db, owner_id, env: dict) -> None:
        self.db = db
        self.owner_id = owner_id
        self.env = env

    def require_embedding_config(self):
        self.env["config_checks"] += 1
        if self.env["config_error"] is not None:
            raise self.env["config_error"]
        return SimpleNamespace(id=77, model_name="stub-embed")

    def assert_index_usable(self, config):
        self.env["index_checks"] += 1
        if self.env["index_error"] is not None:
            raise self.env["index_error"]
        return SimpleNamespace(chunk_count=3)

    def get_project(self, project_id: int):
        if project_id == BAD_PROJECT:
            raise LookupError("项目不存在")
        return SimpleNamespace(id=project_id)

    async def search(self, query, *, top_k, project_id, type_ids):
        self.env["searches"].append(
            {
                "query": query,
                "top_k": top_k,
                "project_id": project_id,
                "type_ids": type_ids,
            }
        )
        if self.env["search_error"] is not None:
            raise self.env["search_error"]
        return list(self.env["hits"])


@pytest.fixture()
def kb(monkeypatch) -> dict:
    env: dict = {
        "searches": [],
        "hits": [],
        "config_checks": 0,
        "index_checks": 0,
        "config_error": None,
        "index_error": None,
        "search_error": None,
    }

    def make(db_, owner_id):
        return StubKnowledge(db_, owner_id, env)

    monkeypatch.setattr(chat_module, "KnowledgeService", make)
    return env


# ---- 打桩：agent 图与模型 -----------------------------------------------------


def hit(chunk_id: int, *, filename: str = "runbook.md", content: str = "", score: float = 0.9) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        doc_id=100 + chunk_id,
        filename=filename,
        seq=chunk_id,
        score=score,
        content=content or f"第 {chunk_id} 段资料",
    )


class ScriptedStream:
    """按脚本吐事件的假流，顺带记下 ``aclose`` 有没有被调用。

    刻意不做成 async generator：被测代码在 finally 里调 ``stream.aclose()``
    是为了释放发往服务商的那条 HTTP 请求，这里要能直接看见它发生了。

    脚本里的 ``("search", query)`` 不是事件，而是「真的去调那次检索」：引用编号
    是工具写进 registry 的，光吐一个 on_tool_end 帧测不到 citations 的累积。
    """

    def __init__(
        self,
        events: List[object],
        error: Optional[BaseException],
        log: list,
        invoke: Callable[[str], Awaitable[str]],
    ) -> None:
        self._events = list(events)
        self._error = error
        self._log = log
        self._invoke = invoke
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index < len(self._events):
            step = self._events[self._index]
            self._index += 1
            if isinstance(step, tuple) and step[0] == "search":
                await self._invoke(step[1])
                return tool_start("search_knowledge_base", {"query": step[1]})
            return step
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        raise StopAsyncIteration

    async def aclose(self):
        self._log.append("stream closed")


def token(text: str) -> dict:
    return {
        "event": "on_chat_model_stream",
        "data": {"chunk": SimpleNamespace(content=text)},
    }


def tool_start(name: str, payload: dict) -> dict:
    return {"event": "on_tool_start", "name": name, "data": {"input": payload}}


TOOL_END = {"event": "on_tool_end", "name": "search_knowledge_base", "data": {"output": "x"}}


class FakeAgent:
    def __init__(self, env: dict, tools: list) -> None:
        self._env = env
        self._tools = {getattr(t, "name", ""): t for t in tools}

    async def _invoke(self, query: str) -> str:
        tool = self._tools["search_knowledge_base"]
        return await tool.ainvoke({"query": query})

    def astream_events(self, state, *, config=None, version=None):
        self._env["captured"]["recursion_limit"] = (config or {}).get("recursion_limit")
        self._env["captured"]["messages"] = state["messages"]
        self._env["captured"]["version"] = version
        return ScriptedStream(
            self._env["events"], self._env["error"], self._env["closes"], self._invoke
        )


@pytest.fixture()
def agent_env(monkeypatch, kb):
    """把 create_agent / build_chat_model / ChatOpsToolbox / 后台任务全部换成替身。

    返回的 dict 里 ``events`` 是这一轮假流要吐的事件、``error`` 是它中途抛的异常，
    测试可以直接改；``captured`` 记下真正传给图的 system prompt、工具名、消息
    列表与步数上限。
    """
    env: dict = {
        "events": [],
        "error": None,
        "closes": [],
        "captured": {},
        "model_kwargs": [],
        "boxes": [],
        "forgotten": [],
        "after": [],
    }

    def create_agent(client, tools, system_prompt=None):
        env["captured"]["system"] = system_prompt or ""
        env["captured"]["tools"] = [getattr(t, "name", str(t)) for t in tools]
        env["captured"]["client"] = client
        return FakeAgent(env, list(tools))

    def build_model(config, **kwargs):
        env["model_kwargs"].append(kwargs)
        return SimpleNamespace(name="fake-model", config_id=config.id)

    class StubToolbox:
        def __init__(self, db, owner_id, *, target=None, conversation_id=None):
            self.target = target
            self.conversation_id = conversation_id
            self.pending_proposals: List[OpsPendingAction] = []
            self.readonly_execs: List[dict] = []
            self.closed = False
            env["boxes"].append(self)

        def tools(self):
            return []

        def prompt_suffix(self):
            return ""

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("langchain.agents.create_agent", create_agent)
    monkeypatch.setattr(chat_module, "build_chat_model", build_model)
    monkeypatch.setattr(chat_module, "ChatOpsToolbox", StubToolbox)
    # 收尾任务由第九节单独验收，这里只记它被排上了队。
    monkeypatch.setattr(chat_module, "fire_and_forget", lambda task: env["forgotten"].append(task))
    monkeypatch.setattr(
        chat_module,
        "after_answer_task",
        lambda owner_id, conversation_id, history_turns: env["after"].append(
            (owner_id, conversation_id, history_turns)
        )
        or SimpleNamespace(),
    )
    return env


# ---- 造数据 -----------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'chat.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", maker)
    # ``service.py`` 是 ``from app.database import SessionLocal`` 拿的引用，模块全局
    # 各一份：``after_answer_task`` 认的是 ``chat_module`` 手里那把。只改上面那一处，
    # 收尾任务就会去连 .env 里的开发库。
    monkeypatch.setattr(chat_module, "SessionLocal", maker)
    session = maker()

    seed_builtin_agents(session)
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


def svc(db, owner_id: int = OWNER) -> ChatService:
    return ChatService(db, owner_id)


def chat_config(db, *, owner_id: int = OWNER) -> ModelConfig:
    return db.scalar(
        select(ModelConfig).where(
            ModelConfig.owner_id == owner_id,
            ModelConfig.purpose == ModelPurpose.CHAT.value,
        )
    )


def add_config(
    db,
    *,
    owner_id: int = OWNER,
    purpose: str = ModelPurpose.CHAT.value,
    title: str = "备用对话模型",
    is_default: bool = False,
    last_test_ok: bool = True,
) -> ModelConfig:
    row = ModelConfig(
        owner_id=owner_id,
        purpose=purpose,
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


def make_conversation(
    db,
    *,
    owner_id: int = OWNER,
    title: str = "对话用例会话",
    agent_id: Optional[int] = None,
    project_id: Optional[int] = None,
    type_ids: Optional[list] = None,
    model_config_id: Optional[int] = None,
    ops_target_type: Optional[str] = None,
    ops_target_id: Optional[int] = None,
    updated_at: Optional[datetime] = None,
) -> ChatConversation:
    row = ChatConversation(
        owner_id=owner_id,
        title=title,
        agent_id=agent_id,
        project_id=project_id,
        type_ids=type_ids,
        model_config_id=model_config_id,
        ops_target_type=ops_target_type,
        ops_target_id=ops_target_id,
    )
    if updated_at is not None:
        row.updated_at = updated_at
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_message(
    db,
    conversation_id: int,
    role: str,
    content: str,
    *,
    owner_id: int = OWNER,
    citations: Optional[list] = None,
) -> ChatMessage:
    row = ChatMessage(
        owner_id=owner_id,
        conversation_id=conversation_id,
        role=role,
        content=content,
        citations=citations,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_agent(
    db,
    *,
    owner_id: int = OWNER,
    name: str = "自定义人设",
    system_prompt: str = "你是一个助手。",
    use_knowledge: bool = False,
    use_ops: bool = False,
    use_memory: bool = False,
    chat_visible: bool = True,
    enabled: bool = True,
    temperature: int = 30,
    sort_order: int = 0,
) -> Agent:
    row = Agent(
        owner_id=owner_id,
        slug=f"chat-svc-{next(_SEQ)}",
        name=name,
        system_prompt=system_prompt,
        use_knowledge=use_knowledge,
        use_ops=use_ops,
        use_memory=use_memory,
        chat_visible=chat_visible,
        enabled=enabled,
        temperature=temperature,
        sort_order=sort_order,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def messages(db, conversation_id: int, *, owner_id: int = OWNER) -> List[ChatMessage]:
    return list(
        db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.owner_id == owner_id,
                ChatMessage.conversation_id == conversation_id,
            )
            .order_by(ChatMessage.id)
        ).all()
    )


# ---- 一、纯函数与引用编号 ------------------------------------------------------


def test_chunk_text_flattens_plain_blocked_and_missing_content():
    """流式 chunk 的 content 有三种形态：字符串、内容块列表、压根没这个属性。"""
    from types import SimpleNamespace as NS

    assert chat_module._chunk_text(NS(content="前半")) == "前半"
    assert chat_module._chunk_text(NS(content=[{"text": "甲"}, {"text": "乙"}])) == "甲乙"
    assert chat_module._chunk_text(NS(content=[{"text": "甲"}, {"index": 2}])) == "甲"
    assert chat_module._chunk_text(NS(content=None)) == ""
    assert chat_module._chunk_text(NS()) == ""
    assert chat_module._chunk_text("不是消息对象") == ""


def test_format_hits_numbers_with_filename_and_body():
    """编号是给模型看的引用锚点，格式一换，[1] 和来源列表就对不上了。"""
    numbered = chat_module._CitationRegistry().register([hit(1, filename="a.md"), hit(2, filename="b.sql")])
    assert chat_module._format_hits(numbered) == (
        "[1] 来源：a.md\n第 1 段资料\n\n[2] 来源：b.sql\n第 2 段资料"
    )


def test_registry_gives_each_chunk_one_number_and_reuses_it():
    registry = chat_module._CitationRegistry()
    first = registry.register([hit(1), hit(2)])
    assert [index for index, _ in first] == [1, 2]

    # 第二轮检索里 chunk 2 又被命中：编号沿用第一次的，新 chunk 从 3 起。
    second = registry.register([hit(2), hit(3)])
    assert [index for index, _ in second] == [2, 3]

    all_citations = registry.all()
    assert [c.index for c in all_citations] == [1, 2, 3]
    assert [c.chunk_id for c in all_citations] == [1, 2, 3]


def test_registry_numbers_from_one_and_carry_only_the_fields_the_ui_needs():
    registry = chat_module._CitationRegistry()
    content = "很长的资料正文" + "尾" * 300
    numbered = registry.register([hit(9, score=0.42, content=content)])
    assert [index for index, _ in numbered] == [1]
    # 登记下来的是落库用的 Citation（编号、出处、节选），hit 只是原样带回给模型渲染。
    assert registry.all()[0] == Citation(
        index=1,
        doc_id=109,
        chunk_id=9,
        filename="runbook.md",
        score=0.42,
        excerpt=content[:200],
    )
    assert len(registry.all()[0].excerpt) == 200
    # register 交回的是 (编号, 原始 hit)：hit 拿去给模型渲染全文，Citation 才是落库的那份。
    assert numbered[0][1].content == content


def test_registry_is_empty_until_a_search_registers_something():
    """模型一次都不检索时，citations 帧就是空列表——前端据此不渲染来源区。"""
    assert chat_module._CitationRegistry().all() == []


@pytest.mark.parametrize(
    "question,expected",
    [
        ("nginx 为什么 502", "nginx 为什么 502"),
        ("  多行\n\t提问  拆开  ", "多行 提问 拆开"),
        ("字" * 40, "字" * 40),  # 40 字整：不加省略号
        ("字" * 41, "字" * 40 + "…"),  # 超长：截到 40 再补省略号
    ],
)
def test_derive_title_collapses_whitespace_and_caps_at_40(question, expected):
    assert chat_module.derive_title(question) == expected


# ---- 二、用哪份对话模型配置 -----------------------------------------------------


def test_require_chat_config_uses_the_default_when_none_is_given(db):
    assert svc(db).require_chat_config().id == chat_config(db).id


def test_require_chat_config_default_accepts_the_owner_only(db):
    """别人的默认对话模型不能顶上来：查询按 owner 限定。"""
    add_config(db, owner_id=OTHER, title="别人的默认", is_default=True)
    assert svc(db).require_chat_config().title == "测试对话模型"


def test_require_chat_config_rejects_missing_or_untested_default(db):
    with pytest.raises(BusinessError) as exc:
        svc(db, OTHER).require_chat_config()
    assert exc.value.code == CODE_CHAT_NOT_READY
    assert "设置" in exc.value.msg

    row = chat_config(db)
    row.last_test_ok = False
    db.commit()
    with pytest.raises(BusinessError):
        svc(db).require_chat_config()


def test_require_chat_config_validates_an_explicit_choice(db):
    extra = add_config(db, title="第二份对话模型")
    assert svc(db).require_chat_config(extra.id).title == "第二份对话模型"

    # 别人的配置：404 而不是「没权限」——确认它存在本身就是泄露。
    theirs = add_config(db, owner_id=OTHER, title="别人的")
    with pytest.raises(LookupError):
        svc(db).require_chat_config(theirs.id)

    with pytest.raises(LookupError):
        svc(db).require_chat_config(4242)


@pytest.mark.parametrize(
    "kind,message",
    [
        ("purpose", "不是对话模型"),
        ("untested", "连通性测试"),
    ],
)
def test_require_chat_config_refuses_a_choice_that_cannot_answer(db, kind, message):
    if kind == "purpose":
        row = add_config(db, title="向量模型", purpose=ModelPurpose.EMBEDDING.value)
    else:
        row = add_config(db, title="没测过的", last_test_ok=False)
    with pytest.raises(BusinessError) as exc:
        svc(db).require_chat_config(row.id)
    assert exc.value.code == CODE_CHAT_NOT_READY
    assert message in exc.value.msg


# ---- 三、会话的读写边界 --------------------------------------------------------


def test_create_conversation_stores_the_selection_and_defaults(db, kb):
    agent = make_agent(db)
    config = add_config(db, title="会话指定的模型")
    row = svc(db).create_conversation(
        title="排查 502",
        agent_id=agent.id,
        project_id=5,
        type_ids=[1, 2],
        model_config_id=config.id,
    )
    assert (row.title, row.agent_id, row.project_id, row.type_ids, row.model_config_id) == (
        "排查 502",
        agent.id,
        5,
        [1, 2],
        config.id,
    )
    assert svc(db).get_conversation(row.id).id == row.id


def test_create_conversation_falls_back_on_an_empty_title_and_truncates_a_long_one(db):
    """兜底只看「空」：空白串归 API 层的 min_length 管，服务层不替它收边。"""
    assert svc(db).create_conversation(title="").title == "新会话"
    assert svc(db).create_conversation().title == "新会话"
    assert svc(db).create_conversation(title="标题" * 200).title == ("标题" * 200)[:255]
    assert len(svc(db).create_conversation(title="标" * 300).title) == 255


def test_create_conversation_treats_an_empty_type_selection_as_no_selection(db):
    """空列表和「没选」在存储上要同一个形态：JSON 里的 [] 会挡住后续读取分支。"""
    assert svc(db).create_conversation(type_ids=[]).type_ids is None
    assert svc(db).create_conversation(type_ids=[3]).type_ids == [3]


def test_create_conversation_validates_every_selection_before_writing(db):
    agent = make_agent(db, owner_id=OTHER)
    config = add_config(db, title="向量模型", purpose=ModelPurpose.EMBEDDING.value)
    before = len(db.scalars(select(ChatConversation)).all())

    with pytest.raises(LookupError):
        svc(db).create_conversation(agent_id=agent.id)
    with pytest.raises(LookupError):
        svc(db).create_conversation(project_id=BAD_PROJECT)
    with pytest.raises(BusinessError):
        svc(db).create_conversation(model_config_id=config.id)

    assert len(db.scalars(select(ChatConversation)).all()) == before


def test_list_conversations_hides_ops_sessions_unless_asked_for(db):
    plain = make_conversation(db, title="普通会话")
    ops = make_conversation(db, title="生产机问答", ops_target_type="server", ops_target_id=7)

    assert [row.id for row in svc(db).list_conversations()] == [plain.id]
    assert {row.id for row in svc(db).list_conversations(include_ops=True)} == {
        plain.id,
        ops.id,
    }


def test_list_conversations_counts_only_this_owners_messages(db):
    conv = make_conversation(db)
    svc(db).add_message(conv.id, ROLE_USER, "我的提问")
    svc(db).add_message(conv.id, ROLE_ASSISTANT, "我的回答")
    make_message(db, conv.id, ROLE_USER, "别人挂在同一个会话上的提问", owner_id=OTHER)

    item = svc(db).list_conversations()[0]
    assert item.message_count == 2
    assert (item.id, item.title) == (conv.id, "对话用例会话")
    assert item.agent_id is None and item.project_id is None


def test_list_conversations_orders_by_recent_activity_then_id(db):
    base = datetime(2026, 10, 12, 10, 0, 0)
    old = make_conversation(db, title="最早", updated_at=base)
    recent = make_conversation(db, title="最近", updated_at=base + timedelta(days=2))
    make_conversation(db, owner_id=OTHER, title="别人的", updated_at=base + timedelta(days=9))

    assert [row.title for row in svc(db).list_conversations()] == ["最近", "最早"]
    assert recent.id > old.id


def test_get_conversation_is_a_404_for_anyone_elses(db):
    theirs = make_conversation(db, owner_id=OTHER)
    with pytest.raises(LookupError):
        svc(db).get_conversation(theirs.id)
    with pytest.raises(LookupError):
        svc(db).get_conversation(4242)


def test_update_conversation_only_touches_the_fields_it_was_given(db):
    agent = make_agent(db)
    row = make_conversation(db, title="旧标题", project_id=5, type_ids=[1, 2])
    service = svc(db)

    updated = service.update_conversation(row.id, title="  新标题  ")
    assert (updated.title, updated.agent_id, updated.project_id, updated.type_ids) == (
        "  新标题  ",
        None,
        5,
        [1, 2],
    )

    updated = service.update_conversation(row.id, agent_id=agent.id, model_config_id=chat_config(db).id)
    assert updated.agent_id == agent.id and updated.model_config_id == chat_config(db).id


def test_update_conversation_clears_stale_types_when_the_project_changes(db, kb):
    """切项目必须把旧项目的类型选择一起清掉，否则整个会话被限到一个空范围里。"""
    row = make_conversation(db, project_id=5, type_ids=[1, 2])
    updated = svc(db).update_conversation(row.id, project_id=6)
    assert (updated.project_id, updated.type_ids) == (6, None)

    # 同项目下显式给出新类型时按新的走。
    row2 = make_conversation(db, project_id=5, type_ids=[1, 2])
    assert svc(db).update_conversation(row2.id, project_id=5, type_ids=[3]).type_ids == [3]


def test_update_conversation_type_selection_can_be_reset_to_all(db):
    row = make_conversation(db, project_id=5, type_ids=[1, 2])
    assert svc(db).update_conversation(row.id, type_ids=[]).type_ids is None


@pytest.mark.parametrize("kind", ["agent", "project", "model"])
def test_update_conversation_refuses_selections_it_should_not(db, kind):
    row = make_conversation(db)
    service = svc(db)
    with pytest.raises((LookupError, BusinessError)):
        if kind == "agent":
            service.update_conversation(row.id, agent_id=make_agent(db, owner_id=OTHER).id)
        elif kind == "project":
            service.update_conversation(row.id, project_id=BAD_PROJECT)
        else:
            service.update_conversation(
                row.id,
                model_config_id=add_config(
                    db, title="向量模型", purpose=ModelPurpose.EMBEDDING.value
                ).id,
            )
    db.rollback()
    assert svc(db).get_conversation(row.id).project_id is None


def test_update_conversation_on_an_unowned_row_is_a_404(db):
    theirs = make_conversation(db, owner_id=OTHER)
    with pytest.raises(LookupError):
        svc(db).update_conversation(theirs.id, title="改别人的")


def test_delete_conversation_takes_messages_actions_and_summary_with_it(db):
    """会话是这三样的边界：删会话就是把它们一起删掉。"""
    conv = make_conversation(db)
    other_conv = make_conversation(db, title="另一个会话")
    svc(db).add_message(conv.id, ROLE_USER, "提问")
    make_message(db, other_conv.id, ROLE_USER, "别的会话的提问")
    db.add(
        OpsPendingAction(
            owner_id=OWNER,
            conversation_id=conv.id,
            target_type="server",
            target_id=7,
            command="systemctl restart nginx",
            reason="重启卡住的 nginx",
            status="pending",
            created_at=datetime.now(),
        )
    )
    db.add(ChatSummary(conversation_id=conv.id, owner_id=OWNER, summary="这个会话的摘要"))
    db.add(ChatSummary(conversation_id=other_conv.id, owner_id=OTHER, summary="别人的摘要"))
    memory = UserMemory(owner_id=OWNER, content="用户是后端工程师")
    db.add(memory)
    db.commit()

    svc(db).delete_conversation(conv.id)

    assert db.get(ChatConversation, conv.id) is None
    assert messages(db, conv.id) == []
    assert db.scalar(select(OpsPendingAction)) is None
    assert db.get(ChatSummary, conv.id) is None
    assert svc(db).list_messages(other_conv.id)[0].content == "别的会话的提问"
    assert db.get(UserMemory, memory.id).content == "用户是后端工程师"  # 记忆属于用户
    assert db.get(ChatSummary, other_conv.id).summary == "别人的摘要"


def test_delete_conversation_is_owner_scoped(db):
    theirs = make_conversation(db, owner_id=OTHER)
    with pytest.raises(LookupError):
        svc(db).delete_conversation(theirs.id)
    assert svc(db, OTHER).get_conversation(theirs.id).id == theirs.id


# ---- 四、消息 -----------------------------------------------------------------


def test_add_message_serializes_citations_and_leaves_the_column_null_without_them(db):
    conv = make_conversation(db)
    service = svc(db)
    row = service.add_message(
        conv.id,
        ROLE_ASSISTANT,
        "答案 [1]",
        citations=[
            Citation(index=1, doc_id=9, chunk_id=9, filename="a.md", score=0.5, excerpt="节选")
        ],
    )
    assert row.citations == [
        {
            "index": 1,
            "doc_id": 9,
            "chunk_id": 9,
            "filename": "a.md",
            "score": 0.5,
            "excerpt": "节选",
        }
    ]
    assert service.add_message(conv.id, ROLE_USER, "没有引用").citations is None


def test_add_message_touches_the_conversation_so_the_list_reorders(db):
    base = datetime.now() - timedelta(days=11)
    stale = make_conversation(db, title="很久没聊", updated_at=base)
    make_conversation(db, title="刚聊过", updated_at=base + timedelta(days=7))
    assert [row.title for row in svc(db).list_conversations()] == ["刚聊过", "很久没聊"]

    svc(db).add_message(stale.id, ROLE_USER, "又问了一句")
    assert [row.title for row in svc(db).list_conversations()] == ["很久没聊", "刚聊过"]


def test_add_message_does_not_need_an_existing_conversation_row(db):
    """路由先落提问再校验会话，历史消息也可能挂在已删会话上——这里不拦。"""
    row = svc(db).add_message(4242, ROLE_USER, "没有这条会话")
    assert row.conversation_id == 4242


def test_list_messages_replays_in_order_and_rebuilds_citations(db):
    conv = make_conversation(db)
    make_message(db, conv.id, ROLE_USER, "提问")
    make_message(
        db,
        conv.id,
        ROLE_ASSISTANT,
        "回答 [1]",
        citations=[
            {
                "index": 1,
                "doc_id": 9,
                "chunk_id": 9,
                "filename": "a.md",
                "score": 0.5,
                "excerpt": "节选",
            }
        ],
    )
    make_message(db, conv.id, ROLE_USER, "别人的消息不出现", owner_id=OTHER)

    items = svc(db).list_messages(conv.id)
    assert [item.role for item in items] == [ROLE_USER, ROLE_ASSISTANT]
    assert items[1].citations[0].filename == "a.md"
    assert isinstance(items[1].citations[0], Citation)
    assert items[0].citations == []


def test_list_messages_needs_a_conversation_you_own(db):
    theirs = make_conversation(db, owner_id=OTHER)
    with pytest.raises(LookupError):
        svc(db).list_messages(theirs.id)


def test_update_message_content_only_allows_fixing_the_assistant_row(db):
    conv = make_conversation(db)
    answer = make_message(db, conv.id, ROLE_ASSISTANT, "画错的图")
    question = make_message(db, conv.id, ROLE_USER, "原始提问")

    assert svc(db).update_message_content(answer.id, "改过的答案").content == "改过的答案"
    # 改了提问，下面的回答就成了答非所问。
    with pytest.raises(BusinessError) as exc:
        svc(db).update_message_content(question.id, "改写提问")
    assert exc.value.code == 409


def test_update_message_content_is_a_404_for_anyone_elses_row(db):
    conv = make_conversation(db, owner_id=OTHER)
    theirs = make_message(db, conv.id, ROLE_ASSISTANT, "别人的回答", owner_id=OTHER)
    with pytest.raises(LookupError):
        svc(db).update_message_content(theirs.id, "改写别人的回答")
    with pytest.raises(LookupError):
        svc(db).update_message_content(4242, "改一条不存在的")


@pytest.mark.parametrize("tail", ["assistant", "user", "empty"])
def test_pop_dangling_user_message_only_pops_a_real_leftover(db, tail):
    """重试的前提是「末尾那条提问还没得到回答」，形态不对就明确拒绝。"""
    conv = make_conversation(db)
    service = svc(db)
    service.add_message(conv.id, ROLE_USER, "第一次提问")
    if tail == "assistant":
        service.add_message(conv.id, ROLE_ASSISTANT, "已经回答过了")
    elif tail == "empty":
        for row in messages(db, conv.id):
            db.delete(row)
        db.commit()

    if tail == "user":
        assert service.pop_dangling_user_message(conv.id) == "第一次提问"
        assert messages(db, conv.id) == []
    else:
        with pytest.raises(BusinessError) as exc:
            service.pop_dangling_user_message(conv.id)
        assert exc.value.code == 409
        assert "没有可重试的提问" in exc.value.msg


def test_pop_dangling_user_message_ignores_other_owners_rows(db):
    conv = make_conversation(db)
    svc(db).add_message(conv.id, ROLE_USER, "我的提问")
    make_message(db, conv.id, ROLE_USER, "别人挂在同一个会话上的提问", owner_id=OTHER)
    assert svc(db).pop_dangling_user_message(conv.id) == "我的提问"
    # 别人的那行还挂着同一个 conversation_id，既没被弹掉也没被删掉。
    assert [row.content for row in messages(db, conv.id, owner_id=OTHER)] == [
        "别人挂在同一个会话上的提问"
    ]
    assert messages(db, conv.id) == []


# ---- 五、喂给模型的历史与初始消息 ----------------------------------------------


def fill_history(db, conv, rows: int) -> None:
    for index in range(rows):
        make_message(
            db,
            conv.id,
            ROLE_USER if index % 2 == 0 else ROLE_ASSISTANT,
            f"第 {index} 条",
        )


def test_history_keeps_the_newest_window_oldest_first(db):
    conv = make_conversation(db)
    window = chat_module.HISTORY_TURNS * 2
    fill_history(db, conv, window + 4)

    history = svc(db)._history(conv.id)
    assert len(history) == window
    assert [row.content for row in history] == [f"第 {index} 条" for index in range(4, window + 4)]
    assert history[-1].role == ROLE_ASSISTANT


def test_history_is_scoped_to_the_owner(db):
    conv = make_conversation(db)
    fill_history(db, conv, 2)
    make_message(db, conv.id, ROLE_USER, "别人的消息", owner_id=OTHER)
    assert [row.content for row in svc(db)._history(conv.id)] == ["第 0 条", "第 1 条"]


def test_replay_maps_roles_and_appends_the_opening_question(db):
    from langchain_core.messages import HumanMessage

    conv = make_conversation(db)
    make_message(db, conv.id, ROLE_USER, "提问一")
    make_message(db, conv.id, ROLE_ASSISTANT, "回答一")
    make_message(db, conv.id, "system", "不该进上下文的角色")
    make_message(db, conv.id, ROLE_USER, "提问二")  # 路由已经落库的那条

    replayed = svc(db)._replay(conv.id, "换个问法")
    assert [type(m).__name__ for m in replayed] == ["HumanMessage", "AIMessage", "HumanMessage"]
    assert isinstance(replayed[-1], HumanMessage) and replayed[-1].content == "换个问法"
    # 末尾那条已落库的提问被丢掉，否则同一个问题在上下文里出现两遍。
    assert [m.content for m in replayed[:2]] == ["提问一", "回答一"]
    assert all("不该进上下文" not in m.content for m in replayed)


def test_replay_keeps_the_tail_when_the_question_was_not_persisted(db):
    """confirm 续答走 False：合成的上下文不该顶掉历史里那条真实的提问。"""
    conv = make_conversation(db)
    make_message(db, conv.id, ROLE_USER, "提问一")
    make_message(db, conv.id, ROLE_ASSISTANT, "回答一")
    make_message(db, conv.id, ROLE_USER, "还没被回答的提问")

    replayed = svc(db)._replay(conv.id, "[系统] 执行结果如下", question_persisted=False)
    assert [m.content for m in replayed] == [
        "提问一",
        "回答一",
        "还没被回答的提问",
        "[系统] 执行结果如下",
    ]


def test_replay_on_an_empty_conversation_is_just_the_question(db):
    conv = make_conversation(db)
    replayed = svc(db)._replay(conv.id, "第一句")
    assert len(replayed) == 1 and replayed[0].content == "第一句"


# ---- 六、这一轮用哪个智能体 -----------------------------------------------------


def test_resolve_agent_with_an_explicit_id(db):
    mine = make_agent(db, name="我自己的")
    assert svc(db).resolve_agent(mine.id).id == mine.id

    builtin = AgentService(db, OWNER).require_by_slug("general-chat")
    assert svc(db).resolve_agent(builtin.id).id == builtin.id

    theirs = make_agent(db, owner_id=OTHER)
    with pytest.raises(LookupError):
        svc(db).resolve_agent(theirs.id)


def test_resolve_agent_default_picks_the_first_open_and_enabled(db):
    """默认人选只从「对对话页开放」的智能体里挑：后台人设不能顶上来答对话。"""
    for row in db.scalars(select(Agent).where(Agent.owner_id != OWNER)).all():
        row.enabled = False  # 内置的全关掉，剩下的就是这两条自己造的
    db.commit()

    hidden = make_agent(db, name="运维人设", chat_visible=False)
    visible = make_agent(db, name="前台人设")
    assert svc(db).resolve_agent(None).id == visible.id

    visible.enabled = False
    db.commit()
    # 前台的都关了，剩下的只有 chat_visible=False 的后台人设——不算可用。
    assert hidden.chat_visible is False
    with pytest.raises(BusinessError) as exc:
        svc(db).resolve_agent(None)
    assert exc.value.code == CODE_CHAT_NOT_READY
    assert "没有可用的智能体" in exc.value.msg


# ---- 七、模型手里的两个工具 ------------------------------------------------------


def test_search_tool_pins_the_scope_in_the_closure(db, kb):
    """project_id / type_ids 是闭包捕获的：模型只能决定查什么，不能决定查哪儿。"""
    kb["hits"] = [hit(1, filename="a.md"), hit(2, filename="b.sql")]
    registry = chat_module._CitationRegistry()
    tool = svc(db)._search_tool(registry, project_id=5, type_ids=[7, 8])

    assert set(tool.args) == {"query"}
    text = wait(tool.ainvoke({"query": "502 是什么原因"}))
    assert kb["searches"] == [
        {
            "query": "502 是什么原因",
            "top_k": chat_module.RETRIEVAL_TOP_K,
            "project_id": 5,
            "type_ids": [7, 8],
        }
    ]
    assert text == "[1] 来源：a.md\n第 1 段资料\n\n[2] 来源：b.sql\n第 2 段资料"
    assert [c.index for c in registry.all()] == [1, 2]


def test_search_tool_treats_an_empty_type_selection_as_the_whole_project(db, kb):
    registry = chat_module._CitationRegistry()
    tool = svc(db)._search_tool(registry, project_id=5, type_ids=[])
    wait(tool.ainvoke({"query": "查全部"}))
    assert kb["searches"][0]["type_ids"] is None and kb["searches"][0]["project_id"] == 5


def test_search_tool_tells_the_model_it_found_nothing(db, kb):
    """空命中回一句人话而不是空串：空串会让模型以为工具坏了去编答案。"""
    registry = chat_module._CitationRegistry()
    tool = svc(db)._search_tool(registry, project_id=5, type_ids=None)
    assert wait(tool.ainvoke({"query": "没有的资料"})) == "没有检索到相关资料。"
    assert registry.all() == []


def test_search_tool_numbers_stay_put_across_two_searches(db, kb):
    registry = chat_module._CitationRegistry()
    tool = svc(db)._search_tool(registry, project_id=5, type_ids=None)
    kb["hits"] = [hit(1), hit(2)]
    first = wait(tool.ainvoke({"query": "第一轮"}))
    kb["hits"] = [hit(2), hit(3)]
    second = wait(tool.ainvoke({"query": "第二轮"}))

    assert first.startswith("[1] 来源：runbook.md") and "[2]" in first
    # chunk 2 第二次被命中还是 [2]，新的 chunk 3 顶到 [3]。
    assert second.startswith("[2] 来源：runbook.md") and "[3]" in second
    assert [c.chunk_id for c in registry.all()] == [1, 2, 3]


def test_search_tool_reraises_after_logging(db, kb, caplog):
    """异常要交给 ToolNode 包成 ToolMessage 让模型换个查法，但不能只留在那儿。"""
    kb["search_error"] = RuntimeError("qdrant 连不上")
    tool = svc(db)._search_tool(chat_module._CitationRegistry(), project_id=5, type_ids=None)
    with caplog.at_level("WARNING", logger="yangvis.chat"):
        with pytest.raises(RuntimeError):
            wait(tool.ainvoke({"query": "查一下"}))
    assert "knowledge search tool failed" in caplog.text
    assert len(kb["searches"]) == 1


def test_datetime_tool_answers_in_beijing_time(db):
    tool = svc(db)._datetime_tool()
    text = tool.invoke({})
    # 「今天」按北京时间换算：格式是 YYYY-MM-DD HH:MM:SS 星期X。
    assert len(text) == 23 and text[10] == " " and text.startswith("20")
    weekday = text.split("星期")[1]
    assert weekday in chat_module._WEEKDAYS
    stamp = datetime.strptime(text.split(" 星期")[0], "%Y-%m-%d %H:%M:%S")
    now_local = datetime.now(chat_module._LOCAL_TZ).replace(tzinfo=None)
    assert abs((now_local - stamp).total_seconds()) < 120


# ---- 八、stream_answer：进循环之前的校验与循环之后的落库 ------------------------


def run_answer(
    db,
    conv,
    agent,
    env,
    events,
    *,
    question="502 是什么原因",
    config=None,
    **kwargs,
) -> list:
    """跑一轮 stream_answer，把 ``(event, payload)`` 收成列表。

    提问按路由的顺序先落库，好让「历史里有一条 user 消息」这个前提成立。
    """
    env["events"] = list(events)
    service = svc(db)
    service.add_message(conv.id, ROLE_USER, question)
    return wait(
        drain(
            service.stream_answer(
                conversation_id=conv.id,
                agent=agent,
                question=question,
                config=config or chat_config(db),
                **kwargs,
            )
        )
    )


def test_answer_refuses_to_start_without_a_project(db, agent_env):
    """检索范围是用户的操作前提，不该等模型去发现——所以拦在循环外面。"""
    conv = make_conversation(db)
    agent = make_agent(db, use_knowledge=True)

    with pytest.raises(BusinessError) as exc:
        run_answer(db, conv, agent, agent_env, [token("不该走到这儿")])

    assert exc.value.code == CODE_CHAT_NOT_READY
    assert "请先选择项目" in exc.value.msg
    assert agent_env["captured"] == {} and agent_env["model_kwargs"] == []
    assert messages(db, conv.id)[0].role == ROLE_USER  # 只留下提问，没有伪造的回答


def test_answer_checks_the_vector_index_before_the_loop(db, agent_env, kb):
    conv = make_conversation(db)
    agent = make_agent(db, use_knowledge=True)
    run_answer(db, conv, agent, agent_env, [token("好了")], project_id=5)
    assert kb["config_checks"] == 1 and kb["index_checks"] == 1


def test_answer_lets_an_unusable_index_stop_the_answer(db, agent_env, kb):
    """索引模型对不上时不能放进循环：ToolNode 会把异常喂回模型，用户只看到它拿报错当资料。"""
    conv = make_conversation(db)
    agent = make_agent(db, use_knowledge=True)
    kb["index_error"] = BusinessError(4002, "向量索引由另一个模型建出")

    with pytest.raises(BusinessError):
        run_answer(db, conv, agent, agent_env, [], project_id=5)
    assert agent_env["captured"] == {}


def test_answer_needs_a_conversation_you_own(db, agent_env):
    theirs = make_conversation(db, owner_id=OTHER)
    with pytest.raises(LookupError):
        run_answer(db, theirs, make_agent(db), agent_env, [])
    assert agent_env["model_kwargs"] == []


def test_answer_hands_the_model_only_the_datetime_tool_until_knowledge_is_on(db, agent_env):
    conv = make_conversation(db)
    run_answer(db, conv, make_agent(db), agent_env, [token("答")])
    assert agent_env["captured"]["tools"] == ["current_datetime"]

    agent_env["captured"].clear()
    run_answer(
        db, conv, make_agent(db, use_knowledge=True), agent_env, [token("答")], project_id=5
    )
    assert agent_env["captured"]["tools"] == ["current_datetime", "search_knowledge_base"]


def test_answer_streams_at_the_agents_temperature(db, agent_env):
    conv = make_conversation(db)
    run_answer(db, conv, make_agent(db, temperature=75), agent_env, [token("答")])
    assert agent_env["model_kwargs"] == [{"temperature": 0.75, "streaming": True}]


def test_prompt_layers_persona_memories_summary_then_rules(db, agent_env):
    """注入顺序：人设 → 记忆 → 会话摘要 → 硬约束。硬约束必须留在最末尾。"""
    conv = make_conversation(db)
    db.add(UserMemory(owner_id=OWNER, content="用户是后端工程师"))
    db.add(ChatSummary(conversation_id=conv.id, owner_id=OWNER, summary="更早的排查经过"))
    db.commit()
    agent = make_agent(db, name="排查助手", system_prompt="你是排查助手。", use_knowledge=True, use_memory=True)

    run_answer(db, conv, agent, agent_env, [token("答")], project_id=5)
    system = agent_env["captured"]["system"]

    assert system.startswith("你是排查助手。\n\n")
    assert chat_module.MEMORY_RULES in system and "- 用户是后端工程师" in system
    assert "以下是本次会话更早部分的摘要" in system and "更早的排查经过" in system
    assert system.endswith(chat_module._CITATION_RULES)
    marks = [
        system.index(chat_module.MEMORY_RULES),
        system.index("以下是本次会话更早部分的摘要"),
        system.index(chat_module._CITATION_RULES),
    ]
    assert marks == sorted(marks)


def test_prompt_stays_the_persona_plus_rules_with_nothing_to_inject(db, agent_env):
    conv = make_conversation(db)
    agent = make_agent(db, system_prompt="只有一句话的人设。", use_knowledge=True, use_memory=True)
    run_answer(db, conv, agent, agent_env, [token("答")], project_id=5)
    assert agent_env["captured"]["system"] == (
        "只有一句话的人设。\n\n" + chat_module._CITATION_RULES
    )


def test_answer_emits_step_then_citations_then_tokens_then_done(db, agent_env, kb):
    conv = make_conversation(db)
    kb["hits"] = [hit(1, filename="a.md", content="nginx 502 常见于上游超时"), hit(2, filename="b.md")]
    agent = make_agent(db, use_knowledge=True)

    events = run_answer(
        db,
        conv,
        agent,
        agent_env,
        [
            ("search", "nginx 502"),
            TOOL_END,
            token("先说一句「我查一下」。"),
            ("search", "上游超时"),
            TOOL_END,
            token("\n\n上游超时导致 [1]。"),
        ],
        project_id=5,
        type_ids=[3],
    )

    assert [name for name, _ in events] == [
        "step",
        "citations",
        "token",
        "step",
        "citations",
        "token",
        "done",
    ]
    assert events[0][1] == {
        "tool": "search_knowledge_base",
        "query": "nginx 502",
        "input": {"query": "nginx 502"},
    }
    # citations 帧是「累积到当前为止的完整列表」，客户端整体替换。
    assert [c["index"] for c in events[1][1]] == [1, 2]
    assert [c["index"] for c in events[4][1]] == [1, 2]
    assert kb["searches"][1] == {
        "query": "上游超时",
        "top_k": chat_module.RETRIEVAL_TOP_K,
        "project_id": 5,
        "type_ids": [3],
    }
    assert events[2][1] == "先说一句「我查一下」。"
    assert events[6][1] == {"message_id": messages(db, conv.id)[-1].id, "conversation_id": conv.id}


def test_answer_saves_what_the_user_saw_plus_the_sources(db, agent_env, kb):
    conv = make_conversation(db)
    kb["hits"] = [hit(7, filename="a.md", content="资料正文")]
    agent = make_agent(db, use_knowledge=True)

    run_answer(
        db,
        conv,
        agent,
        agent_env,
        [("search", "查一下"), TOOL_END, token("模型调用前的那句「我查一下」。"), token("结论 [1]")],
        project_id=5,
    )

    answer = messages(db, conv.id)[-1]
    assert answer.role == ROLE_ASSISTANT
    # 中间那句确实出现在了用户屏幕上，落库内容和屏幕上对不上才是更糟的事。
    assert answer.content == "模型调用前的那句「我查一下」。结论 [1]"
    assert [c["chunk_id"] for c in answer.citations] == [7]
    assert answer.citations[0]["index"] == 1

    item = svc(db).list_messages(conv.id)[-1]
    assert [c.filename for c in item.citations] == ["a.md"]


def test_answer_without_a_search_leaves_the_sources_column_null(db, agent_env):
    conv = make_conversation(db)
    events = run_answer(db, conv, make_agent(db), agent_env, [token("  凭常识的回答  ")])
    assert [name for name, _ in events] == ["token", "done"]
    answer = messages(db, conv.id)[-1]
    assert answer.content == "凭常识的回答"  # 落库前收掉首尾空白
    assert answer.citations is None


def test_answer_with_no_text_at_all_says_it_found_nothing(db, agent_env):
    """模型一个字都没吐：存一句实话，而不是空消息。"""
    conv = make_conversation(db)
    events = run_answer(db, conv, make_agent(db), agent_env, [])
    assert [name for name, _ in events] == ["done"]
    assert messages(db, conv.id)[-1].content == chat_module.NO_CONTEXT_REPLY


def test_answer_without_a_toolbox_stays_on_the_search_step_budget(db, agent_env):
    conv = make_conversation(db)
    run_answer(db, conv, make_agent(db), agent_env, [token("答")])
    assert agent_env["captured"]["recursion_limit"] == chat_module.MAX_REACT_STEPS
    assert agent_env["captured"]["version"] == "v2"


def test_abort_with_nothing_streamed_still_records_the_stop(db, agent_env):
    """一点正文都没吐就停止：存的必须是「已停止生成」，不能是空串或别人的话。"""
    conv = make_conversation(db)

    async def disconnected():
        return True

    events = run_answer(
        db, conv, make_agent(db), agent_env, [token("不该出现")], is_disconnected=disconnected
    )
    assert events == []
    assert messages(db, conv.id)[-1].content == chat_module.ABORTED_EMPTY
    assert agent_env["closes"] == ["stream closed"]


def test_answer_schedules_the_wrap_up_after_the_message_lands(db, agent_env):
    conv = make_conversation(db)
    run_answer(db, conv, make_agent(db), agent_env, [token("答")])
    assert agent_env["after"] == [(OWNER, conv.id, chat_module.HISTORY_TURNS)]
    assert len(agent_env["forgotten"]) == 1


def test_wrap_up_is_scheduled_even_for_an_aborted_answer(db, agent_env):
    """半截回答也是回答：摘要与记忆提取照样得跑，否则下一次重算少一段历史。"""
    conv = make_conversation(db)

    async def disconnected():
        return True

    run_answer(
        db, conv, make_agent(db), agent_env, [token("答")], is_disconnected=disconnected
    )
    assert agent_env["after"] == [(OWNER, conv.id, chat_module.HISTORY_TURNS)]


def test_step_limit_with_no_text_saves_the_hint_itself(db, agent_env):
    """撞上限却一个字没吐：补出去的那句提示就是这条回答的全部正文。"""
    from langgraph.errors import GraphRecursionError

    conv = make_conversation(db)
    agent_env["error"] = GraphRecursionError("step limit")
    events = run_answer(db, conv, make_agent(db), agent_env, [])

    assert [name for name, _ in events] == ["token", "done"]
    assert events[0][1] == chat_module.STEP_LIMIT_SUFFIX  # 标记先补流出去再落库
    assert messages(db, conv.id)[-1].content == chat_module.STEP_LIMIT_SUFFIX.strip()


# ---- 九、问答落库后的收尾任务 -----------------------------------------------------


class StubMemory:
    """顶替 ``MemoryService``：只记账，顺便决定哪一步该抛异常。

    ``after_answer_task`` 用的是自己开的 session，所以这里连 db 一起收下，
    好验证它拿到的确实是那条会话。
    """

    env: dict = {}

    def __init__(self, db, owner_id) -> None:
        self.db = db
        self.owner_id = owner_id

    async def refresh_summary(self, conversation, config, *, history_turns):
        self.env["calls"].append(("refresh", conversation.id, config.id, history_turns))
        if self.env["refresh_error"] is not None:
            raise self.env["refresh_error"]

    async def extract_memories(self, conversation, config, agent):
        self.env["calls"].append(("extract", conversation.id, config.id, agent and agent.slug))
        if self.env["extract_error"] is not None:
            raise self.env["extract_error"]


@pytest.fixture()
def memory(monkeypatch):
    env: dict = {"calls": [], "refresh_error": None, "extract_error": None}
    StubMemory.env = env
    monkeypatch.setattr(chat_module, "MemoryService", StubMemory)
    return env


def test_wrap_up_refreshes_summary_then_extracts(db, memory):
    conv = make_conversation(db)
    agent = make_agent(db)
    conv.agent_id = agent.id
    db.commit()

    wait(chat_module.after_answer_task(OWNER, conv.id, 6))

    assert memory["calls"] == [
        ("refresh", conv.id, chat_config(db).id, 6),
        ("extract", conv.id, chat_config(db).id, agent.slug),
    ]


def test_wrap_up_follows_the_conversations_own_model_config(db, memory):
    conv = make_conversation(db, model_config_id=add_config(db, title="会话指定").id)
    wait(chat_module.after_answer_task(OWNER, conv.id, 6))
    chosen = db.scalar(select(ModelConfig).where(ModelConfig.title == "会话指定"))
    assert memory["calls"] == [
        ("refresh", conv.id, chosen.id, 6),
        ("extract", conv.id, chosen.id, None),  # 没绑智能体：提取门槛不满足，仍要交给它判断
    ]


def test_wrap_up_does_nothing_when_the_conversation_is_gone(db, memory):
    """回答落库后用户把会话删了：这一步没有对象可算。"""
    wait(chat_module.after_answer_task(OWNER, 4242, 6))
    assert memory["calls"] == []


def test_wrap_up_does_nothing_when_the_chat_config_is_gone(db, memory):
    """对话模型没配好不该拖垮收尾，也不该拿一份不相干的配置去算摘要。"""
    conv = make_conversation(db)
    row = chat_config(db)
    row.last_test_ok = False  # 现在没有「可用」的默认对话配置了
    db.commit()

    wait(chat_module.after_answer_task(OWNER, conv.id, 6))
    assert memory["calls"] == []


def test_wrap_up_survives_a_dangling_config_reference(db, memory):
    """会话指着一份已经不存在的配置：读它抛的是 404 那条 ``LookupError``，
    收尾任务一样得咽下去——以前这里没人接，异常只落在事件循环的日志里。"""
    conv = make_conversation(db, model_config_id=add_config(db, title="删掉的那份").id)
    gone = db.scalar(select(ModelConfig).where(ModelConfig.title == "删掉的那份"))
    db.delete(gone)
    db.commit()
    db.expire_all()
    assert conv.model_config_id is not None  # 悬空 id 就是这一刀要钉的旧数据形态

    wait(chat_module.after_answer_task(OWNER, conv.id, 6))
    assert memory["calls"] == []


def test_wrap_up_keeps_going_when_the_summary_fails(db, memory, caplog):
    """两步各自兜底：摘要炸了不影响记忆提取，反之亦然。"""
    conv = make_conversation(db)
    memory["refresh_error"] = RuntimeError("provider 503")
    with caplog.at_level("ERROR", logger="yangvis.memory"):
        wait(chat_module.after_answer_task(OWNER, conv.id, 6))
    assert [name for name, *_ in memory["calls"]] == ["refresh", "extract"]
    assert "summary refresh failed" in caplog.text


def test_wrap_up_logs_a_failing_extraction_without_dying(db, memory, caplog):
    conv = make_conversation(db)
    memory["extract_error"] = RuntimeError("bad json")
    with caplog.at_level("ERROR", logger="yangvis.memory"):
        wait(chat_module.after_answer_task(OWNER, conv.id, 6))
    assert "memory extraction failed" in caplog.text


def test_wrap_up_passes_no_agent_when_the_row_was_deleted(db, memory):
    conv = make_conversation(db)
    agent = make_agent(db)
    conv.agent_id = agent.id
    db.commit()
    db.delete(agent)
    db.commit()

    wait(chat_module.after_answer_task(OWNER, conv.id, 6))
    assert memory["calls"] == [
        ("refresh", conv.id, chat_config(db).id, 6),
        ("extract", conv.id, chat_config(db).id, None),
    ]


def test_wrap_up_runs_on_a_session_it_opened_itself(db, memory, monkeypatch):
    """SSE 流一结束请求级 session 就关了，收尾任务必须自己开、自己关。"""
    conv = make_conversation(db)
    opened: list = []
    real_maker = sessionmaker(bind=db.get_bind(), future=True)

    def spied():
        session = real_maker()
        opened.append(session)
        original_close = session.close

        def close():
            opened.append("closed")
            original_close()

        session.close = close
        return session

    monkeypatch.setattr(chat_module, "SessionLocal", spied)
    wait(chat_module.after_answer_task(OWNER, conv.id, 6))

    assert [type(x).__name__ for x in opened[:1]] == ["Session"]
    assert "closed" in opened
    assert memory["calls"]  # 用的就是这个 session 读到的会话

