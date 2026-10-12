"""请求结束后还要继续跑的 asyncio 任务。

SSE 流用不了 FastAPI 的 ``BackgroundTasks``——响应到那儿已经结束了，后台收尾只能
自己挂任务。挂法只有一种正确的：见 :func:`fire_and_forget`。
"""
from __future__ import annotations

import asyncio

# asyncio.create_task 创建的任务只被事件循环弱引用，不放一份强引用，任务可能
# 在跑完之前就被 GC 回收。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def fire_and_forget(coro) -> None:
    """在请求结束后继续跑一个协程。

    任务体内的异常必须自己兜住——这里的回调只负责回收引用。
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
