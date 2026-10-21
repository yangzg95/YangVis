"""会话级可选工具（本地工具 + MCP 服务）的落库与装配。

两半：

1. 会话 CRUD 对 ``local_tool_keys`` / ``mcp_server_ids`` 的语义——去重保序、
   空数组归 NULL、未知本地 key/越权 MCP 在保存时拒绝、停用 MCP 保存放行、
   update 的「替换/清空/保持」三语义、清单条目带字段。
2. ``stream_answer`` 的工具装配：按会话选择挂本地工具与 MCP 工具（后者经
   ``resolve_servers``/``open_bundle`` 并被输出截断壳包一层），ReAct 步数上限
   随 MCP/运维放宽到 25、连接束结束时关闭；各类失效落成用户能看懂的异常；
   什么都不选时生效集合为空（时间工具也是显式勾选的本地工具），只有运维会话
   仍隐式挂时间工具——运维面板没有选择器，保持旧行为。

create_agent 按本仓库既有约定打桩（test_chat_service/test_ops_confirm 同款），
但桩里的 ``("call", 工具名, 参数)`` 脚本步骤会**真的去 ainvoke 装配出来的
LangChain 工具**，所以本地工具的构建壳与 MCP 的截断壳都跑了真实路径。
跑文件型 SQLite，不连共享 MySQL、不开任何真实 MCP 连接。
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import List

import pytest
from langchain_core.tools import StructuredTool
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entities import (
    Agent,
    ChatConversation,
    ChatMessage,
    McpServer,
    ModelConfig,
)
from app.models.schemas import ModelPurpose
from app.services.chat import ChatService, ROLE_ASSISTANT, ROLE_USER
from app.services.chat import service as chat_module

OWNER = 1
OTHER = 2

wait = asyncio.run


async def _drain(agen) -> list:
    return [item async for item in agen]


# ---- 打桩：假事件流（call 步骤真实调用装配出的工具） ---------------------------


class ScriptedStream:
    """按脚本吐 v2 事件；``("call", name, args)`` 会真调一次装配出的工具。

    与 test_chat_service 的 ScriptedStream 同理不做成 async generator：
    被测代码在 finally 里调 ``aclose()``，测试要能直接看见它发生。
    """

    def __init__(self, script, tools, env) -> None:
        self._script = list(script)
        self._tools = {getattr(tool, "name", ""): tool for tool in tools}
        self._env = env
        self._index = 0
        # 同名工具的 call/end 按栈配对 run_id，与真实 astream_events 同口径。
        self._run_ids: dict = {}

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._script):
            raise StopAsyncIteration
        step = self._script[self._index]
        self._index += 1
        if isinstance(step, tuple) and step[0] == "call":
            _, name, args = step
            run_id = uuid.uuid4().hex
            self._run_ids.setdefault(name, []).append(run_id)
            output = await self._tools[name].ainvoke(args)
            self._env["tool_outputs"].append((name, output))
            return {
                "event": "on_tool_start",
                "name": name,
                "run_id": run_id,
                "data": {"input": args},
            }
        if isinstance(step, tuple) and step[0] == "end":
            name = step[1]
            run_id = self._run_ids.get(name, [""]).pop(0)
            return {
                "event": "on_tool_end",
                "name": name,
                "run_id": run_id,
                "data": {"output": "ok"},
            }
        kind, text = step
        return {
            "event": "on_chat_model_stream",
            "data": {"chunk": SimpleNamespace(content=text)},
        }

    async def aclose(self):
        self._env["closes"].append("stream")


class FakeAgent:
    def __init__(self, script, env, tools) -> None:
        self._script = script
        self._env = env
        self._tools = list(tools)

    def astream_events(self, state, *, config=None, version=None):
        self._env["captured"]["recursion_limit"] = (config or {}).get("recursion_limit")
        self._env["captured"]["version"] = version
        self._env["captured"]["tools"] = [getattr(t, "name", str(t)) for t in self._tools]
        return ScriptedStream(self._script, self._tools, self._env)


@pytest.fixture()
def runtime(monkeypatch):
    """打桩 create_agent/build_chat_model/收尾任务，MCP 解析默认不许被碰到。"""
    env: dict = {
        "captured": {},
        "closes": [],
        "tool_outputs": [],
        "script": [],
    }

    def create_agent(client, tools, system_prompt=None):
        return FakeAgent(env["script"], env, list(tools))

    def build_model(config, **kwargs):
        return SimpleNamespace(name="fake-model")

    monkeypatch.setattr("langchain.agents.create_agent", create_agent)
    monkeypatch.setattr(chat_module, "build_chat_model", build_model)
    # 收尾协程不跑：close 掉免得「coroutine was never awaited」告警。
    monkeypatch.setattr(chat_module, "fire_and_forget", lambda task: task.close())
    # 没有挂 MCP 的用例绝不该触发解析/连接：挂了就让用例当场失败。
    monkeypatch.setattr(
        chat_module,
        "resolve_servers",
        lambda *a, **k: pytest.fail("resolve_servers 不应被调用"),
    )
    monkeypatch.setattr(
        chat_module,
        "open_bundle",
        lambda *a, **k: pytest.fail("open_bundle 不应被调用"),
    )
    return env


# ---- 造数据 ------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'chat_tools.db'}",
        connect_args={"check_same_thread": False},
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


def svc(db, owner_id: int = OWNER) -> ChatService:
    return ChatService(db, owner_id)


def make_agent(db, **flags) -> Agent:
    fields = {
        "owner_id": OWNER,
        "slug": "tool-chat",
        "name": "工具助手",
        "system_prompt": "你是助手。",
        "use_knowledge": False,
        "use_ops": False,
        "use_memory": False,
    }
    fields.update(flags)
    agent = Agent(**fields)
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def mcp_server(
    db,
    *,
    owner_id: int = OWNER,
    enabled: bool = True,
    name: str = "计算服务",
) -> McpServer:
    server = McpServer(
        owner_id=owner_id,
        name=name,
        transport="stdio",
        enabled=enabled,
        command="python",
        args=[],
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    return server


def make_conversation(db, **kwargs) -> ChatConversation:
    row = ChatConversation(owner_id=OWNER, title="工具用例会话", **kwargs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def run_answer(db, conv, agent, env, script) -> list:
    env["script"] = list(script)
    service = svc(db)
    service.add_message(conv.id, ROLE_USER, "调用一下工具")
    return wait(
        _drain(
            service.stream_answer(
                conversation_id=conv.id,
                agent=agent,
                question="调用一下工具",
                config=svc(db).require_chat_config(),
            )
        )
    )


def assistant_messages(db, conv_id: int) -> List[ChatMessage]:
    return list(
        db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.owner_id == OWNER,
                ChatMessage.conversation_id == conv_id,
                ChatMessage.role == ROLE_ASSISTANT,
            )
            .order_by(ChatMessage.id)
        ).all()
    )


# ---- 一、会话 CRUD 的选择落库语义 ----------------------------------------------


def test_create_persists_selection_dedup_and_order(db):
    server = mcp_server(db)
    row = svc(db).create_conversation(
        local_tool_keys=["generate_uuid", "generate_uuid", "random_integer"],
        mcp_server_ids=[server.id, server.id],
    )
    assert row.local_tool_keys == ["generate_uuid", "random_integer"]
    assert row.mcp_server_ids == [server.id]


def test_create_normalizes_empty_lists_to_null(db):
    row = svc(db).create_conversation(local_tool_keys=[], mcp_server_ids=[])
    assert row.local_tool_keys is None and row.mcp_server_ids is None


def test_create_rejects_unknown_local_key_and_persists_nothing(db):
    before = db.scalar(select(func.count()).select_from(ChatConversation))
    with pytest.raises(ValueError, match="本地工具已不可用：no_such_tool"):
        svc(db).create_conversation(local_tool_keys=["no_such_tool"])
    db.rollback()
    after = db.scalar(select(func.count()).select_from(ChatConversation))
    assert before == after


def test_create_rejects_missing_or_foreign_mcp_server(db):
    other_server = mcp_server(db, owner_id=OTHER, name="别人的服务")
    with pytest.raises(ValueError, match="不存在或不属于本人"):
        svc(db).create_conversation(mcp_server_ids=[999_999])
    db.rollback()
    with pytest.raises(ValueError, match="不存在或不属于本人"):
        svc(db).create_conversation(mcp_server_ids=[other_server.id])
    db.rollback()


def test_create_allows_disabled_mcp_server_at_save_time(db):
    server = mcp_server(db, enabled=False, name="待启用服务")
    row = svc(db).create_conversation(mcp_server_ids=[server.id])
    assert row.mcp_server_ids == [server.id]


def test_update_replace_clear_and_keep_semantics(db):
    first = mcp_server(db, name="服务甲")
    second = mcp_server(db, name="服务乙")
    row = svc(db).create_conversation(
        local_tool_keys=["generate_uuid"], mcp_server_ids=[first.id]
    )

    # 替换：点名的覆盖，没点名的保持不动。
    row = svc(db).update_conversation(
        row.id, local_tool_keys=["random_integer"], mcp_server_ids=[second.id]
    )
    assert row.local_tool_keys == ["random_integer"]
    assert row.mcp_server_ids == [second.id]

    # 只传一个字段，另一个保持。
    row = svc(db).update_conversation(row.id, local_tool_keys=[])
    assert row.local_tool_keys is None and row.mcp_server_ids == [second.id]

    # 连字段都不传（改标题），两者都保持。
    row = svc(db).update_conversation(row.id, title="改个名")
    assert row.local_tool_keys is None and row.mcp_server_ids == [second.id]
    assert row.title == "改个名"


def test_update_rejects_unknown_key_without_touching_stored_selection(db):
    row = svc(db).create_conversation(local_tool_keys=["generate_uuid"])
    with pytest.raises(ValueError, match="本地工具已不可用：ghost"):
        svc(db).update_conversation(row.id, local_tool_keys=["ghost"])
    db.rollback()
    db.refresh(row)
    assert row.local_tool_keys == ["generate_uuid"]


def test_conversation_list_carries_the_selection(db):
    server = mcp_server(db)
    svc(db).create_conversation(
        local_tool_keys=["generate_uuid"], mcp_server_ids=[server.id]
    )
    items = svc(db).list_conversations()
    assert len(items) == 1
    assert items[0].local_tool_keys == ["generate_uuid"]
    assert items[0].mcp_server_ids == [server.id]


# ---- 二、stream_answer 装配 ----------------------------------------------------


def test_local_tools_are_mounted_run_for_real_and_recorded(db, runtime):
    conv = make_conversation(db, local_tool_keys=["generate_uuid"])
    events = run_answer(
        db,
        conv,
        make_agent(db),
        runtime,
        [("call", "generate_uuid", {"count": 2}), ("end", "generate_uuid"), ("text", "好了")],
    )

    assert runtime["captured"]["tools"] == ["generate_uuid"]
    assert runtime["captured"]["recursion_limit"] == chat_module.MAX_REACT_STEPS
    # 没挂 MCP：连接束打开桩被设成失败，没触发即通过。
    name, output = runtime["tool_outputs"][0]
    assert name == "generate_uuid" and len(output.split(", ")) == 2

    steps = [payload for event, payload in events if event == "step"]
    assert len(steps) == 1 and steps[0]["tool"] == "generate_uuid"
    assert steps[0]["query"] == "" and steps[0]["input"] == {"count": 2}
    run_id = steps[0]["run_id"]
    assert isinstance(run_id, str) and run_id
    # 工具返回要逐次推 step_result，run_id 与开始事件对得上。
    results = [payload for event, payload in events if event == "step_result"]
    assert results == [{"run_id": run_id, "output": "ok", "is_error": False}]
    assert events[-1][0] == "done"
    assert events[-1][1]["conversation_id"] == conv.id
    assert isinstance(events[-1][1]["message_id"], int)

    saved = assistant_messages(db, conv.id)
    assert len(saved) == 1
    assert {
        "type": "step",
        "tool": "generate_uuid",
        "query": "",
        "input": {"count": 2},
        "run_id": run_id,
        "output": "ok",
        "is_error": False,
    } in (saved[0].timeline or [])
    assert runtime["closes"] == ["stream"]


def _mcp_calc_tool() -> StructuredTool:
    def calc_add(a: int, b: int) -> int:
        """两数相加。"""
        return a + b

    return StructuredTool.from_function(calc_add, name="calc_add", description="两数相加")


class _FakeBundle:
    def __init__(self, tools) -> None:
        self.tools = list(tools)
        self.exits = 0

    async def __aexit__(self, *exc_info) -> None:
        self.exits += 1


def _patch_mcp(monkeypatch, env, *, servers=None, resolve=None, open_=None):
    resolved: list = []
    opened: list = []
    bundle = _FakeBundle([_mcp_calc_tool()])
    env["bundle"] = bundle

    def fake_resolve(db_, owner_id, ids):
        resolved.append((owner_id, list(ids)))
        if resolve is not None:
            raise resolve
        return servers if servers is not None else [SimpleNamespace(id=i) for i in ids]

    async def fake_open(servers_):
        opened.append(list(servers_))
        if open_ is not None:
            raise open_
        return bundle

    monkeypatch.setattr(chat_module, "resolve_servers", fake_resolve)
    monkeypatch.setattr(chat_module, "open_bundle", fake_open)
    return resolved, opened, bundle


def test_mcp_tools_are_mounted_capped_and_bundle_closed(db, runtime, monkeypatch):
    server = mcp_server(db)
    conv = make_conversation(db, mcp_server_ids=[server.id])
    resolved, opened, bundle = _patch_mcp(monkeypatch, runtime)

    events = run_answer(
        db,
        conv,
        make_agent(db),
        runtime,
        [("call", "calc_add", {"a": 2, "b": 3}), ("end", "calc_add"), ("text", "等于 5")],
    )

    assert resolved == [(OWNER, [server.id])]
    assert len(opened) == 1
    # 截断壳保留工具名，原始 int 返回在壳外已是字符串。
    assert runtime["captured"]["tools"] == ["calc_add"]
    assert runtime["tool_outputs"] == [("calc_add", "5")]
    assert runtime["captured"]["recursion_limit"] == chat_module.OPS_REACT_STEPS
    steps = [p for e, p in events if e == "step"]
    assert len(steps) == 1
    assert steps[0]["tool"] == "calc_add"
    assert steps[0]["query"] == "" and steps[0]["input"] == {"a": 2, "b": 3}
    assert isinstance(steps[0]["run_id"], str) and steps[0]["run_id"]
    assert [p for e, p in events if e == "step_result"] == [
        {"run_id": steps[0]["run_id"], "output": "ok", "is_error": False}
    ]
    assert bundle.exits == 1


def test_missing_mcp_server_at_runtime_gets_a_guiding_error(db, runtime, monkeypatch):
    server = mcp_server(db)
    conv = make_conversation(db, mcp_server_ids=[server.id])
    _patch_mcp(monkeypatch, runtime, resolve=LookupError("挂载的 MCP 服务不存在或无权访问：9"))

    with pytest.raises(LookupError, match="选择的 MCP 服务不可用"):
        run_answer(db, conv, make_agent(db), runtime, [("text", "x")])


def test_disabled_mcp_server_at_runtime_gets_a_guiding_error(db, runtime, monkeypatch):
    server = mcp_server(db, enabled=False)
    conv = make_conversation(db, mcp_server_ids=[server.id])
    _patch_mcp(monkeypatch, runtime, resolve=ValueError("挂载的 MCP 服务已停用：计算服务"))

    with pytest.raises(ValueError, match="请重新启用或在工具选择中移除后重试"):
        run_answer(db, conv, make_agent(db), runtime, [("text", "x")])


def test_mcp_connection_failure_is_wrapped_as_value_error(db, runtime, monkeypatch):
    server = mcp_server(db)
    conv = make_conversation(db, mcp_server_ids=[server.id])
    _patch_mcp(monkeypatch, runtime, open_=RuntimeError("连接被拒"))

    with pytest.raises(ValueError, match="MCP 服务连接失败：连接被拒"):
        run_answer(db, conv, make_agent(db), runtime, [("text", "x")])


def test_tool_name_clash_between_local_and_mcp_is_rejected(db, runtime, monkeypatch):
    server = mcp_server(db)
    conv = make_conversation(
        db, local_tool_keys=["generate_uuid"], mcp_server_ids=[server.id]
    )

    async def clash_coroutine(**kwargs):  # noqa: ANN001
        return "x"

    clash_tool = StructuredTool.from_function(
        coroutine=clash_coroutine, name="generate_uuid", description="撞名工具"
    )
    bundle = _FakeBundle([clash_tool])

    monkeypatch.setattr(
        chat_module, "resolve_servers", lambda db_, owner, ids: [SimpleNamespace(id=i) for i in ids]
    )

    async def fake_open(servers):
        return bundle

    monkeypatch.setattr(chat_module, "open_bundle", fake_open)

    with pytest.raises(ValueError, match="工具名冲突：generate_uuid"):
        run_answer(db, conv, make_agent(db), runtime, [("text", "x")])


def test_plain_conversation_without_selection_mounts_no_tools(db, runtime):
    conv = make_conversation(db)
    events = run_answer(
        db, conv, make_agent(db), runtime, [("text", "普通回答")]
    )

    # 时间工具已改成显式勾选的本地工具：什么都不选就是零工具纯文本对话。
    assert runtime["captured"]["tools"] == []
    assert runtime["captured"]["recursion_limit"] == chat_module.MAX_REACT_STEPS
    assert not [payload for event, payload in events if event == "step"]
    assert events[-1][0] == "done"


def test_ops_conversation_keeps_datetime_mounted_without_selection(db, runtime):
    # 运维面板没有工具选择器：时间工具随工具箱继续默认挂载，且排在工具箱之前。
    conv = make_conversation(db)
    events = run_answer(
        db, conv, make_agent(db, use_ops=True), runtime, [("text", "运维回答")]
    )

    names = runtime["captured"]["tools"]
    assert names[0] == "current_datetime"
    assert "list_servers" in names  # 自由模式工具箱照常挂上
    assert runtime["captured"]["recursion_limit"] == chat_module.OPS_REACT_STEPS
    assert not [payload for event, payload in events if event == "step"]
    assert events[-1][0] == "done"
