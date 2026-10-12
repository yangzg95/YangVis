"""``app/background.py``：请求结束后继续跑的那个协程。

asyncio 对任务只持弱引用，没人 await 的分离任务可能在跑完之前就被回收——这个模块
存在的唯一理由就是那份强引用，所以用例也只钉这一件事：进集合、跑完出集合。
"""
from __future__ import annotations

import asyncio

from app.background import _BACKGROUND_TASKS, fire_and_forget

wait = asyncio.run


def test_fire_and_forget_holds_the_task_until_it_finishes():
    """asyncio 只弱引用任务：不留强引用，协程可能在跑完前就被 GC 掉。"""
    ran: list = []

    async def body():
        ran.append("ran")

    async def main():
        before = len(_BACKGROUND_TASKS)
        fire_and_forget(body())
        assert len(_BACKGROUND_TASKS) == before + 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert ran == ["ran"]
        assert len(_BACKGROUND_TASKS) == before

    wait(main())
