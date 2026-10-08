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
from app.models.entities import McpServer
from app.services.mcp_servers import (
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
        client = await stack.enter_async_context(
            httpx.AsyncClient(headers=headers or None)
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
    return session


class McpToolBundle:
    """一次工作流运行共用的连接束：每个服务只 initialize 一次。"""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.tools: List[BaseTool] = []
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
        # 前缀按服务名隔离了跨服务重名；两个服务规范化后同名（如 "A B" 与
        # "A_B"）仍会撞，当场报错让用户重命名服务。
        existing = {tool.name for tool in self.tools}
        clashes = [tool.name for tool in tools if tool.name in existing]
        if clashes:
            raise ValueError(
                f"MCP 工具名冲突：{'、'.join(clashes)}，请重命名对应的 MCP 服务"
            )
        self.tools.extend(tools)
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


async def test_connection(server: McpServer) -> Tuple[bool, str, int]:
    """连通性探测：成功返回工具数，失败返回原因。"""
    try:
        async with McpToolBundle() as bundle:
            count = await bundle.add_server(server)
    except Exception as exc:  # noqa: BLE001 - 测试接口就是要把原因带回去
        logger.info("mcp test connection failed for %s: %s", server.name, exc)
        return False, str(exc)[:1024], 0
    return True, f"连接成功，发现 {count} 个工具", count
