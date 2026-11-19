"""App 通知 SSE 的进程内连接表。

只负责实时加速，数据库里的 ``app_message`` 才是事实源。慢连接会被关闭，
客户端用最后收到的消息 ID 重连补拉，避免静默丢帧后还继续推进游标。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import cast

from app.models.schemas import AppMessageItem

_CLOSE = object()
_QUEUE_SIZE = 128


class AppStreamConnection:
    def __init__(self, registry: "AppStreamRegistry", owner_id: int) -> None:
        self._registry = registry
        self.owner_id = owner_id
        self.queue: asyncio.Queue[AppMessageItem | object] = asyncio.Queue(
            maxsize=_QUEUE_SIZE
        )
        self.closed = False

    def publish(self, message: AppMessageItem) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.close()

    async def next_message(self) -> AppMessageItem | None:
        item = await self.queue.get()
        if item is _CLOSE:
            return None
        return cast(AppMessageItem, item)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._registry.unregister(self)
        # 队列满时丢掉一条实时帧并放入关闭哨兵；被丢的帧会由重连补拉恢复。
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(_CLOSE)


class AppStreamRegistry:
    """仅在所属 worker 的事件循环里访问，不跨线程、不跨进程。"""

    def __init__(self) -> None:
        self._connections: dict[int, set[AppStreamConnection]] = defaultdict(set)

    def register(self, owner_id: int) -> AppStreamConnection:
        connection = AppStreamConnection(self, owner_id)
        self._connections[owner_id].add(connection)
        return connection

    def unregister(self, connection: AppStreamConnection) -> None:
        connections = self._connections.get(connection.owner_id)
        if connections is None:
            return
        connections.discard(connection)
        if not connections:
            self._connections.pop(connection.owner_id, None)

    def publish(self, owner_id: int, message: AppMessageItem) -> None:
        for connection in tuple(self._connections.get(owner_id, ())):
            connection.publish(message)

    def close_all(self) -> None:
        for connections in tuple(self._connections.values()):
            for connection in tuple(connections):
                connection.close()
        self._connections.clear()

    def connection_count(self, owner_id: int | None = None) -> int:
        if owner_id is not None:
            return len(self._connections.get(owner_id, ()))
        return sum(len(connections) for connections in self._connections.values())


app_stream = AppStreamRegistry()
