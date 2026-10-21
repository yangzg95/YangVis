"""MCP 连接束：多服务的会话在整个工作流运行期间保持打开，结束统一关闭。

工作流 ``workflow.mcp_server_ids`` 里的每个服务在这里各建一条传输 + 一个
``ClientSession``，经 ``langchain-mcp-adapters`` 转成 LangChain 工具后聚合：
同组并发步骤共享同一条连接束（JSON-RPC 按请求 id 多路复用）。

API 已对照 mcp==1.30.0 / langchain-mcp-adapters==0.3.2 源码核实：
- stdio：``stdio_client(StdioServerParameters(...))``；
- sse：``sse_client(url, headers=...)``；
- streamable_http：1.30 的 ``streamable_http_client`` 不再直接收 headers，
  认证头要通过预配置的 ``httpx.AsyncClient`` 传入。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import anyio
import httpx
from langchain_core.tools import BaseTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import decrypt
from app.models.entities import McpServer, WorkflowStep
from app.services.mcp.servers import (
    TRANSPORT_HTTP,
    TRANSPORT_SSE,
    TRANSPORT_STDIO,
)

logger = logging.getLogger("yangvis.mcp_tools")

# initialize 上限：stdio 命令写错（假启动）时靠它快速失败，而不是干等。
CONNECT_TIMEOUT = 30.0
# ClientSession 每次请求的读超时；SDK 默认 None（永不超时），必须显式给。
READ_TIMEOUT = 60.0

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_]+")


def server_prefix(name: str) -> str:
    """服务名 → 工具前缀：非字母数字下划线一律换成 ``_``。"""
    return _UNSAFE_NAME.sub("_", name).strip("_") or "mcp"


def _flatten_exc_leaves(exc: BaseException) -> List[BaseException]:
    """递归展开 ExceptionGroup（anyio TaskGroup 的失败形态），返回叶子异常。"""
    leaves: List[BaseException] = []

    def walk(node: BaseException) -> None:
        if isinstance(node, BaseExceptionGroup):
            for child in node.exceptions:
                walk(child)
        else:
            leaves.append(node)

    walk(exc)
    return leaves


async def _buffer_error_response(response: httpx.Response) -> None:
    """httpx 响应钩子：非 2xx 响应在报错前先把 body 读进缓存。

    MCP SDK 在 ``raise_for_status()`` 后随 ``async with`` 立刻关闭响应流，
    事后从 HTTPStatusError.response 读 body 只会得到 StreamClosed；必须在
    钩子（raise 之前）缓存下来。只处理错误响应：成功的 SSE 是长流，aread
    会一直阻塞到流结束。
    """
    if response.status_code >= 400:
        try:
            await response.aread()
        except Exception:  # noqa: BLE001 - 读不到 body 也不影响状态码报错
            pass


def _format_http_status_error(exc: httpx.HTTPStatusError) -> str:
    """HTTP 非 2xx：状态码 + URL，并尽量带上响应体里的服务端原因。"""
    response = exc.response
    message = f"HTTP {response.status_code} {response.reason_phrase}：{response.request.url}"
    try:
        body = (response.text or "").strip()
    except httpx.ResponseNotRead:
        body = ""
    if body:
        # 千帆这类网关把具体原因（InvalidHTTPAuthHeader 等）塞在 JSON/plain 体里。
        detail: object = body
        try:
            data = json.loads(body)
        except ValueError:
            pass
        else:
            if isinstance(data, dict):
                detail = (
                    data.get("message")
                    or data.get("error_message")
                    or data.get("error")
                    or body
                )
        message = f"{message}；服务端返回：{str(detail)[:300]}"
    return message


def describe_connection_error(exc: BaseException) -> str:
    """把连接失败异常转成可读原因。

    MCP SDK 的传输层错误被 anyio TaskGroup 包成
    "unhandled errors in a TaskGroup (1 sub-exception)"，直接展示毫无信息量；
    这里拆到叶子异常：HTTP 错误带状态码和响应体，网络错误带类型，其余取 str。
    """
    if isinstance(exc, BaseExceptionGroup):
        # get_cancelled_exc_class 在 anyio 4 里要求当前线程跑着事件循环；
        # 离线单测（同步调用）没有循环，退回 asyncio.CancelledError。
        try:
            cancelled: tuple = (anyio.get_cancelled_exc_class(),)
        except Exception:  # noqa: BLE001 - 仅取取消异常类型，失败走回退
            cancelled = (asyncio.CancelledError,)
        leaves = [
            leaf for leaf in _flatten_exc_leaves(exc) if not isinstance(leaf, cancelled)
        ]
        if not leaves:
            return str(exc)
        # 有 HTTP 响应的错误信息最全，优先展示。
        leaves.sort(key=lambda leaf: 0 if isinstance(leaf, httpx.HTTPStatusError) else 1)
        exc = leaves[0]

    if isinstance(exc, httpx.HTTPStatusError):
        return _format_http_status_error(exc)
    if isinstance(exc, httpx.RequestError):
        return f"网络请求失败（{type(exc).__name__}）：{exc}"
    text = str(exc)
    return text or f"{type(exc).__name__}（无错误信息）"


def _secret_dict(blob: Optional[bytes]) -> Dict[str, str]:
    """解密一个密钥袋，并把值统一成 str。"""
    if not blob:
        return {}
    data = json.loads(decrypt(blob))
    return {str(key): str(value) for key, value in data.items()}


async def _enter_transport(
    stack: AsyncExitStack, server: McpServer
) -> Tuple[object, object]:
    """按传输进入连接，返回 (read_stream, write_stream)。"""
    headers = _secret_dict(server.headers_enc)

    if server.transport == TRANSPORT_STDIO:
        user_env = _secret_dict(server.env_enc)
        # SDK 语义：env 传 dict 是**完全替换**而非追加，不合并 os.environ 会
        # 丢掉 PATH，npx/node 这类命令直接「假启动」。用户变量覆盖父环境。
        env = {**os.environ, **user_env}
        return await stack.enter_async_context(
            stdio_client(
                StdioServerParameters(
                    command=server.command,
                    args=list(server.args or []),
                    env=env,
                )
            )
        )

    if server.transport == TRANSPORT_SSE:
        return await stack.enter_async_context(
            sse_client(server.url, headers=headers or None)
        )

    if server.transport == TRANSPORT_HTTP:
        # httpx client 的生命周期也交给 stack：streamable_http_client 对
        # 外部传入的 client 不负责关闭。
        # 响应钩子把非 2xx 的 body 提前缓存，否则 SDK raise 完就关流，
        # 测试报错只能拿到状态码、拿不到千帆这类网关返回的具体原因。
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                headers=headers or None,
                event_hooks={"response": [_buffer_error_response]},
            )
        )
        read, write, _session_id = await stack.enter_async_context(
            streamable_http_client(server.url, http_client=client)
        )
        return read, write

    raise ValueError(f"不支持的传输类型：{server.transport}")


async def _enter_session(
    stack: AsyncExitStack, server: McpServer
) -> ClientSession:
    read, write = await _enter_transport(stack, server)
    session = await stack.enter_async_context(
        ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(seconds=READ_TIMEOUT),
        )
    )
    # stdio_client 对进程立即退出不报错（read 流自然结束），initialize 才会
    # 暴露问题：30s 兜底 + 流关闭的 BrokenResource 错误。
    try:
        with anyio.fail_after(CONNECT_TIMEOUT):
            await session.initialize()
    except TimeoutError as exc:
        raise RuntimeError(
            f"MCP 服务「{server.name}」连接超时（{CONNECT_TIMEOUT:.0f} 秒），"
            "请检查启动命令、参数或服务地址"
        ) from exc
    except Exception as exc:
        # 传输层错误被 TaskGroup 包着，拆出真正的叶子原因（401/连接被拒等）。
        # 错误响应体已由 httpx 响应钩子 _buffer_error_response 提前缓存。
        raise RuntimeError(describe_connection_error(exc)) from exc
    return session


class McpToolBundle:
    """一次工作流运行共用的连接束：每个服务只 initialize 一次。

    同时按服务 id 记录各服务贡献的工具（:attr:`tools_by_server`）：连接束装的
    是全部挂载服务的**并集**，每个步骤实际能看见哪些工具由
    :func:`plan_step_tools` 按「工作流级 ∪ 节点级」过滤后给出。
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.tools: List[BaseTool] = []
        self.tools_by_server: Dict[int, List[BaseTool]] = {}
        self.server_names: List[str] = []

    async def __aenter__(self) -> "McpToolBundle":
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.__aexit__(*exc_info)

    async def add_server(self, server: McpServer) -> int:
        """连接并加入一个服务，返回该服务贡献的工具数。"""
        from langchain_mcp_adapters.tools import load_mcp_tools

        session = await _enter_session(self._stack, server)
        tools = await load_mcp_tools(
            session,
            server_name=server_prefix(server.name),
            tool_name_prefix=True,
        )
        # 束层面不再判跨服务重名：工具名冲突是「同一节点生效集合」的事
        # （见 plan_step_tools）——两个服务规范化后同名，只要没挂到同一个
        # 节点，就可以在一趟运行里共存、各自只连一次。
        self.tools.extend(tools)
        self.tools_by_server[server.id] = list(tools)
        self.server_names.append(server.name)
        logger.info(
            "mcp bundle added server %s (%s, %d tool(s))",
            server.name, server.transport, len(tools),
        )
        return len(tools)


def resolve_servers(
    db: Session, owner_id: int, server_ids: Sequence[int]
) -> List[McpServer]:
    """运行时解析挂载 id：去重保序 → 必须全部存在、属于本人、已启用。"""
    ids = list(dict.fromkeys(server_ids))
    if not ids:
        return []

    rows = list(
        db.scalars(
            select(McpServer).where(
                McpServer.owner_id == owner_id,
                McpServer.id.in_(ids),
            )
        ).all()
    )
    found = {row.id for row in rows}
    missing = [str(server_id) for server_id in ids if server_id not in found]
    if missing:
        raise LookupError(
            f"挂载的 MCP 服务不存在或无权访问：{', '.join(missing)}，"
            "请调整工作流的 MCP 挂载"
        )
    disabled = [row.name for row in rows if not row.enabled]
    if disabled:
        raise ValueError(f"挂载的 MCP 服务已停用：{'、'.join(disabled)}")

    rows.sort(key=lambda row: ids.index(row.id))
    return rows


async def open_bundle(servers: Sequence[McpServer]) -> McpToolBundle:
    """进入连接束并逐个加入服务；任一失败先整体关闭再抛。"""
    bundle = McpToolBundle()
    await bundle.__aenter__()
    try:
        for server in servers:
            await bundle.add_server(server)
    except Exception:
        await bundle.__aexit__(None, None, None)
        raise
    return bundle


def plan_step_tools(
    bundle: McpToolBundle,
    workflow_ids: Sequence[int],
    steps: Sequence[WorkflowStep],
) -> Dict[str, List[BaseTool]]:
    """预计算每个节点生效的工具：工作流级挂载 ∪ 节点级挂载，按服务 id 去重保序。

    连接束装的是全部节点挂载的并集；这里按步骤把不属于自己的服务剔掉。同一
    服务在两级（或一处重复填写）都挂了，只取一次。

    工具名冲突只在**同一节点的生效集合**里判：两个服务规范化后同名，只有同时
    出现在某个节点（工作流级挂载天然与谁都同屏）才算冲突，报出来让用户重命名
    服务或调整挂载；分别挂在互不相关的节点上则放行。
    """
    global_ids = list(dict.fromkeys(workflow_ids))
    plan: Dict[str, List[BaseTool]] = {}
    for step in steps:
        ids = list(dict.fromkeys(global_ids + list(step.mcp_server_ids or [])))
        tools: List[BaseTool] = []
        seen_names: set[str] = set()
        clashes: List[str] = []
        for server_id in ids:
            # 能进 plan 的 id 都已通过 resolve + open_bundle，缺映射是内部
            # 一致性问题，直接 KeyError 暴露，不静默吞掉。
            for tool in bundle.tools_by_server[server_id]:
                if tool.name in seen_names:
                    clashes.append(tool.name)
                else:
                    seen_names.add(tool.name)
                    tools.append(tool)
        if clashes:
            raise ValueError(
                f"节点 {step.key} 上 MCP 工具名冲突："
                f"{'、'.join(sorted(set(clashes)))}，"
                "请重命名对应的 MCP 服务，或调整该节点的挂载"
            )
        plan[step.key] = tools
    return plan


async def test_connection(server: McpServer) -> Tuple[bool, str, int]:
    """连通性探测：成功返回工具数，失败返回原因。"""
    try:
        async with McpToolBundle() as bundle:
            count = await bundle.add_server(server)
    except Exception as exc:  # noqa: BLE001 - 测试接口就是要把原因带回去
        message = describe_connection_error(exc)
        logger.info("mcp test connection failed for %s: %s", server.name, message)
        return False, message[:1024], 0
    return True, f"连接成功，发现 {count} 个工具", count
