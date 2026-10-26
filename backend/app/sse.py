"""SSE 响应的公共部分：事件帧的格式与禁用缓冲的响应头。

对话回答、工作流运行进度这些流式端点都要用到同一套。放在这里而不是各路由自
带一份：``X-Accel-Buffering: no`` 漏写一处，表现就是那条流在 Nginx 后面整段
攒完才吐出来——本地开发看不出问题。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator, TypeVar, cast

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

# 队列里的结束哨兵：不能用 None 之类，下层帧本身可能是任意值。
_END = object()


async def _drive(source: AsyncIterator[Any], queue: "asyncio.Queue[Any]") -> None:
    """生产者：在**唯一一个 task** 里迭代下层生成器，帧/异常/结束都丢进队列。

    消费者每一步都换 ``ensure_future`` 去驱动 ``__anext__`` 是错的：下层在某次
    step 进入、跨多次 yield 才退出的 anyio cancel scope（MCP ``ClientSession``
    的 task group、asyncssh 连接等）会绑定**进入它的那个 task**，在另一个 task
    退出直接抛 "Attempted to exit cancel scope in a different task"。由这个固定
    的生产者 task 持有下层生成器，scope 的进出就永远在同一个 task 里。
    """
    try:
        async for item in source:
            await queue.put(item)
    except BaseException as exc:  # noqa: BLE001 - 原样透传，包括 CancelledError
        # 生产者被取消时，这里的 put 会立刻再吃一个 CancelledError；消费者此时
        # 已在收尾、不再读队列，放掉即可。
        with contextlib.suppress(BaseException):
            await queue.put(exc)
    else:
        with contextlib.suppress(BaseException):
            await queue.put(_END)


async def with_heartbeat(
    source: AsyncIterator[SseChunk],
    interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    heartbeat: SseChunk = cast(SseChunk, SSE_HEARTBEAT),
) -> AsyncIterator[SseChunk]:
    """转发下层 SSE 帧，产出停滞超过 ``interval`` 时插入一帧心跳。

    模型长思考时下层生成器几十秒不出一个字节，链路上每一段空闲读超时
    （Nginx proxy_read_timeout 等）都可能在这时掐断连接，而客户端只会看到
    永久转圈。心跳帧一到，各方的空闲计时全部重置，连接「活着」就有了证据。

    下层生成器跑在一个独立的生产者 task 里（见 :func:`_drive`）：心跳超时只
    作用在 ``queue.get`` 上，取消不会打进正在等上游响应的下层，同时下层内部
    跨多帧打开的 cancel scope 也不会被切换 task 踩爆。
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    producer = asyncio.ensure_future(_drive(source, queue))
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield heartbeat
                continue
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield cast(SseChunk, item)
    finally:
        # 本生成器被取消（客户端断开）时取消生产者：CancelledError 在生产者
        # task 里抛进下层，它持有的连接/子进程随之在同一个 task 里收尾，
        # 否则以「pending task destroyed」或跨 task 退出 scope 收场。
        producer.cancel()
        with contextlib.suppress(BaseException):
            await producer


async def with_byte_heartbeat(
    source: AsyncIterator[bytes],
    interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    heartbeat: bytes = SSE_HEARTBEAT_BYTES,
) -> AsyncIterator[bytes]:
    """字节透传流专用：心跳只插在 SSE 帧边界（``\\n\\n`` 之后）。

    透传的 chunk 可能落在任意 TCP 边界，上一帧写到一半时塞心跳会把事件拼坏；
    而生成停顿几乎总发生在事件之间（模型思考时下一帧还没产生），错过边界时
    就再等一个 interval，不会硬插。生产者模型与 :func:`with_heartbeat` 相同。
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    producer = asyncio.ensure_future(_drive(source, queue))
    # 已转发数据的末尾两字节：判断当前位置是否恰在帧边界只需要这些。
    tail = b""
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                if tail.endswith(b"\n\n"):
                    yield heartbeat
                continue
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            tail = (tail + item)[-2:]
            yield item
    finally:
        producer.cancel()
        with contextlib.suppress(BaseException):
            await producer


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx buffers proxied responses by default, which holds the whole
    # stream until completion and defeats the point of streaming.
    "X-Accel-Buffering": "no",
}
