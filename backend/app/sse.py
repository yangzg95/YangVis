"""SSE 响应的公共部分：事件帧的格式与禁用缓冲的响应头。

对话回答、工作流运行进度这些流式端点都要用到同一套。放在这里而不是各路由自
带一份：``X-Accel-Buffering: no`` 漏写一处，表现就是那条流在 Nginx 后面整段
攒完才吐出来——本地开发看不出问题。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import AsyncIterator, TypeVar, cast

# SSE 注释帧：EventSource 语义下客户端必须忽略，正好用来当心跳。
SSE_HEARTBEAT = ": ping\n\n"
# bytes 版：原样透传上游字节的端点（AI 网关 relay）不能混入 str 帧。
SSE_HEARTBEAT_BYTES = b": ping\n\n"

# 15s 远小于链路上最短的空闲读超时（网关 proxy_read_timeout 300s），
# 给抖动留足余量，又不至于让用户等太久才发现断了。
DEFAULT_HEARTBEAT_INTERVAL = 15.0


def sse_event(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SseChunk = TypeVar("SseChunk", str, bytes)


async def with_heartbeat(
    source: AsyncIterator[SseChunk],
    interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    heartbeat: SseChunk = cast(SseChunk, SSE_HEARTBEAT),
) -> AsyncIterator[SseChunk]:
    """转发下层 SSE 帧，产出停滞超过 ``interval`` 时插入一帧心跳。

    模型长思考时下层生成器几十秒不出一个字节，链路上每一段空闲读超时
    （Nginx proxy_read_timeout 等）都可能在这时掐断连接，而客户端只会看到
    永久转圈。心跳帧一到，各方的空闲计时全部重置，连接「活着」就有了证据。

    关键实现点：``wait_for`` 超时时取消的只是 ``shield`` 外壳，下层真正的
    ``__anext__`` 任务不动——直接 wait_for 会把取消打进正在等待上游响应的
    生成器，等于自己把回答掐了。
    """
    pending: asyncio.Future | None = None
    try:
        while True:
            pending = asyncio.ensure_future(source.__anext__())
            while True:
                try:
                    value = await asyncio.wait_for(asyncio.shield(pending), timeout=interval)
                except asyncio.TimeoutError:
                    yield heartbeat
                    continue
                break
            yield value
    except StopAsyncIteration:
        return
    finally:
        # 本生成器被取消（客户端断开）时，挂起的下层任务要收掉，
        # 否则它会以「pending task destroyed」告警收场。
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending


async def with_byte_heartbeat(
    source: AsyncIterator[bytes],
    interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    heartbeat: bytes = SSE_HEARTBEAT_BYTES,
) -> AsyncIterator[bytes]:
    """字节透传流专用：心跳只插在 SSE 帧边界（``\\n\\n`` 之后）。

    透传的 chunk 可能落在任意 TCP 边界，上一帧写到一半时塞心跳会把事件拼坏；
    而生成停顿几乎总发生在事件之间（模型思考时下一帧还没产生），错过边界时
    就再等一个 interval，不会硬插。
    """
    pending: asyncio.Future | None = None
    # 已转发数据的末尾两字节：判断当前位置是否恰在帧边界只需要这些。
    tail = b""
    try:
        while True:
            pending = asyncio.ensure_future(source.__anext__())
            while True:
                try:
                    chunk = await asyncio.wait_for(asyncio.shield(pending), timeout=interval)
                except asyncio.TimeoutError:
                    if tail.endswith(b"\n\n"):
                        yield heartbeat
                    continue
                break
            tail = (tail + chunk)[-2:]
            yield chunk
    except StopAsyncIteration:
        return
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx buffers proxied responses by default, which holds the whole
    # stream until completion and defeats the point of streaming.
    "X-Accel-Buffering": "no",
}
