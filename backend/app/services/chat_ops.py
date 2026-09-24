"""运维工具箱：主对话与嵌入运维问答共用的一套工具。

两种挂载形态，同一份实现：

- 自由模式（主对话里开了 use_ops 的智能体）：目标由模型从用户自己的台账里挑，
  id 作为工具参数暴露，但归属校验仍在闭包里做——模型能决定查哪台，够不到
  别人的机器。
- 绑定模式（终端页 / 数据库页嵌入的运维问答）：会话创建时就绑死了一个目标
  （``chat_conversation.ops_target_*``），工具签名里根本没有 id 参数。

安全边界三层：

1. 分类。每条命令先过 ``ops_safety``，只读的直接执行，危险的直接挡，其余
   落进「需要确认」。
2. 确认。写操作不由模型执行，只能由它 *提议*：propose_command 落一行
   ``ops_pending_action`` 就返回，本轮回答正常结束；用户点确认后由 confirm
   端点认领（CAS）并执行——确认与执行在同一次 HTTP 请求里，不依赖任何
   进程内状态（这条性质是两阶段落库换来的，旧 WebSocket 方案做不到）。
3. 留痕。跑过的、被拒的、被挡下的，全部写进 ``ops_audit_log``。

SSH 连接随一次回答复用、回答结束即关（``aclose``）：一次回答可能只跑一两条
命令，长连接占着不如随用随建。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import OpsAuditLog, OpsDatabase, OpsPendingAction, OpsServer
from app.models.schemas import DatabaseType
from app.services import ops_database as db_ops
from app.services import ops_server as server_ops
from app.services.ops_actions import finish_execution
from app.services.ops_safety import classify_command

logger = logging.getLogger("yangvis.chat_ops")

settings = get_settings()

# 喂给模型的输出上限。命令输出可能极长（journalctl 一翻几千行），原样回灌会把
# 上下文窗口撑爆；模型要细看某一段时可以自己加 tail/grep 再查。
OUTPUT_LIMIT = 4000

TARGET_SERVER = "server"
TARGET_DATABASE = "database"


class Auditor:
    """把每一次工具执行写进审计表。"""

    def __init__(self, db: Session, owner_id: int, target_type: str) -> None:
        self._db = db
        self._owner_id = owner_id
        self._target_type = target_type

    def record(
        self,
        *,
        target_id: int,
        target_name: Optional[str],
        command: str,
        verdict: str,
        success: bool,
        error: Optional[str] = None,
        actor: str = "ai",
        pending_action_id: Optional[int] = None,
    ) -> None:
        try:
            self._db.add(
                OpsAuditLog(
                    owner_id=self._owner_id,
                    target_type=self._target_type,
                    target_id=target_id,
                    target_name=target_name,
                    actor=actor,
                    command=command[:8000],
                    verdict=verdict,
                    success=success,
                    error=(error or None) and error[:512],
                    pending_action_id=pending_action_id,
                )
            )
            self._db.commit()
        except Exception:  # pragma: no cover - 审计失败不该拖垮会话
            self._db.rollback()
            logger.exception("failed to write an audit record")


def render_result(result) -> str:
    """把查询结果渲染成模型好读的纯文本表格。"""
    if not result.columns:
        return result.text or "(无输出)"

    header = " | ".join(result.columns)
    body = "\n".join(" | ".join("" if v is None else str(v) for v in row) for row in result.rows)
    tail = f"\n（已截断，仅显示前 {len(result.rows)} 行）" if result.truncated else ""
    return f"{header}\n{'-' * len(header)}\n{body}{tail}\n共 {result.row_count} 行，耗时 {result.elapsed_ms}ms"


@dataclass(frozen=True)
class OpsTarget:
    """绑定模式会话锚定的运维目标。自由模式为 None。"""

    type: str  # TARGET_SERVER | TARGET_DATABASE
    id: int


@dataclass(frozen=True)
class ConfirmedExecResult:
    """一条已确认命令的执行结果，喂给 confirm 流的 exec 事件与续答上下文。"""

    success: bool
    exit_status: Optional[int]
    output: str
    elapsed_ms: int


class ChatOpsToolbox:
    """一次回答期间的运维工具集合，生命周期与一次 stream_answer 一致。

    ``conversation_id`` 是 propose_command 落库的要件：pending 行必须知道自己
    属于哪个会话，confirm 端点才能顺着它找到续答的上下文。
    """

    def __init__(
        self,
        db: Session,
        owner_id: int,
        *,
        target: Optional[OpsTarget] = None,
        conversation_id: Optional[int] = None,
    ) -> None:
        self._db = db
        self._owner_id = owner_id
        self._target = target
        self._conversation_id = conversation_id
        self._servers = server_ops.OpsServerService(db, owner_id)
        self._databases = db_ops.OpsDatabaseService(db, owner_id)
        self._server_audit = Auditor(db, owner_id, TARGET_SERVER)
        self._db_audit = Auditor(db, owner_id, TARGET_DATABASE)
        self._conns: Dict[int, Any] = {}
        self._target_server_cache: Optional[OpsServer] = None
        self._target_database_cache: Optional[OpsDatabase] = None
        # 本轮回答里新落库的待确认项，按落库顺序排列；stream_answer 据此发
        # confirm 事件、在回答落库后回填 message_id。
        self.pending_proposals: List[OpsPendingAction] = []
        # 本轮回答里实际执行过的只读服务器命令，按执行顺序排列；stream_answer
        # 据此发 exec 事件，运维终端页把它们打进终端窗口，让用户看到 AI 干了
        # 什么（与确认后的写命令同一条回显通道）。
        self.readonly_execs: List[Dict[str, Any]] = []

    async def aclose(self) -> None:
        """关掉这次回答期间建立的 SSH 连接。"""
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()

    async def _connection(self, server: OpsServer):
        """同一台机器在一次回答里复用一条连接：每条命令都重连太慢，也刷 auth 日志。"""
        conn = self._conns.get(server.id)
        if conn is None:
            conn = await server_ops.connect(server)
            self._conns[server.id] = conn
            logger.debug("opened an ssh connection to server %s for this answer", server.id)
        else:
            logger.debug("reusing the ssh connection to server %s", server.id)
        return conn

    # -- 绑定目标 -------------------------------------------------------------

    def _target_server(self) -> OpsServer:
        """绑定模式的服务器；目标中途被删时抛 LookupError，由工具转成给模型的文案。"""
        if self._target_server_cache is None:
            if self._target is None or self._target.type != TARGET_SERVER:
                raise LookupError("当前会话没有绑定服务器")
            self._target_server_cache = self._servers.get(self._target.id)
        return self._target_server_cache

    def _target_database(self) -> OpsDatabase:
        if self._target_database_cache is None:
            if self._target is None or self._target.type != TARGET_DATABASE:
                raise LookupError("当前会话没有绑定数据库")
            self._target_database_cache = self._databases.get(self._target.id)
        return self._target_database_cache

    def prompt_suffix(self) -> str:
        """绑定模式下追加到 system prompt 的目标说明（自由模式为空串）。"""
        if self._target is None:
            return ""
        if self._target.type == TARGET_SERVER:
            s = self._target_server()
            return (
                f"\n\n当前已连接的服务器：{s.name}（{s.username}@{s.host}:{s.port}）。"
                "你的所有命令都在这台机器上执行。"
            )
        d = self._target_database()
        kind = "MySQL/MariaDB" if d.db_type == DatabaseType.MYSQL.value else "Redis"
        target = f"{d.host}:{d.port}" + (f"/{d.db_name}" if d.db_name else "")
        return (
            f"\n\n当前已连接的数据库：{d.name}（{kind}，{target}）。"
            f"单次查询最多返回 {settings.OPS_SQL_ROW_LIMIT} 行。"
        )

    # -- 服务器 --------------------------------------------------------------

    async def _exec_server_command(self, server: OpsServer, command: str) -> str:
        """真正下发一条只读命令，并留痕。"""
        try:
            conn = await self._connection(server)
        except server_ops.OpsConnectError as exc:
            self._server_audit.record(
                target_id=server.id,
                target_name=server.name,
                command=command,
                verdict="readonly",
                success=False,
                error=str(exc),
            )
            return f"无法连接服务器：{exc}"

        # AI 通道用更宽的超时：du/find 这类盘点命令在大磁盘上远超交互通道的
        # 20 秒，用交互通道的超时会被误杀。
        started = time.monotonic()
        status, output = await server_ops.run_once(
            conn, command, timeout=settings.OPS_AGENT_CMD_TIMEOUT
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        truncated = (output or "(无输出)")[:OUTPUT_LIMIT]
        self._server_audit.record(
            target_id=server.id,
            target_name=server.name,
            command=command,
            verdict="readonly",
            success=status == 0,
            error=None if status == 0 else output[:512],
        )
        self.readonly_execs.append(
            {
                "command": command,
                "exit_status": status,
                "output": truncated,
                "elapsed_ms": elapsed_ms,
            }
        )
        return f"exit={status}\n{truncated}"

    def _refuse_forbidden(self, server: OpsServer, command: str, reason: str) -> str:
        self._server_audit.record(
            target_id=server.id,
            target_name=server.name,
            command=command,
            verdict="forbidden",
            success=False,
            error=reason,
        )
        return f"该命令被安全策略拒绝：{reason}"

    async def _run_readonly(self, server: OpsServer, command: str) -> str:
        """只读通道：三道分类，该挡的挡，该指引去 propose 的指引。"""
        verdict = classify_command(command)
        if verdict.kind == "forbidden":
            return self._refuse_forbidden(server, command, verdict.reason)
        if verdict.kind == "needs_confirm":
            return (
                f"「{command}」不在只读白名单内（{verdict.reason}），"
                "不能用这个工具执行。如果确实需要，请用 propose_command 提交给用户确认。"
            )
        return await self._exec_server_command(server, command)

    async def _propose(self, server: OpsServer, command: str, reason: str) -> str:
        """写命令通道：落一行 pending 就返回，绝不在这里等用户。

        等待是旧 WebSocket 方案的形态，跨不了 gunicorn 的进程边界。落库之后
        由 confirm 端点认领执行；这里要做的只是告诉模型「本轮到此为止」。
        """
        verdict = classify_command(command)
        if verdict.kind == "forbidden":
            return self._refuse_forbidden(server, command, verdict.reason) + "，无法提交确认"
        if verdict.kind == "readonly":
            # 只读命令没必要打扰用户。
            return await self._exec_server_command(server, command)

        if self._conversation_id is None:
            # 工具箱总会从 stream_answer 拿到会话 id；走到这说明装配错了。
            raise RuntimeError("propose_command requires a conversation_id")

        action = OpsPendingAction(
            owner_id=self._owner_id,
            conversation_id=self._conversation_id,
            target_type=TARGET_SERVER,
            target_id=server.id,
            target_name=server.name,
            command=command,
            reason=reason[:512] if reason else None,
        )
        self._db.add(action)
        self._db.commit()
        self._db.refresh(action)
        self.pending_proposals.append(action)
        # 命令全文可能带敏感参数，审计表已有全文，日志只留首词。
        logger.info(
            "owner %s proposed ops action %s on server %s (%s): %s",
            self._owner_id,
            action.id,
            server.id,
            server.name,
            command.split(None, 1)[0] if command.strip() else "",
        )
        return (
            f"命令已提交用户确认（action #{action.id}）。请立即结束本轮回答，"
            "告知用户在界面点击确认后系统会自动执行并把结果交给你继续分析；"
            "不要重复提交同一条命令。"
        )

    async def execute_confirmed(self, action: OpsPendingAction) -> ConfirmedExecResult:
        """confirm 端点专用：在当前请求自己的 worker 上执行一条已批准的命令。

        执行结果同时写回 pending 行（续答上下文与历史展示用）和审计表
        （verdict=confirmed，带上 pending_action_id 双向关联）。
        """
        started = time.monotonic()
        logger.info(
            "executing confirmed ops action %s on server %s", action.id, action.target_id
        )
        status: Optional[int] = None
        try:
            server = self._servers.get(action.target_id)
            conn = await self._connection(server)
            status, output = await server_ops.run_once(
                conn, action.command, timeout=settings.OPS_AGENT_CMD_TIMEOUT
            )
        except (server_ops.OpsConnectError, LookupError) as exc:
            output = f"无法执行：{exc}"
        elapsed_ms = int((time.monotonic() - started) * 1000)

        success = status == 0
        if success:
            logger.info(
                "ops action %s executed: exit_status=%s, elapsed=%dms",
                action.id,
                status,
                elapsed_ms,
            )
        else:
            logger.warning(
                "ops action %s failed: exit_status=%s, elapsed=%dms",
                action.id,
                status,
                elapsed_ms,
            )
        truncated = (output or "(无输出)")[:OUTPUT_LIMIT]
        finish_execution(
            self._db, action, success=success, result=truncated, exit_status=status
        )
        self._server_audit.record(
            target_id=action.target_id,
            target_name=action.target_name,
            command=action.command,
            verdict="confirmed",
            success=success,
            error=None if success else truncated[:512],
            pending_action_id=action.id,
        )
        return ConfirmedExecResult(
            success=success,
            exit_status=status,
            output=truncated,
            elapsed_ms=elapsed_ms,
        )

    # -- 数据库 ---------------------------------------------------------------

    async def _exec_database_query(self, database: OpsDatabase, statement: str) -> str:
        try:
            result = await db_ops.execute(database, statement)
        except db_ops.OpsDbForbidden as exc:
            self._db_audit.record(
                target_id=database.id,
                target_name=database.name,
                command=statement,
                verdict="forbidden",
                success=False,
                error=str(exc),
            )
            return f"该命令被安全策略拒绝：{exc}。只能执行只读查询。"
        except db_ops.OpsDbError as exc:
            self._db_audit.record(
                target_id=database.id,
                target_name=database.name,
                command=statement,
                verdict="readonly",
                success=False,
                error=str(exc),
            )
            return f"执行失败：{exc}"

        self._db_audit.record(
            target_id=database.id,
            target_name=database.name,
            command=statement,
            verdict="readonly",
            success=True,
        )
        return render_result(result)

    # -- 工具装配 --------------------------------------------------------------

    def tools(self) -> List:
        if self._target is None:
            return self._free_tools()
        if self._target.type == TARGET_SERVER:
            return self._bound_server_tools()
        return self._bound_database_tools()

    def _free_tools(self) -> List:
        """自由模式：主对话里的运维助手，目标 id 由模型从台账里挑。"""
        from langchain_core.tools import tool

        @tool
        async def list_servers() -> str:
            """列出用户在「运维」里登记的所有服务器（含 id），用于回答「我有哪些机器」，
            也是在调用 run_server_command / propose_command 之前确认 server_id 的途径。
            """
            rows = self._servers.list()
            if not rows:
                return "用户还没有登记任何服务器。"
            return "\n".join(
                f"- id={row.id} {row.name}：{row.username}@{row.host}:{row.port}"
                for row in rows
            )

        @tool
        async def run_server_command(server_id: int, command: str) -> str:
            """在指定服务器上执行一条只读命令，返回它的输出。

            server_id 来自 list_servers。只接受不会改变系统状态的命令（ls、cat、
            df、free、ps、top -b -n1、systemctl status、journalctl、docker ps、
            netstat 等）；管道可以用，但每一段都必须是只读命令；重定向、分号、
            反引号一律不允许。需要修改系统状态时，改用 propose_command。
            """
            try:
                server = self._servers.get(server_id)
            except LookupError:
                return "server_id 不存在，请先用 list_servers 确认可用的服务器。"
            return await self._run_readonly(server, command)

        @tool
        async def propose_command(server_id: int, command: str, reason: str) -> str:
            """提议执行一条会改变系统状态的命令，交由用户确认后再执行。

            server_id 来自 list_servers。reason 用中文说明这条命令做什么、
            为什么现在要做、有什么风险——用户看到的就是这段话。提交后请立即
            结束本轮回答；用户拒绝时不要换个写法重试，直接向他说明情况。
            """
            try:
                server = self._servers.get(server_id)
            except LookupError:
                return "server_id 不存在，请先用 list_servers 确认可用的服务器。"
            return await self._propose(server, command, reason)

        @tool
        async def list_databases() -> str:
            """列出用户在「运维」里登记的所有数据库（含 id 与类型），也是在调用
            run_database_query 之前确认 database_id 的途径。
            """
            rows = self._databases.list()
            if not rows:
                return "用户还没有登记任何数据库。"
            lines = []
            for row in rows:
                kind = "MySQL/MariaDB" if row.db_type == DatabaseType.MYSQL.value else "Redis"
                target = f"{row.host}:{row.port}" + (f"/{row.db_name}" if row.db_name else "")
                lines.append(f"- id={row.id} {row.name}：{kind}，{target}")
            return "\n".join(lines)

        @tool
        async def run_database_query(database_id: int, statement: str) -> str:
            """在指定数据库上执行一条只读查询，返回结果表格。

            database_id 来自 list_databases。MySQL 只接受 SELECT / SHOW / DESCRIBE /
            EXPLAIN / WITH（单条语句，不加分号）；Redis 只接受 GET / SCAN / INFO / TTL
            等只读命令。连接本身开在只读事务里，任何写入都会被服务端直接拒绝。
            数据库没有「确认后可写」的通道：需要写入时把 SQL 写给用户，让他到
            「运维」页面自行执行。
            """
            try:
                database = self._databases.get(database_id)
            except LookupError:
                return "database_id 不存在，请先用 list_databases 确认可用的数据库。"
            return await self._exec_database_query(database, statement)

        return [list_servers, run_server_command, propose_command, list_databases, run_database_query]

    def _bound_server_tools(self) -> List:
        """绑定模式（服务器）：工具签名不带 id，命令永远落在绑定的这台机器上。"""
        from langchain_core.tools import tool

        @tool
        async def run_readonly_command(command: str) -> str:
            """在当前服务器上执行一条只读命令，返回它的输出。

            只接受不会改变系统状态的命令（ls、cat、df、free、ps、top -b -n1、
            systemctl status、journalctl、docker ps、netstat 等）。管道可以用，
            但每一段都必须是只读命令；重定向、分号、反引号一律不允许。
            需要修改系统状态时，改用 propose_command。
            """
            try:
                server = self._target_server()
            except LookupError as exc:
                return str(exc)
            return await self._run_readonly(server, command)

        @tool
        async def propose_command(command: str, reason: str) -> str:
            """提议在当前服务器上执行一条会改变系统状态的命令，交由用户确认后再执行。

            reason 用中文说明这条命令做什么、为什么现在要做、有什么风险——
            用户看到的就是这段话。提交后请立即结束本轮回答；用户拒绝时不要
            换个写法重试，直接向他说明情况。
            """
            try:
                server = self._target_server()
            except LookupError as exc:
                return str(exc)
            return await self._propose(server, command, reason)

        @tool
        async def list_servers() -> str:
            """列出用户注册的所有服务器，用于回答「我有哪些机器」这类问题。

            只能看，不能切换：命令始终在当前已连接的那台机器上执行。
            """
            rows = self._servers.list()
            if not rows:
                return "没有注册任何服务器。"
            current_id = self._target.id if self._target is not None else None
            lines = [
                f"- {row.name}：{row.username}@{row.host}:{row.port}"
                f"（上次检测{'正常' if row.last_check_ok else '未通过'}）"
                + ("　← 当前连接" if row.id == current_id else "")
                for row in rows
            ]
            return "\n".join(lines)

        return [run_readonly_command, propose_command, list_servers]

    def _bound_database_tools(self) -> List:
        """绑定模式（数据库）。只读是硬约束，不存在「确认后可写」。"""
        from langchain_core.tools import tool

        try:
            database = self._target_database()
        except LookupError:
            database = None
        is_mysql = database is not None and database.db_type == DatabaseType.MYSQL.value

        async def _run(statement: str) -> str:
            if database is None:
                return "当前会话绑定的数据库已不存在。"
            return await self._exec_database_query(database, statement)

        @tool
        async def run_query(sql: str) -> str:
            """在当前数据库上执行一条只读 SQL，返回结果表格。

            只接受 SELECT / SHOW / DESCRIBE / EXPLAIN / WITH。连接本身开在只读
            事务里，任何写入都会被服务端直接拒绝。不要写多条语句，也不要加分号。
            """
            return await _run(sql)

        @tool
        async def list_tables() -> str:
            """列出当前库里的表及其大致行数，用来摸清结构。"""
            if database is not None and database.db_name:
                # 会话的默认库就是台账里的 db_name（见 _connect_mysql），
                # 用 DATABASE() 取值，避免把库名拼进 SQL 文本。
                return await _run(
                    "SELECT table_name, engine, table_rows, "
                    "ROUND((data_length+index_length)/1024/1024, 2) AS size_mb "
                    "FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() "
                    "ORDER BY (data_length+index_length) DESC"
                )
            return await _run("SHOW DATABASES")

        @tool
        async def describe_table(table: str) -> str:
            """查看一张表的字段定义与索引。写查询之前先用它确认字段名。"""
            # 表名来自模型，不能直接拼进 SQL。只放行标识符字符，其余一律挡掉。
            if not table.replace("_", "").replace("$", "").isalnum():
                return "表名只能包含字母、数字、下划线。"
            columns = await _run(f"SHOW FULL COLUMNS FROM `{table}`")
            indexes = await _run(f"SHOW INDEX FROM `{table}`")
            return f"字段：\n{columns}\n\n索引：\n{indexes}"

        @tool
        async def redis_command(command: str) -> str:
            """在当前 Redis 实例上执行一条只读命令（GET / SCAN / INFO / TTL 等）。

            写命令、EVAL、CONFIG SET、FLUSHALL 等一律会被拒绝。生产实例上
            请用 SCAN 而不是 KEYS。
            """
            return await _run(command)

        return [run_query, list_tables, describe_table] if is_mysql else [redis_command]
