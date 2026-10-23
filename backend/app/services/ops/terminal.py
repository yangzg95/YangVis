"""脱机可存活的 PTY 终端会话。

浏览器与 PTY 的生命周期在这里被拆开：WebSocket 只是会话的一块「屏幕」，
关掉页面、网络抖动、空闲掉线都不杀 SSH 连接和远端 shell。会话在最后一个
浏览器断开后保留 ``OPS_TERMINAL_TTL`` 秒，期间远端输出继续进缓冲；新连接
凭 ``(user_id, server_id)`` 接回同一会话，先回放缓冲里的近期输出，再接着
实时流往下走——xterm 重放字节流即可重建屏幕，和 tmux attach 一个思路，
只是不依赖远端装了 tmux。

约束：
- 会话按人隔离（台账虽共用，shell 与审计仍归个人），同一时刻一块屏幕，
  新连接顶掉旧连接（``WS_TAKENOVER``）。
- 缓冲只保留最近约 20 万字符，更早的回滚找不回来；全屏程序（vim/top）
  重连后靠客户端的 resize 触发一次重绘，若进缓冲时切换序列已被截掉，
  画面顶部可能有少量乱码——这是无远端协同的务实上限。
- 进程只在单 worker 内存里，后端重启即全部丢失。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from typing import Awaitable, Callable, List, Optional, Tuple

import asyncssh
from fastapi import WebSocket

from app.config import get_settings
from app.services.chat_ops import Auditor

logger = logging.getLogger("yangvis.ops.terminal")

# 自定义 WS 关闭码（4000-4099 为私有段）。
WS_TAKENOVER = 4001  # 同一会话在另一个页面打开，旧连接让位
WS_SESSION_ENDED = 4002  # 远端 shell 退出 / SSH 已断，会话没了，重连是新会话

# 服务端 → 浏览器的控制帧前缀。裸 PTY 输出理论上也可能以 '{' 开头，
# 0x1e（记录分隔符）在真实终端输出里几乎不会出现，用它消歧。
CONTROL_MARK = "\x1e"

# 回放缓冲上限：约 5000 行终端的量级，按 4096 字节的读取块整段丢弃。
REPLAY_MAX_CHARS = 200_000


def control(payload: dict) -> str:
    return CONTROL_MARK + json.dumps(payload, ensure_ascii=False)


# 终端输出里出现这类提示，说明下一行键盘输入是密码而不是命令——不记。
# 真实提示形态不一（"[sudo] password for ops: "、"Enter passphrase for key ...: "、
# "Password: "），关键词不一定贴着结尾，所以允许关键词后隔一段再以冒号收尾。
_PASSWORD_PROMPT = re.compile(
    r"(password|passphrase|密码|口令)[^\n\r]{0,40}[:：]\s*$", re.IGNORECASE
)


class CommandLineTracker:
    """从 PTY 输入流里尽力重建用户提交的命令行。

    真 PTY 不做命令过滤（那是用户自己的手），但「他敲过什么」要留痕。难处在
    PTY 是原始字节流，带着行编辑的全部复杂性：退格、Ctrl+U、转义序列、上下
    翻历史。完美重建需要一台完整的终端模拟器，那是堡垒机级别的活；这里走
    务实路线——可打印字符入缓冲，常见编辑键尽力处理，回车提交。翻历史翻出
    来的命令会重建不全，接受这个偏差：审计要的是「看得到大概干过什么」。

    一条硬规则：输出侧刚问过密码（sudo、mysql -p 之类），下一行输入不记——
    把密码写进审计库比漏记一条命令糟得多。
    """

    def __init__(self) -> None:
        self._buf: List[str] = []
        self._escape = False  # 正在吞一个 ANSI 转义序列
        self._output_tail = ""  # 终端输出的滚动尾部，用来认密码提示
        self._muted = False  # 下一行是密码输入，跳过

    def feed_output(self, chunk: str) -> None:
        """SSH → 浏览器方向的输出，只留一小段尾巴认提示符。"""
        self._output_tail = (self._output_tail + chunk)[-200:]
        if _PASSWORD_PROMPT.search(self._output_tail):
            self._muted = True

    def feed(self, text: str) -> List[str]:
        """喂入一段键盘输入，返回本次提交的命令行（粘贴多行时可能多条）。"""
        submitted: List[str] = []
        for ch in text:
            if self._escape:
                # CSI/SS3 序列以字母或 ~ 收尾，整个吞掉不进缓冲。
                if ch.isalpha() or ch == "~":
                    self._escape = False
                continue
            if ch == "\x1b":
                self._escape = True
            elif ch in ("\r", "\n"):
                line = "".join(self._buf).strip()
                self._buf.clear()
                if self._muted:
                    # 密码行：跳过并恢复记录。
                    self._muted = False
                elif line:
                    submitted.append(line)
            elif ch in ("\x7f", "\x08"):  # 退格
                if self._buf:
                    self._buf.pop()
            elif ch == "\x15":  # Ctrl+U 清行
                self._buf.clear()
            elif ch == "\x17":  # Ctrl+W 删一个词
                while self._buf and self._buf[-1] == " ":
                    self._buf.pop()
                while self._buf and self._buf[-1] != " ":
                    self._buf.pop()
            elif ch == "\x03":  # Ctrl+C：当前行作废
                self._buf.clear()
            elif ch >= " ":  # 可打印字符（含中文等多字节字符）
                self._buf.append(ch)
            # \t（补全请求，不产生字符）与其余控制字符忽略。
        return submitted


def is_cwd_probe(command: str) -> bool:
    """前端右键「在文件浏览器打开」的 cwd 探测命令。

    那条 printf 在终端屏幕上会自我擦除，审计里也不该留痕——否则每右键一次
    就刷一条无意义的 printf。标记串（OSC 697 格式 + $PWD）足够独特。
    """
    return "697;%s" in command and "$PWD" in command


async def record_manual_command(user_id: int, server_id: int, server_name: str, command: str) -> None:
    """把终端里手敲的一行命令写进审计表。

    审计自己开短会话并放到工作线程跑（长连接不该占连接池，也不该卡事件
    循环）。verdict 记 ``manual``：人工输入没有「白名单判定」这回事，success
    也只是「已送达」，退出码在这条通道上拿不到。
    """
    await Auditor(user_id, "server").record(
        target_id=server_id,
        target_name=server_name,
        command=command,
        verdict="manual",
        success=True,
        actor="user",
    )


class TerminalSession:
    """一条 SSH 连接 + 一个 PTY 进程，以及围着它们的缓冲、泵和收尸钟。

    输出通道是「每块屏幕一个队列 + 单写者」：泵只往当前队列里放，attach
    直接发握手与回放，实时帧在回放之后由写者从同一队列取出——回放与实时
    因此严格有序，不会互相穿插。
    """

    def __init__(
        self,
        registry: "TerminalRegistry",
        *,
        user_id: int,
        server_id: int,
        audit_target: Tuple[int, str],
        conn: asyncssh.SSHClientConnection,
        process: asyncssh.SSHClientProcess,
        cols: int,
        rows: int,
    ) -> None:
        self.registry = registry
        self.user_id = user_id
        self.server_id = server_id
        self.audit_target = audit_target
        self.conn = conn
        self.process = process
        self.cols = cols
        self.rows = rows

        self.client: Optional[WebSocket] = None
        self.closed = False
        self.end_reason = ""

        self.tracker = CommandLineTracker()
        # 近期输出的回放缓冲。chunk 是 asyncssh 解码后的 str（utf-8/replace）。
        self._chunks: deque[str] = deque()
        self._buffered = 0

        self._queue: Optional[asyncio.Queue[Optional[str]]] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._reaper_task: Optional[asyncio.Task] = None

    # -- 生命周期 -------------------------------------------------------------

    def start(self) -> None:
        self._pump_task = asyncio.create_task(self._pump())

    async def attach(self, websocket: WebSocket, *, resumed: bool) -> None:
        """把一块新屏幕挂上来；旧屏幕（如果有）被顶掉。"""
        old = self.client
        old_writer = self._writer_task
        # 先摘掉旧屏幕：泵立刻停止给它投递，也避免新旧两个写者并发写不同 ws。
        self.client = None
        self._queue = None
        self._writer_task = None
        if old_writer is not None:
            old_writer.cancel()
        self._cancel_reaper()
        if old is not None and old is not websocket:
            asyncio.create_task(self._kick_old_client(old))

        # 关键时序：先同步拍快照、再挂队列，两步之间不能有 await——否则泵会在
        # 期间把同一块输出同时放进快照和实时队列，屏幕上就重了一遍。
        # 新会话也回放：shell 的首批输出（motd/提示符）可能抢在 attach 之前到。
        queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        snapshot = list(self._chunks)
        self.client = websocket
        self._queue = queue

        try:
            await websocket.send_text(control({"type": "hello", "resume": resumed}))
            for chunk in snapshot:
                await websocket.send_text(chunk)
            await websocket.send_text(control({"type": "live"}))
        except Exception:
            # 浏览器在回放途中就没了：摘掉这块屏幕并拉起收尸钟，别让会话裸奔。
            self.detach(websocket)
            raise

        self._writer_task = asyncio.create_task(self._writer(websocket, queue))

    async def _kick_old_client(self, old: WebSocket) -> None:
        try:
            await old.send_text(control({"type": "takenover"}))
            await old.close(code=WS_TAKENOVER, reason="终端已在另一个页面打开")
        except Exception:
            pass

    def detach(self, websocket: WebSocket) -> None:
        """浏览器离开。只在它还是当前屏幕时才算数（让位旧连接的收尾不算）。"""
        if self.client is not websocket or self.closed:
            return
        self.client = None
        self._queue = None
        if self._writer_task is not None:
            self._writer_task.cancel()
            self._writer_task = None
        self._arm_reaper()

    async def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        try:
            self.process.change_terminal_size(cols, rows)
        except Exception as exc:  # pragma: no cover - 依赖真实 SSH 服务端
            logger.debug(
                "terminal resize ignored: user %s, server %s: %s",
                self.user_id,
                self.server_id,
                exc,
            )

    async def handle_input(self, text: str) -> None:
        for command in self.tracker.feed(text):
            if not is_cwd_probe(command):
                await record_manual_command(
                    self.user_id, self.audit_target[0], self.audit_target[1], command
                )
        self.process.stdin.write(text)

    # -- 内部 -----------------------------------------------------------------

    def _buffer(self, chunk: str) -> None:
        self._chunks.append(chunk)
        self._buffered += len(chunk)
        while self._buffered > REPLAY_MAX_CHARS and len(self._chunks) > 1:
            self._buffered -= len(self._chunks.popleft())

    async def _writer(self, websocket: WebSocket, queue: "asyncio.Queue[Optional[str]]") -> None:
        """队列的唯一消费者：回放之后的所有帧都经它有序发出。"""
        while True:
            item = await queue.get()
            if item is None:
                # 终结控制帧已排在队列尾部，发完它再关连接。
                try:
                    await websocket.close(
                        code=WS_SESSION_ENDED, reason=self.end_reason or "会话已结束"
                    )
                except Exception:
                    pass
                return
            try:
                await websocket.send_text(item)
            except Exception:
                # 浏览器已断；receive 侧的收尾会 detach 并拉起收尸钟。
                return

    async def _pump(self) -> None:
        """SSH → 回放缓冲 + 当前屏幕；没人看时只入缓冲，会话照样活着。"""
        try:
            while True:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                self.tracker.feed_output(chunk)
                self._buffer(chunk)
                queue = self._queue
                if queue is not None:
                    queue.put_nowait(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "terminal pump task died: user %s, server %s: %s",
                self.user_id,
                self.server_id,
                exc,
            )
        finally:
            await self._destroy(reason="远端 shell 已退出或连接已中断")

    def _arm_reaper(self) -> None:
        self._cancel_reaper()
        ttl = get_settings().OPS_TERMINAL_TTL
        if ttl > 0:
            self._reaper_task = asyncio.create_task(self._reap(ttl))

    def _cancel_reaper(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None

    async def _reap(self, ttl: int) -> None:
        await asyncio.sleep(ttl)
        if self.client is None and not self.closed:
            logger.info(
                "terminal session expired after %ss idle: user %s, server %s",
                ttl,
                self.user_id,
                self.server_id,
            )
            await self._destroy(reason="会话空闲超时已回收", notify=False)

    async def _destroy(self, *, reason: str, notify: bool = True) -> None:
        if self.closed:
            return
        self.closed = True
        self.end_reason = reason
        self.registry.drop((self.user_id, self.server_id), self)
        self._cancel_reaper()

        queue = self._queue
        if queue is not None and notify:
            # 排在输出最后一帧之后：浏览器先看到结尾，再收到结束帧与关闭。
            queue.put_nowait(control({"type": "ended", "reason": reason}))
            queue.put_nowait(None)
        try:
            self.conn.close()
        except Exception:
            pass
        logger.info("terminal session closed: user %s, server %s", self.user_id, self.server_id)


# 新建会话所需的工厂：完成 SSH 握手与 PTY 创建，返回
# (conn, process, (server_id, server_name))。抛 LookupError 表示机器没了，
# 抛 OpsConnectError 表示连不上，由路由层翻译成 WS 关闭。
CreateSession = Callable[
    [],
    Awaitable[Tuple[asyncssh.SSHClientConnection, asyncssh.SSHClientProcess, Tuple[int, str]]],
]


class TerminalRegistry:
    """内存中的活动会话表，按 (用户, 服务器) 索引。"""

    def __init__(self) -> None:
        self._sessions: dict[Tuple[int, int], TerminalSession] = {}
        self._locks: dict[Tuple[int, int], asyncio.Lock] = {}

    async def attach_or_create(
        self,
        *,
        user_id: int,
        server_id: int,
        websocket: WebSocket,
        cols: int,
        rows: int,
        create: CreateSession,
    ) -> Tuple[TerminalSession, bool]:
        """续接已有会话，或在 per-key 锁内跑工厂建一个新会话。

        同一把锁保证「断线瞬间自动重连」不会并发建出两条 SSH。
        返回 ``(session, resumed)``。
        """
        key = (user_id, server_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            session = self._sessions.get(key)
            if session is not None and not session.closed:
                await session.attach(websocket, resumed=True)
                logger.info("terminal tunnel resumed: user %s, server %s", user_id, server_id)
                return session, True

            conn, process, audit_target = await create()
            session = TerminalSession(
                self,
                user_id=user_id,
                server_id=server_id,
                audit_target=audit_target,
                conn=conn,
                process=process,
                cols=cols,
                rows=rows,
            )
            self._sessions[key] = session
            session.start()
            await session.attach(websocket, resumed=False)
            logger.info("terminal tunnel opened: user %s, server %s", user_id, server_id)
            return session, False

    def drop(self, key: Tuple[int, int], session: TerminalSession) -> None:
        # 只摘自己：理论上不会有第二个会话占同一个 key，稳妥起见比对身份。
        if self._sessions.get(key) is session:
            self._sessions.pop(key, None)


# 模块级单表（单 worker 部署）。
terminal_sessions = TerminalRegistry()
