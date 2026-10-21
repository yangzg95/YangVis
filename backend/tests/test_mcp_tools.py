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
from app.services.mcp import (
    McpServerService,
    read_secret,
    validate_transport_fields,
)
from app.services.mcp.tools import (
    describe_connection_error,
    describe_tool_call_error,
    plan_step_tools,
    project_tool_result,
    resolve_servers,
    server_prefix,
    snapshot_server_tools,
)

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
    service.record_test(
        server, ok=True, message="连接成功", tools=[{"name": "t", "description": "", "input_schema": {}}]
    )

    service.update(server.id, McpServerUpdate(command="python3"))

    assert server.last_test_ok is None and server.last_test_msg is None
    assert server.tools_snapshot is None


def test_record_test_persists_snapshot_on_success_and_clears_on_failure(db):
    service = McpServerService(db, OWNER)
    server = service.create(_stdio_create())
    tools = [
        {
            "name": "echo",
            "description": "回声",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }
    ]

    service.record_test(server, ok=True, message="连接成功，发现 1 个工具", tools=tools)
    item = service.to_item(server)
    assert item.tools is not None and len(item.tools) == 1
    assert item.tools[0].name == "echo"
    assert item.tools[0].input_schema["properties"]["text"] == {"type": "string"}

    # 失败不能留下旧清单。
    service.record_test(server, ok=False, message="HTTP 401")
    assert server.tools_snapshot is None
    assert service.to_item(server).tools is None


def test_non_connection_update_keeps_the_snapshot(db):
    service = McpServerService(db, OWNER)
    server = service.create(_stdio_create())
    service.record_test(
        server, ok=True, message="ok", tools=[{"name": "t", "description": "", "input_schema": {}}]
    )

    # 只改名称/备注：连接没变，快照保留。
    service.update(server.id, McpServerUpdate(name="改名后的时间服务"))
    assert server.tools_snapshot is not None


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


def test_describe_connection_error_unwraps_http_status_group():
    # 千帆 401 的真实形态：TaskGroup 里包一个 httpx.HTTPStatusError，
    # 响应体是 text/plain 的 {"code": ..., "message": "..."}。
    import httpx

    request = httpx.Request("POST", "https://x/mcp")
    response = httpx.Response(
        401,
        request=request,
        text='{"code":216003,"message":"[Code: InvalidHTTPAuthHeader]"}',
        headers={"content-type": "text/plain"},
    )
    group = ExceptionGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)",
        [httpx.HTTPStatusError("Client error '401'", request=request, response=response)],
    )

    message = describe_connection_error(group)

    assert "TaskGroup" not in message
    assert "401" in message
    assert "InvalidHTTPAuthHeader" in message


def test_describe_connection_error_passes_plain_exception_through():
    assert describe_connection_error(RuntimeError("连接被拒绝")) == "连接被拒绝"


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


def test_snapshot_server_tools_keeps_name_description_and_input_schema():
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session

    mcp = FastMCP("docs")

    @mcp.tool()
    async def search(query: str, limit: int = 5) -> str:
        """搜索文档。

        Args:
            query: 查询词。
            limit: 返回条数。
        """
        return query

    async def drive():
        async with create_connected_server_and_client_session(mcp) as session:
            return await snapshot_server_tools(session)

    snapshots = asyncio.run(drive())

    assert [tool["name"] for tool in snapshots] == ["search"]
    snapshot = snapshots[0]
    assert snapshot["description"].startswith("搜索文档")
    properties = snapshot["input_schema"]["properties"]
    assert set(properties) == {"query", "limit"}
    assert properties["query"]["type"] == "string"
    assert properties["limit"]["type"] == "integer"
    assert snapshot["input_schema"]["required"] == ["query"]


# ---- 方法测试：tools/call 结果投影与错误描述 ----------------------------------


def test_project_tool_result_success_and_is_error():
    """成功调用投影出 text 内容块；工具自己抛错时 isError=True 仍是协议成功。"""
    import json

    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session

    mcp = FastMCP("calc")

    @mcp.tool()
    async def add(a: int, b: int) -> int:
        """两数相加。"""
        return a + b

    @mcp.tool()
    async def boom(query: str) -> str:
        """一定会炸的工具。"""
        raise ValueError("炸了")

    async def drive():
        async with create_connected_server_and_client_session(mcp) as session:
            ok_result = await session.call_tool("add", {"a": 2, "b": 3})
            err_result = await session.call_tool("boom", {"query": "x"})
            return ok_result, err_result

    ok_raw, err_raw = asyncio.run(drive())

    ok = project_tool_result(ok_raw)
    assert ok["is_error"] is False
    text_block = ok["content"][0]
    assert text_block["type"] == "text"
    assert json.loads(text_block["text"]) == 5

    # 工具异常被 FastMCP 兜成 isError 文本块，不是连接/协议失败。
    err = project_tool_result(err_raw)
    assert err["is_error"] is True
    assert err["content"]
    assert "炸了" in str(err["content"])


def test_describe_tool_call_error_formats_mcp_error():
    """JSON-RPC 错误转成带 code 的原因；被 TaskGroup 包着也能在叶子里找到。"""
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    exc = McpError(
        ErrorData(code=-32602, message="Unknown tool: ghost", data={"reason": "x"})
    )

    message = describe_tool_call_error(exc)
    assert "code -32602" in message
    assert "ghost" in message

    wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [exc])
    assert "ghost" in describe_tool_call_error(wrapped)

    # 非协议错误回退连接错误口径，不吞信息。
    assert describe_tool_call_error(RuntimeError("连接被拒绝")) == "连接被拒绝"


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


# ---- plan_step_tools：两级挂载的并集、去重与冲突范围 -------------------------


class _Named:
    """一个只带 name 的最小工具：plan 只关心工具名。"""

    def __init__(self, name: str) -> None:
        self.name = name


class _StepLike:
    """一个只带 key 与节点级挂载的最小步骤。"""

    def __init__(self, key: str, ids: tuple = ()) -> None:
        self.key = key
        self.mcp_server_ids = list(ids)


class _BundleLike:
    def __init__(self, tools_by_server: dict) -> None:
        self.tools_by_server = dict(tools_by_server)


@pytest.fixture()
def clash_bundle():
    """两个服务各贡献一个工具，工具名相同（模拟规范化后撞名）。"""
    return _BundleLike(
        {
            1: [_Named("shared_foo")],
            2: [_Named("shared_foo")],
        }
    )


def _names(tools) -> list[str]:
    return [tool.name for tool in tools]


def test_plan_empty_when_nothing_mounted(clash_bundle):
    plan = plan_step_tools(clash_bundle, [], [_StepLike("a")])

    assert plan["a"] == []


def _distinct_bundle() -> _BundleLike:
    """两个服务工具名不同：顺序/并集展示用。"""
    return _BundleLike({1: [_Named("a_tool")], 2: [_Named("b_tool")]})


def test_plan_unions_workflow_and_step_mount():
    bundle = _distinct_bundle()
    plan = plan_step_tools(bundle, [1], [_StepLike("a", (2,))])

    assert _names(plan["a"]) == ["a_tool", "b_tool"]


def test_plan_workflow_level_reaches_every_step():
    bundle = _distinct_bundle()
    plan = plan_step_tools(
        bundle, [1], [_StepLike("a"), _StepLike("b", (2,))]
    )

    assert _names(plan["a"]) == ["a_tool"]
    assert _names(plan["b"]) == ["a_tool", "b_tool"]


def test_plan_same_tool_name_on_different_steps_is_fine(clash_bundle):
    """同名工具分挂不同节点：各自的生效集合里只有一个，放行。"""
    plan = plan_step_tools(
        clash_bundle, [], [_StepLike("a", (1,)), _StepLike("b", (2,))]
    )

    assert _names(plan["a"]) == ["shared_foo"]
    assert _names(plan["b"]) == ["shared_foo"]


def test_plan_clashes_when_same_step_gets_both_servers(clash_bundle):
    """两个服务挂到同一节点：生效集合内工具重名，报错且点名节点。"""
    with pytest.raises(ValueError, match="工具名冲突") as exc_info:
        plan_step_tools(clash_bundle, [], [_StepLike("a", (1, 2))])

    assert "a" in str(exc_info.value)


def test_plan_dedups_repeated_ids(clash_bundle):
    plan = plan_step_tools(clash_bundle, [1, 1], [_StepLike("a", (1,))])

    assert _names(plan["a"]) == ["shared_foo"]
