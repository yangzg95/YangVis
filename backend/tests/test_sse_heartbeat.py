"""心跳包装器的契约：补帧、保序、异常透传，以及下层始终在同一个 task 里跑。

最后这一条是线上事故的回归：旧实现每一步都 ``ensure_future(source.__anext__())``，
下层生成器被一串临时 task 轮流驱动。下层若在某次 step 进入了跨多帧才退出的
anyio cancel scope（MCP ``ClientSession`` 的 task group 就是这个形态），退出时
scope 所在的 task 已经换了，anyio 直接抛
"Attempted to exit cancel scope in a different task than it was entered in"，
在 SSE 末尾落成一帧「重试失败」。
"""
from __future__ import annotations

import asyncio
from typing import Any, List

import anyio
import pytest

from app.sse import (
    SSE_HEARTBEAT,
    SSE_HEARTBEAT_BYTES,
    with_byte_heartbeat,
    with_heartbeat,
)


def collect(agen, close_early: bool = False) -> List[Any]:
    """同步跑一个异步生成器；close_early 时只取一帧就 aclose。"""

    async def go() -> List[Any]:
        items: List[Any] = []
        iterator = agen.__aiter__()
        if close_early:
            await iterator.__anext__()
            await iterator.aclose()
            return items
        async for item in iterator:
            items.append(item)
        return items

    return asyncio.run(go())


def test_source_is_always_driven_by_one_single_task() -> None:
    """下层每个 step 都必须落在同一个生产者 task 上，哪怕中途插了多次心跳。"""
    tasks: List[asyncio.Task] = []

    async def source():
        tasks.append(asyncio.current_task())
        yield "a"
        tasks.append(asyncio.current_task())
        await asyncio.sleep(0.04)  # 跨过若干个 5ms 心跳窗口
        tasks.append(asyncio.current_task())
        yield "b"
        tasks.append(asyncio.current_task())

    frames = collect(with_heartbeat(source(), interval=0.005))

    assert len({id(t) for t in tasks}) == 1, "下层生成器被多个 task 轮流驱动了"
    assert [f for f in frames if f is not SSE_HEARTBEAT] == ["a", "b"]
    assert SSE_HEARTBEAT in frames, "40ms 的空档里应当插进过心跳"


def test_cancel_scope_open_across_frames_closes_cleanly() -> None:
    """MCP 会话形态：scope 在首帧前进入、末帧后退出，中间夹着心跳。"""

    async def source():
        # 与 anyio task group 同样有 owner-task 校验；挂在整个生成器生命周期上。
        with anyio.CancelScope():
            yield "step"
            await asyncio.sleep(0.03)
            yield "done"

    frames = collect(with_heartbeat(source(), interval=0.005))

    assert [f for f in frames if f is not SSE_HEARTBEAT] == ["step", "done"]


def test_scope_is_exited_in_producer_task_when_consumer_abandons() -> None:
    """客户端断开：取消在生产者 task 内抛进下层，scope 在同一个 task 里退出。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def source():
        with anyio.CancelScope():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield "never"

    # 第一帧是心跳（下层一直挂着），随后断开，不应抛跨 task 退出 scope。
    collect(with_heartbeat(source(), interval=0.005), close_early=True)

    assert started.is_set() and cancelled.is_set()


def test_source_exception_is_reraised_after_frames() -> None:
    """下层炸了要原样冒给路由层（由它落成 error 帧），不能被心跳吞掉。"""

    async def source():
        yield "ok"
        raise ValueError("boom")

    async def go():
        with pytest.raises(ValueError, match="boom"):
            async for _ in with_heartbeat(source(), interval=0.01):
                pass

    asyncio.run(go())


def test_heartbeat_does_not_overtake_slow_frame() -> None:
    """心跳超时只等队列，绝不能把取消打进正在跑的那一帧。"""

    async def source():
        yield "a"
        await asyncio.sleep(0.05)
        yield "b"

    frames = collect(with_heartbeat(source(), interval=0.01))

    assert [f for f in frames if f is not SSE_HEARTBEAT] == ["a", "b"]
    assert SSE_HEARTBEAT in frames


def test_byte_heartbeat_only_fires_on_frame_boundary() -> None:
    """字节流：非 ``\\n\\n`` 边界上的停顿不许插心跳，顺序也不能乱。"""

    async def source():
        yield b"event: a\n\n"
        await asyncio.sleep(0.03)  # 此时 tail 是边界 -> 可以插
        yield b"partial"          # 之后 tail 不是边界 -> 不许插

    frames = collect(with_byte_heartbeat(source(), interval=0.01))

    data = [f for f in frames if f is not SSE_HEARTBEAT_BYTES]
    assert data == [b"event: a\n\n", b"partial"]
    beat_positions = [i for i, f in enumerate(frames) if f is SSE_HEARTBEAT_BYTES]
    assert beat_positions, "边界上的停顿应当插进过心跳"
    partial_position = frames.index(b"partial")
    assert max(beat_positions) < partial_position, "半个帧之后插心跳会把事件拼坏"
