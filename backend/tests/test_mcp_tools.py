"""MCP：密钥掩码合并、传输字段校验、挂载解析、SDK 工具转换契约与有界工具循环。

跑真实 SQLite（临时文件库）：mask 合并与 resolve 必须对着真实的行才有意义；
模型本身全部打桩——run_agent 要验证的是消息编排，不是上游模型。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entities import Agent, McpServer, ModelConfig
from app.models.schemas import McpServerCreate, McpServerUpdate, ModelPurpose
from app.services import agent_run as agent_run_module
from app.services.agent_run import TOOL_OUTPUT_LIMIT, run_agent
from app.services.mcp_servers import (
    McpServerService,
    read_secret,
    validate_transport_fields,
)
from app.services.mcp_tools import resolve_servers, server_prefix

OWNER = 1


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path):
    # 文件库：与 test_workflows 同一理由，避免内存库多连接互不可见。
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp.db'}", connect_args={"check_same_thread": False}
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
    monkeypatch.setattr("app.database.SessionLocal", maker)
    try:
        yield session
    finally:
        session.close()


# ---- 小工具 -----------------------------------------------------------------


def _stdio_create(**overrides) -> McpServerCreate:
    base = {
        "name": "时间服务",
        "transport": "stdio",
        "command": "python",
        "args": [],
        "env": {},
    }
    base.update(overrides)
    return McpServerCreate(**base)


def _server_row(
    db,
    *,
    name: str = "时间服务",
    transport: str = "stdio",
    enabled: bool = True,
    owner_id: int = OWNER,
) -> McpServer:
    server = McpServer(
        owner_id=owner_id,
        name=name,
        transport=transport,
        enabled=enabled,
        command="python" if transport == "stdio" else None,
        args=[] if transport == "stdio" else None,
        url=None if transport == "stdio" else "http://localhost:1/sse",
    )
    db.add(server)
    db.commit()
    return server


def _agent(db) -> Agent:
    agent = Agent(
        owner_id=OWNER,
        slug="writer",
        name="写手",
        system_prompt="你是写手",
        temperature=30,
    )
    db.add(agent)
    db.commit()
    return agent


def _chat_model(db) -> ModelConfig:
    config = ModelConfig(
        owner_id=OWNER,
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


# ---- 密钥掩码合并 ------------------------------------------------------------


def test_secret_round_trip_mask_merge_and_delete(db):
    service = McpServerService(db, OWNER)
    server = service.create(
        _stdio_create(env={"API_KEY": "secretvalue123"})
    )

    item = service.to_item(server)
    # 回参绝不出明文：mask 带末 4 位。
    assert item.env == {"API_KEY": "sk-****e123"}

    service.update(
        server.id,
        # 掩码原样回传 = 不改；新键写新明文。
        McpServerUpdate(env={"API_KEY": "sk-****e123", "EXTRA": "plain456"}),
    )
    assert read_secret(server.env_enc) == {
        "API_KEY": "secretvalue123",
        "EXTRA": "plain456",
    }

    service.update(server.id, McpServerUpdate(env={"EXTRA": "changed999"}))
    assert read_secret(server.env_enc)["EXTRA"] == "changed999"

    # 空值删键。
    service.update(server.id, McpServerUpdate(env={"API_KEY": ""}))
    assert read_secret(server.env_enc) == {"EXTRA": "changed999"}


def test_headers_round_trip_for_remote_transport(db):
    service = McpServerService(db, OWNER)
    server = service.create(
        McpServerCreate(
            name="远端",
            transport="sse",
            url="http://localhost:1/sse",
            headers={"Authorization": "Bearer token1234"},
        )
    )

    assert service.to_item(server).headers == {"Authorization": "sk-****1234"}
    service.update(
        server.id, McpServerUpdate(headers={"Authorization": "sk-****1234"})
    )
    assert read_secret(server.headers_enc) == {"Authorization": "Bearer token1234"}
    service.update(
        server.id, McpServerUpdate(headers={"Authorization": ""})
    )
    assert read_secret(server.headers_enc) == {}


def test_switching_transport_drops_the_other_secret_bag(db):
    """stdio 改 sse：env 袋失效清掉；headers 袋建立。"""
    service = McpServerService(db, OWNER)
    server = service.create(
        _stdio_create(env={"API_KEY": "secretvalue123"})
    )

    service.update(
        server.id,
        McpServerUpdate(
            transport="sse",
            url="http://localhost:1/sse",
            headers={"X-Token": "headervalue"},
        ),
    )

    assert server.env_enc is None
    assert read_secret(server.headers_enc) == {"X-Token": "headervalue"}


def test_connection_change_resets_the_old_test_result(db):
    service = McpServerService(db, OWNER)
    server = service.create(_stdio_create())
    service.record_test(server, ok=True, message="连接成功")

    service.update(server.id, McpServerUpdate(command="python3"))

    assert server.last_test_ok is None and server.last_test_msg is None


# ---- 传输字段校验 ------------------------------------------------------------


def test_validate_transport_fields():
    validate_transport_fields("stdio", command="uvx", url=None)
    validate_transport_fields("sse", command=None, url="http://x/sse")
    validate_transport_fields("streamable_http", command=None, url="http://x/mcp")

    with pytest.raises(ValueError, match="启动命令"):
        validate_transport_fields("stdio", command="  ", url=None)
    with pytest.raises(ValueError, match="服务 URL"):
        validate_transport_fields("sse", command=None, url="")
    with pytest.raises(ValueError, match="服务 URL"):
        validate_transport_fields("streamable_http", command=None, url=None)
    with pytest.raises(ValueError, match="不支持的传输类型"):
        validate_transport_fields("websocket", command=None, url=None)


def test_create_rejects_incomplete_transport(db):
    with pytest.raises(ValueError, match="启动命令"):
        McpServerService(db, OWNER).create(_stdio_create(command=" "))
    with pytest.raises(ValueError, match="服务 URL"):
        McpServerService(db, OWNER).create(
            McpServerCreate(name="远端", transport="sse")
        )


# ---- 挂载解析 ----------------------------------------------------------------


def test_resolve_servers_empty_returns_empty_list(db):
    assert resolve_servers(db, OWNER, []) == []


def test_resolve_servers_dedups_and_keeps_input_order(db):
    a = _server_row(db, name="a")
    b = _server_row(db, name="b")

    rows = resolve_servers(db, OWNER, [b.id, a.id, b.id])

    assert [row.id for row in rows] == [b.id, a.id]


def test_resolve_servers_rejects_missing_and_foreign_ids(db):
    mine = _server_row(db)
    _server_row(db, name="别人的", owner_id=OWNER + 1)

    with pytest.raises(LookupError, match="不存在或无权访问"):
        resolve_servers(db, OWNER, [999999])
    with pytest.raises(LookupError, match="不存在或无权访问"):
        resolve_servers(db, OWNER, [mine.id + 1])


def test_resolve_servers_rejects_disabled(db):
    enabled = _server_row(db, name="启用")
    disabled = _server_row(db, name="停用", enabled=False)

    with pytest.raises(ValueError, match="已停用"):
        resolve_servers(db, OWNER, [enabled.id, disabled.id])


def test_server_prefix():
    assert server_prefix("A B") == "A_B"
    assert server_prefix("x.y") == "x_y"
    assert server_prefix("--") == "mcp"
    assert server_prefix("中文") == "mcp"
    assert server_prefix("ok") == "ok"


# ---- SDK 工具转换契约（FastMCP 内存对连，无外网无真实进程）---------------------


def test_fastmcp_tools_load_and_invoke_contract():
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session
    from langchain_mcp_adapters.tools import load_mcp_tools

    mcp = FastMCP("calc")

    @mcp.tool()
    async def add(a: int, b: int) -> int:
        """两数相加。"""
        return a + b

    async def drive():
        async with create_connected_server_and_client_session(mcp) as session:
            tools = await load_mcp_tools(
                session, server_name="calc", tool_name_prefix=True
            )
            assert [tool.name for tool in tools] == ["calc_add"]
            return await tools[0].ainvoke({"a": 2, "b": 3})

    assert "5" in str(asyncio.run(drive()))


# ---- run_agent 有界工具循环 --------------------------------------------------


class FakeAIMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        # 形状与 langchain AIMessage.tool_calls 一致。
        self.tool_calls = tool_calls or []


class FakeChatModel:
    """按脚本依次返回：script 前若干条给 bound.ainvoke，末条给未绑定收敛调用。"""

    def __init__(self, script):
        self.script = list(script)
        self.cursor = 0
        self.received: list[list] = []
        self.bound_tools: list = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages):
        self.received.append(list(messages))
        item = self.script[self.cursor]
        self.cursor += 1
        return item


def _make_tool(*, name: str = "echo_tool", output: str = "echo:hi", raises: Exception | None = None):
    from langchain_core.tools import StructuredTool

    def _sync(text: str = "x") -> str:
        if raises:
            raise raises
        return output

    async def _coro(text: str = "x") -> str:
        return _sync(text)

    return StructuredTool.from_function(
        _sync, coroutine=_coro, name=name, description="测试工具"
    )


def _run_with_fake_model(db, fake, tools):
    def _factory(config, **kwargs):
        return fake

    async def drive():
        # run_agent 在模块顶部以「from ... import build_chat_model」绑定，
        # 打桩必须落在 agent_run 模块属性上。
        original = agent_run_module.build_chat_model
        agent_run_module.build_chat_model = _factory
        try:
            return await run_agent(
                db, OWNER, "writer", "查一下", tools=tools
            )
        finally:
            agent_run_module.build_chat_model = original

    return asyncio.run(drive())


def test_run_agent_runs_a_tool_round_and_returns_final_answer(db):
    _agent(db)
    _chat_model(db)
    tool = _make_tool()
    fake = FakeChatModel(
        [
            FakeAIMessage(
                "我先查",
                [{"name": "echo_tool", "args": {"text": "hi"}, "id": "c1"}],
            ),
            FakeAIMessage("最终答案", []),
        ]
    )

    result = _run_with_fake_model(db, fake, [tool])

    assert result == "最终答案"
    assert fake.bound_tools == [tool]
    # 第二轮调用收到的消息序列：System/Human/AIMessage/ToolMessage，
    # ToolMessage 带回工具输出与对应的 tool_call_id。
    from langchain_core.messages import ToolMessage

    second_round = fake.received[1]
    tool_message = second_round[3]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "echo:hi"
    assert tool_message.tool_call_id == "c1"


def test_run_agent_handles_an_unknown_tool_name(db):
    _agent(db)
    _chat_model(db)
    fake = FakeChatModel(
        [
            FakeAIMessage(
                "点名一个不存在的工具",
                [{"name": "ghost", "args": {}, "id": "c2"}],
            ),
            FakeAIMessage("改别的工具查到了", []),
        ]
    )

    assert _run_with_fake_model(db, fake, [_make_tool()]) == "改别的工具查到了"

    from langchain_core.messages import ToolMessage

    message = fake.received[1][3]
    assert isinstance(message, ToolMessage)
    assert "工具 ghost 不存在" in message.content


def test_run_agent_feeds_a_tool_failure_back_without_crashing(db):
    _agent(db)
    _chat_model(db)
    tool = _make_tool(raises=RuntimeError("boom"))
    fake = FakeChatModel(
        [
            FakeAIMessage(
                "试工具",
                [{"name": "echo_tool", "args": {}, "id": "c3"}],
            ),
            FakeAIMessage("知道失败了", []),
        ]
    )

    assert _run_with_fake_model(db, fake, [tool]) == "知道失败了"
    assert "工具调用失败：boom" in fake.received[1][3].content


def test_run_agent_truncates_oversized_tool_output(db):
    _agent(db)
    _chat_model(db)
    long_output = "x" * (TOOL_OUTPUT_LIMIT + 500)
    tool = _make_tool(output=long_output)
    fake = FakeChatModel(
        [
            FakeAIMessage(
                "查长文本",
                [{"name": "echo_tool", "args": {}, "id": "c4"}],
            ),
            FakeAIMessage("读完了", []),
        ]
    )

    _run_with_fake_model(db, fake, [tool])

    content = fake.received[1][3].content
    assert len(content) < len(long_output)
    assert "工具输出过长，已截断" in content


def test_run_agent_forces_convergence_after_max_rounds(db):
    _agent(db)
    _chat_model(db)
    tool_call = {
        "name": "echo_tool",
        "args": {},
        "id": "c5",
    }
    # 8 轮都在调工具 → 走未绑定模型的强制收敛分支。
    fake = FakeChatModel(
        [FakeAIMessage("继续查", [tool_call])] * 8
        + [FakeAIMessage("收敛答案")]
    )

    assert _run_with_fake_model(db, fake, [_make_tool()]) == "收敛答案"
    # 恰好 8 次工具轮 + 1 次收敛调用。
    assert fake.cursor == 9
    nudge = fake.received[-1][-1]
    assert "工具调用次数已达上限" in nudge.content
