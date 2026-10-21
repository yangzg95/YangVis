"""AI 运维助手的两阶段写确认：状态机、提议落库、确认后执行与续答上下文。

这条链路此前一个测试都没有，而它的正确性恰恰全靠「界面上点不到的分支」撑着：
CAS 只许赢一次、超时不许 confirm 但仍然可以 reject、exec 帧必须先于模型配置
解析、续答不许落成 user 消息。这些在页面上点一遍是点不出来的。

跑文件型 SQLite，SSH 与模型调用全部打桩，不连真实机器、也不碰服务商。

**时间口径**：``ops_pending_action.created_at`` 的 ``server_default`` 在 SQLite 上落
UTC、在 MySQL 上落本地墙上时间，而 ``ops_actions`` 拿它和 ``datetime.now()``（本地
naive）比。所以本文件造行都**显式传 ``created_at=datetime.now()``**，不去吃
``server_default``——否则所有 pending 一建出来就「已超时」，测试会假绿。这条差异
本身由 ``tests/test_time_convention.py`` 用扫源码的方式钉着。

异步入口统一用 ``asyncio.run``（本仓库没装 pytest-asyncio，其余测试也这么写）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base
from app.errors import BusinessError
from app.models.entities import (
    ChatConversation,
    ChatMessage,
    ModelConfig,
    OpsAuditLog,
    OpsDatabase,
    OpsPendingAction,
    OpsServer,
)
from app.models.schemas import DatabaseType, ModelPurpose
from app.services.chat import service as chat_module
from app.services.ops import server as server_ops
from app.services.agents import AgentService, seed_builtin_agents
from app.services.chat import ChatService, ROLE_ASSISTANT, ROLE_USER
from app.services.chat_ops import TARGET_SERVER, ChatOpsToolbox, OpsTarget
from app.services.ops.actions import (
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    claim_for_confirm,
    claim_for_reject,
    display_status,
    finish_execution,
    get_owned,
    is_expired,
    list_for_conversation,
    reset_stale_ops_actions,
)

OWNER = 1
OTHER = 2
CONFIRM_TIMEOUT = int(get_settings().OPS_CONFIRM_TIMEOUT)

WRITE_CMD = "systemctl restart nginx"  # needs_confirm
READONLY_CMD = "df -h"  # readonly
FORBIDDEN_CMD = "rm -rf /var/log/old"  # forbidden
# 只读通道里塞进重定向：分类落 needs_confirm，用来验证「只读工具不许偷偷执行写操作」
SNEAKY_CMD = "cat app.log > /tmp/snap"


def wait(coro):
    return asyncio.run(coro)


async def drain(agen):
    return [item async for item in agen]


# ---- 打桩 ------------------------------------------------------------------


class FakeSSH:
    """ops_server 的连接与执行的替身，同时充当计数与关连接的记账本。"""

    def __init__(self) -> None:
        self.runs: List[tuple] = []
        self.connects = 0
        self.closed = 0
        self.exit_status = 0
        self.output = "nginx.service is now active"
        self.connect_error: Exception | None = None

    async def connect(self, server, service=None):
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        outer = self

        class _Conn:
            def close(self):
                outer.closed += 1

        return _Conn()

    async def run_once(self, conn, command, *, timeout=None, limit=None, server_id=None):
        self.runs.append((command, timeout))
        return self.exit_status, self.output


@pytest.fixture()
def ssh(monkeypatch) -> FakeSSH:
    fake = FakeSSH()
    monkeypatch.setattr(server_ops, "connect", fake.connect)
    monkeypatch.setattr(server_ops, "run_once", fake.run_once)
    return fake


# ---- 造数据 ---------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ops.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", maker)
    session = maker()

    seed_builtin_agents(session)
    session.add(
        ModelConfig(
            owner_id=OWNER,
            purpose=ModelPurpose.CHAT.value,
            title="测试对话模型",
            model_name="unit-test",
            base_url="http://localhost:1/v1",
            is_default=True,
            last_test_ok=True,
        )
    )
    session.add(
        OpsServer(
            owner_id=OWNER,
            name="生产机A",
            host="10.0.0.1",
            port=22,
            username="root",
            auth_type="password",
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def server(db) -> OpsServer:
    return db.scalar(select(OpsServer).where(OpsServer.name == "生产机A"))


def make_database(db, *, name: str, db_type: str) -> OpsDatabase:
    row = OpsDatabase(
        owner_id=OWNER,
        name=name,
        db_type=db_type,
        host="127.0.0.1",
        port=3306 if db_type == DatabaseType.MYSQL.value else 6379,
        username="root",
        db_name="app" if db_type == DatabaseType.MYSQL.value else "0",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_conversation(db, *, owner_id: int = OWNER, target_id: int | None = None) -> ChatConversation:
    """一个绑定了服务器的问答会话（绑定模式）。"""
    row = ChatConversation(
        owner_id=owner_id,
        title="生产机A 运维问答",
        agent_id=AgentService(db, owner_id).require_by_slug("server-ops").id,
        ops_target_type="server",
        ops_target_id=target_id if target_id is not None else server(db).id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_action(
    db,
    *,
    conversation_id: int,
    owner_id: int = OWNER,
    command: str = WRITE_CMD,
    status: str = STATUS_PENDING,
    created_at: datetime | None = None,
    target_id: int | None = None,
    message_id: int | None = None,
) -> OpsPendingAction:
    row = OpsPendingAction(
        owner_id=owner_id,
        conversation_id=conversation_id,
        message_id=message_id,
        target_type=TARGET_SERVER,
        target_id=target_id if target_id is not None else server(db).id,
        target_name="生产机A",
        command=command,
        reason="重启卡住的 nginx",
        status=status,
        # 见模块 docstring：绝不吃 server_default，SQLite 上那是 UTC。
        created_at=created_at or datetime.now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def aged(seconds: int) -> datetime:
    return datetime.now() - timedelta(seconds=seconds)


def audits(db, action_id: int | None = None):
    stmt = select(OpsAuditLog)
    if action_id is not None:
        stmt = stmt.where(OpsAuditLog.pending_action_id == action_id)
    return list(db.scalars(stmt).all())


def toolbox(db, conversation, *, target: OpsTarget | None) -> ChatOpsToolbox:
    return ChatOpsToolbox(
        db, OWNER, target=target, conversation_id=conversation.id
    )


def bound(db, conversation) -> ChatOpsToolbox:
    """绑定模式：会话锚定那台机器，工具签名不带 id。"""
    return toolbox(db, conversation, target=OpsTarget("server", server(db).id))


def free(db, conversation) -> ChatOpsToolbox:
    """自由模式：目标是模型报上来的 id，工具箱不带 target。"""
    return toolbox(db, conversation, target=None)


# ---- 一、状态机：CAS 与过期 ------------------------------------------------


def test_claim_for_confirm_only_wins_once(db):
    """并发/重复点击只有一个请求能迁移——这是整条链路的裁决点。

    SQLite 上做不到真并发，钉的是这条性质：第二次来必须看到非 1 的 rowcount。
    """
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)

    claimed = claim_for_confirm(db, OWNER, action.id)
    assert claimed.status == STATUS_APPROVED
    assert claimed.resolved_at is not None

    with pytest.raises(LookupError):
        claim_for_confirm(db, OWNER, action.id)
    # 第一次的裁决没被改写
    db.expire_all()
    assert get_owned(db, OWNER, action.id).status == STATUS_APPROVED


def test_claim_for_confirm_refuses_expired_but_still_pends(db):
    """超时视同未批准：没人应答就执行写命令，正是这套设计要防的事。

    并且不许顺手改写那一行——过期是惰性换算出来的展示状态，不是新事实。
    """
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id, created_at=aged(CONFIRM_TIMEOUT + 60))

    with pytest.raises(LookupError):
        claim_for_confirm(db, OWNER, action.id)
    assert get_owned(db, OWNER, action.id).status == STATUS_PENDING


def test_claim_for_reject_allows_expired_but_not_twice(db):
    """拒绝和确认的口径是刻意不对称的：用户点的「拒绝」就是答案，只是来晚了。"""
    conv = make_conversation(db)
    late = make_action(db, conversation_id=conv.id, created_at=aged(CONFIRM_TIMEOUT + 60))
    assert claim_for_reject(db, OWNER, late.id).status == STATUS_REJECTED

    fresh = make_action(db, conversation_id=conv.id)
    claim_for_reject(db, OWNER, fresh.id)
    with pytest.raises(LookupError):
        claim_for_reject(db, OWNER, fresh.id)


def test_expired_pending_cannot_be_confirmed_after_being_rejected(db):
    """终态之后两条路都关死：rejected 的行既不能 confirm 也不能再 reject。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    claim_for_reject(db, OWNER, action.id)

    with pytest.raises(LookupError):
        claim_for_confirm(db, OWNER, action.id)
    assert get_owned(db, OWNER, action.id).status == STATUS_REJECTED


def test_finish_execution_insists_on_approved(db):
    """approved 是唯一合法前态：来路不对说明状态机被并发搞乱了，宁可报错。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id, status=STATUS_PENDING)

    with pytest.raises(RuntimeError):
        finish_execution(db, action, success=True, result="ok", exit_status=0)

    action.status = STATUS_APPROVED
    db.commit()
    finish_execution(db, action, success=False, result="boom", exit_status=1)
    db.refresh(action)
    assert action.status == STATUS_FAILED
    assert action.result == "boom"
    assert action.exit_status == 1


def test_display_status_translates_stale_pending_without_writing_back(db):
    conv = make_conversation(db)
    stale = make_action(db, conversation_id=conv.id, created_at=aged(CONFIRM_TIMEOUT + 1))
    fresh = make_action(db, conversation_id=conv.id)

    assert is_expired(stale) is True
    assert display_status(stale) == STATUS_EXPIRED
    assert display_status(fresh) == STATUS_PENDING
    # 惰性换算：库里那行还是 pending
    db.expire_all()
    assert get_owned(db, OWNER, stale.id).status == STATUS_PENDING


def test_reset_stale_ops_actions_materializes_only_the_abandoned(db):
    """进程重启后那些永远等不到回答的 pending 要收尾成 expired，而不是在界面上永远可点。"""
    conv = make_conversation(db)
    abandoned = make_action(db, conversation_id=conv.id, created_at=aged(CONFIRM_TIMEOUT + 30))
    waiting = make_action(db, conversation_id=conv.id)
    done = make_action(db, conversation_id=conv.id, status=STATUS_EXECUTED)

    reset_stale_ops_actions(db)
    db.expire_all()

    assert get_owned(db, OWNER, abandoned.id).status == STATUS_EXPIRED
    assert get_owned(db, OWNER, abandoned.id).resolved_at is not None
    assert get_owned(db, OWNER, waiting.id).status == STATUS_PENDING
    assert get_owned(db, OWNER, done.id).status == STATUS_EXECUTED


def test_owner_scope_is_enforced_everywhere(db):
    """别人的待确认项一律 LookupError 而不是 403：确认它存在本身就是泄露。"""
    conv = make_conversation(db, owner_id=OTHER)
    action = make_action(db, conversation_id=conv.id, owner_id=OTHER)

    with pytest.raises(LookupError):
        claim_for_confirm(db, OWNER, action.id)
    with pytest.raises(LookupError):
        claim_for_reject(db, OWNER, action.id)
    with pytest.raises(LookupError):
        get_owned(db, OWNER, action.id)
    # 会话回放也看不到别人的行
    assert list_for_conversation(db, OWNER, conv.id) == []
    assert list_for_conversation(db, OTHER, conv.id)


# ---- 二、提议阶段：落库就返回，绝不在这条协程里等 ----------------------------


def test_propose_lands_pending_and_never_touches_ssh(db, ssh):
    """写命令只能被*提议*：落一行 pending、返回文案，一次 SSH 都不发起。"""
    conv = make_conversation(db)
    box = bound(db, conv)

    reply = wait(box._propose(server(db), WRITE_CMD, "nginx 卡住了"))

    assert "已提交用户确认" in reply
    rows = list_for_conversation(db, OWNER, conv.id)
    assert len(rows) == 1
    assert rows[0].status == STATUS_PENDING
    assert rows[0].command == WRITE_CMD
    assert rows[0].reason == "nginx 卡住了"
    assert rows[0].message_id is None, "回答还没落库，只能等 stream_answer 回填"
    # detached 对象与 persistent 对象不直接相等，按 id 比对
    assert [a.id for a in box.pending_proposals] == [r.id for r in rows]
    assert ssh.runs == [], "提议阶段不许执行任何东西"
    assert ssh.connects == 0, "提议也不该顺手开一条 SSH 连接"
    assert audits(db) == [], "提议本身不是一种执行，留痕等在 confirm/reject"


def test_propose_of_readonly_runs_it_instead_of_bothering_the_user(db, ssh):
    """模型把只读命令交给 propose 时不该弹窗打扰用户，直接执行。"""
    conv = make_conversation(db)
    box = bound(db, conv)

    reply = wait(box._propose(server(db), READONLY_CMD, "看一眼磁盘"))

    assert "exit=0" in reply
    assert len(ssh.runs) == 1
    assert box.pending_proposals == []
    assert list_for_conversation(db, OWNER, conv.id) == []


def test_propose_of_forbidden_is_refused_without_a_card(db, ssh):
    """连确认都不给的命令，也不能伪造成一张待确认卡片骗用户点。"""
    conv = make_conversation(db)
    box = bound(db, conv)

    reply = wait(box._propose(server(db), FORBIDDEN_CMD, "清理日志"))

    assert "被安全策略拒绝" in reply and "无法提交确认" in reply
    assert list_for_conversation(db, OWNER, conv.id) == []
    assert ssh.runs == []
    assert [row.verdict for row in audits(db)] == ["forbidden"]


def test_readonly_channel_refuses_writes_and_points_to_propose(db, ssh):
    """模型想绕过分类把写命令塞进只读工具：挡下，并把它指引去 propose。"""
    conv = make_conversation(db)
    box = bound(db, conv)

    reply = wait(box._run_readonly(server(db), SNEAKY_CMD))

    assert "propose_command" in reply
    assert ssh.runs == []
    assert list_for_conversation(db, OWNER, conv.id) == []
    assert audits(db) == [], "只是指引，没执行也没被拒，不留痕"


def test_propose_needs_a_conversation_id(db, ssh):
    """没有会话就落不出 pending 行——走到这里说明工具箱装配错了，硬失败。"""
    box = ChatOpsToolbox(db, OWNER, target=OpsTarget("server", server(db).id))

    with pytest.raises(RuntimeError):
        wait(box._propose(server(db), WRITE_CMD, "重启"))
    assert db.scalars(select(OpsPendingAction)).all() == []


def test_command_timeout_is_the_agent_channel_one(db, ssh):
    """AI 通道的命令超时比交互终端宽（du/find 会被 20s 误杀），且不是硬编码。"""
    conv = make_conversation(db)

    wait(bound(db, conv)._run_readonly(server(db), READONLY_CMD))

    assert ssh.runs[0][0] == READONLY_CMD
    assert ssh.runs[0][1] == get_settings().OPS_AGENT_CMD_TIMEOUT
    assert ssh.runs[0][1] > get_settings().OPS_CMD_TIMEOUT


def test_one_answer_reuses_a_single_ssh_connection_and_closes_it(db, ssh):
    """一次回答里同机复用一条连接（每条命令重连既慢又刷 auth 日志），回答结束即关。"""
    conv = make_conversation(db)
    box = bound(db, conv)

    async def go():
        await box._run_readonly(server(db), READONLY_CMD)
        await box._run_readonly(server(db), READONLY_CMD)
        await box.aclose()

    wait(go())

    assert ssh.connects == 1
    assert len(ssh.runs) == 2
    assert ssh.closed == 1


def test_connect_failure_is_reported_to_the_model_and_audited(db, ssh):
    """连不上不是异常，而是一条给模型看的文案 + 一条失败留痕。"""
    conv = make_conversation(db)
    ssh.connect_error = server_ops.OpsConnectError("host key mismatch")
    box = bound(db, conv)

    reply = wait(box._run_readonly(server(db), READONLY_CMD))

    assert "无法连接服务器" in reply
    assert ssh.runs == []
    row = audits(db)[0]
    assert (row.verdict, row.success) == ("readonly", False)
    assert "host key mismatch" in row.error


# ---- 三、工具面：绑定模式没有 id，自由模式才有 -------------------------------


def tool_names(box: ChatOpsToolbox) -> List[str]:
    return [item.name for item in box.tools()]


def tool_args(box: ChatOpsToolbox, name: str) -> List[str]:
    for item in box.tools():
        if item.name == name:
            return sorted(item.args)
    raise AssertionError(f"没有工具 {name}，实际是 {tool_names(box)}")


def test_bound_server_tools_expose_no_target_id(db):
    """绑定模式的工具签名里根本没有 id：模型切不了目标，命令只落在这台机器上。"""
    conv = make_conversation(db)
    box = bound(db, conv)

    assert tool_names(box) == ["run_readonly_command", "propose_command", "list_servers"]
    assert tool_args(box, "propose_command") == ["command", "reason"]


def test_bound_database_tools_are_readonly_only(db):
    """数据库侧不存在「确认后可写」：既没有 propose 工具，也没有写入通道。"""
    conv = make_conversation(db)
    mysql = make_database(db, name="业务库", db_type=DatabaseType.MYSQL.value)

    box = toolbox(db, conv, target=OpsTarget("database", mysql.id))

    assert tool_names(box) == ["run_query", "list_tables", "describe_table"]
    assert "propose_command" not in tool_names(box)

    redis = make_database(db, name="缓存", db_type=DatabaseType.REDIS.value)
    assert tool_names(toolbox(db, conv, target=OpsTarget("database", redis.id))) == [
        "redis_command"
    ]


def test_free_tools_expose_ids_and_the_ownership_check_stays_in_the_closure(db):
    """自由模式把 id 交给模型挑，但写入的归属永远是发起人：别人的机器也替他确认不了。"""
    conv = make_conversation(db)
    box = free(db, conv)

    assert tool_names(box) == [
        "list_servers",
        "run_server_command",
        "propose_command",
        "list_databases",
        "run_database_query",
    ]
    assert tool_args(box, "propose_command") == ["command", "reason", "server_id"]
    assert box.prompt_suffix() == "", "自由模式不追加目标说明"


def test_free_mode_proposal_belongs_to_the_acting_user(db, ssh):
    """台账全员共用，模型能对别人登记的机器发起提议；但 pending 行的 owner 是发起人。

    这条才是自由模式真正的归属保证：confirm/reject 都按 owner 认领，登记那台机器的
    人看不见、也确认不了这条提议。
    """
    other = OpsServer(
        owner_id=OTHER, name="别人的机器", host="10.0.0.9", port=22,
        username="root", auth_type="password",
    )
    db.add(other)
    db.commit()
    conv = make_conversation(db)
    box = free(db, conv)

    reply = wait(box._propose(other, WRITE_CMD, "重启"))

    assert "已提交用户确认" in reply
    action = list_for_conversation(db, OWNER, conv.id)[0]
    assert action.owner_id == OWNER, "归属记在发起人身上，不是机器登记人"
    assert action.target_id == other.id
    with pytest.raises(LookupError):
        claim_for_confirm(db, OTHER, action.id)


def test_bound_prompt_suffix_names_the_current_target(db):
    conv = make_conversation(db)

    suffix = bound(db, conv).prompt_suffix()

    assert "当前已连接的服务器" in suffix and "生产机A" in suffix
    assert "root@10.0.0.1:22" in suffix


# ---- 四、确认的下半场：执行、写回、留痕 ------------------------------------


def test_execute_confirmed_writes_result_and_audits_with_backlink(db, ssh):
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    claim_for_confirm(db, OWNER, action.id)
    db.refresh(action)
    box = bound(db, conv)

    result = wait(box.execute_confirmed(action))

    assert (result.success, result.exit_status) == (True, 0)
    assert result.output == ssh.output
    db.refresh(action)
    assert action.status == STATUS_EXECUTED
    assert action.result == ssh.output
    assert ssh.runs == [(WRITE_CMD, get_settings().OPS_AGENT_CMD_TIMEOUT)]

    row = audits(db, action.id)[0]
    assert row.verdict == "confirmed"
    assert row.pending_action_id == action.id
    assert (row.target_name, row.actor) == ("生产机A", "ai")


def test_execute_confirmed_marks_nonzero_exit_as_failed(db, ssh):
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    claim_for_confirm(db, OWNER, action.id)
    db.refresh(action)
    ssh.exit_status, ssh.output = 5, "Job for nginx.service failed"

    result = wait(bound(db, conv).execute_confirmed(action))

    assert result.success is False and result.exit_status == 5
    db.refresh(action)
    assert action.status == STATUS_FAILED
    assert audits(db, action.id)[0].success is False


def test_execute_confirmed_turns_connect_failure_into_an_answerable_result(db, ssh):
    """连不上也要给出可续答的结果，而不是把 confirm 这条流炸掉。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    claim_for_confirm(db, OWNER, action.id)
    db.refresh(action)
    ssh.connect_error = server_ops.OpsConnectError("connection refused")

    result = wait(bound(db, conv).execute_confirmed(action))

    assert result.success is False and result.exit_status is None
    assert "无法执行" in result.output
    db.refresh(action)
    assert action.status == STATUS_FAILED


def test_execute_confirmed_survives_a_deleted_server(db, ssh):
    """目标被删（LookupError）同样落成失败，续答看得到发生了什么。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    claim_for_confirm(db, OWNER, action.id)
    db.refresh(action)
    target_id = action.target_id
    db.delete(server(db))
    db.commit()

    result = wait(
        ChatOpsToolbox(
            db, OWNER, target=OpsTarget("server", target_id), conversation_id=conv.id
        ).execute_confirmed(action)
    )

    assert result.success is False and "无法执行" in result.output


# ---- 五、确认流：exec 先于一切，续答不落 user 消息 ---------------------------


@pytest.fixture()
def answer_stub(monkeypatch):
    """替掉 stream_answer，只记录它收到了什么——事件序列的另一半在这里钉。"""
    seen: List[dict] = []

    async def fake(self, **kwargs):
        seen.append(kwargs)
        yield "token", "nginx 已经起来了"
        yield "done", {"message_id": 0, "conversation_id": kwargs["conversation_id"]}

    monkeypatch.setattr(ChatService, "stream_answer", fake)
    return seen


def test_confirm_stream_puts_exec_before_the_answer(db, ssh, answer_stub):
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)

    events = wait(drain(ChatService(db, OWNER).confirm_action_stream(action_id=action.id)))

    names = [name for name, _ in events]
    assert names[0] == "exec"
    assert set(names) == {"exec", "token", "done"}
    payload = dict(events[0][1])
    assert payload["action_id"] == action.id
    assert payload["command"] == WRITE_CMD
    assert payload["exit_status"] == 0
    assert payload["success"] is True
    assert payload["elapsed_ms"] >= 0


def test_confirm_feeds_the_result_back_as_a_system_note_not_a_user_message(
    db, ssh, answer_stub
):
    """续答的上下文是合成的旁白：它不落 user 消息，否则历史里会出现一句用户没说的话。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)

    wait(drain(ChatService(db, OWNER).confirm_action_stream(action_id=action.id)))

    assert len(answer_stub) == 1
    kwargs = answer_stub[0]
    assert kwargs["question_persisted"] is False
    assert kwargs["conversation_id"] == conv.id
    context = kwargs["question"]
    assert context.startswith("[系统] 用户已批准")
    assert WRITE_CMD in context
    assert "生产机A" in context
    assert ssh.output in context and "退出码：0" in context
    assert "请基于执行结果继续分析" in context

    assert db.scalars(select(ChatMessage)).all() == [], "续答期间不许往历史里塞 user 消息"


def test_exec_event_survives_a_broken_model_config(db, ssh):
    """命令已经跑完了，这时才发现模型不可用绝不能把结果吞掉——exec 必须先发。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    config = db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))
    config.last_test_ok = False
    db.commit()

    collected: List = []
    with pytest.raises(BusinessError):
        wait(drain_iter(ChatService(db, OWNER).confirm_action_stream(action_id=action.id), collected))

    assert [name for name, _ in collected] == ["exec"]
    db.refresh(action)
    assert action.status == STATUS_EXECUTED, "结果已经落库，续答失败不影响它"


async def drain_iter(agen, sink):
    async for item in agen:
        sink.append(item)


def test_confirm_stream_on_an_already_handled_action_is_a_409(db, ssh, answer_stub):
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    claim_for_confirm(db, OWNER, action.id)

    with pytest.raises(BusinessError) as exc:
        wait(drain(ChatService(db, OWNER).confirm_action_stream(action_id=action.id)))
    assert exc.value.code == 409
    assert ssh.runs == [], "抢不到认领就不许执行"


def test_confirm_stream_on_an_expired_action_is_a_409(db, ssh, answer_stub):
    """超时也归 409（已处理或已超时）：前端据此把卡片翻成「等待超时」。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id, created_at=aged(CONFIRM_TIMEOUT + 60))

    with pytest.raises(BusinessError) as exc:
        wait(drain(ChatService(db, OWNER).confirm_action_stream(action_id=action.id)))
    assert exc.value.code == 409
    assert ssh.runs == []
    assert get_owned(db, OWNER, action.id).status == STATUS_PENDING


def test_confirm_stream_on_a_missing_action_is_a_plain_lookup(db, ssh, answer_stub):
    with pytest.raises(LookupError):
        wait(drain(ChatService(db, OWNER).confirm_action_stream(action_id=12345)))
    assert ssh.runs == []


def test_confirm_closes_the_ssh_connection_it_opened(db, ssh, answer_stub):
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)

    wait(drain(ChatService(db, OWNER).confirm_action_stream(action_id=action.id)))

    assert ssh.closed == 1, "confirm 期间的连接要在本请求结束时收掉"


# ---- 六、拒绝：留痕 + 一条让历史自洽的助手消息 -------------------------------


def test_reject_records_a_card_and_writes_an_explicit_end_to_history(db):
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)

    item = ChatService(db, OWNER).reject_action(action.id)

    assert item.status == STATUS_REJECTED
    assert audits(db, action.id)[0].verdict == "rejected"
    tail = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conv.id)).all()
    assert len(tail) == 1
    assert tail[0].role == ROLE_ASSISTANT, "不给模型留一个「提议了但没有下文」的悬案"
    assert WRITE_CMD in tail[0].content


def test_reject_is_allowed_without_ops_write(db):
    """拒绝是安全方向，不设权限位——这条不设限是刻意的。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)

    assert ChatService(db, OWNER).reject_action(action.id).status == STATUS_REJECTED


def test_reject_after_being_confirmed_gives_the_late_arrival_message(db, ssh):
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    claim_for_confirm(db, OWNER, action.id)

    with pytest.raises(BusinessError) as exc:
        ChatService(db, OWNER).reject_action(action.id)
    assert exc.value.code == 409


def test_reject_of_a_missing_action_is_a_plain_lookup(db):
    with pytest.raises(LookupError):
        ChatService(db, OWNER).reject_action(12345)


def test_list_actions_keeps_the_proposal_order_for_replay(db):
    conv = make_conversation(db)
    first = make_action(db, conversation_id=conv.id, command="systemctl restart a")
    second = make_action(db, conversation_id=conv.id, command="systemctl restart b")

    items = ChatService(db, OWNER).list_actions(conv.id)

    assert [item.id for item in items] == [first.id, second.id]
    assert items[0].timeout_seconds == CONFIRM_TIMEOUT
    assert [item.status for item in items] == [STATUS_PENDING, STATUS_PENDING]


def test_list_actions_reports_an_expired_proposal_as_expired(db):
    """回放走 display_status：惰性换算出来的过期要在接口里就说成 expired。"""
    conv = make_conversation(db)
    make_action(db, conversation_id=conv.id, created_at=aged(CONFIRM_TIMEOUT + 1))

    assert ChatService(db, OWNER).list_actions(conv.id)[0].status == STATUS_EXPIRED


# ---- 七、stream_answer 的事件协议：提议如何变成一帧 confirm -------------------


def as_event(spec) -> dict:
    kind = spec[0]
    if kind == "tool":
        return {"event": "on_tool_start", "name": spec[1], "data": {"input": spec[2]}}
    if kind == "tool_end":
        return {"event": "on_tool_end", "name": "t", "data": {"output": "x"}}
    if kind == "token":
        return {
            "event": "on_chat_model_stream",
            "data": {"chunk": SimpleNamespace(content=spec[1])},
        }
    raise AssertionError(f"未知事件 {kind}")


class FakeExecutor:
    def __init__(self, specs, holder, *, captured):
        self._specs = specs
        self._holder = holder
        self._captured = captured

    def astream_events(self, state, *, config=None, version=None):
        self._captured["recursion_limit"] = (config or {}).get("recursion_limit")
        self._captured["messages"] = state["messages"]

        specs, error = list(self._specs), self._holder["error"]

        async def gen():
            for spec in specs:
                yield as_event(spec)
            if error is not None:
                raise error

        return gen()


class StubToolbox:
    """顶替 ChatOpsToolbox：只负责把「本轮记账」摆出来，让事件发射那段逻辑可测。

    实例由 ``stream_answer`` 自己构造，测试通过 agent_env 预置这一轮该看到的
    pending/exec，并从 instances 里取回它，验证收尾时的 aclose。
    """

    def __init__(self, db, owner_id, *, target=None, conversation_id=None):
        self.db = db
        self.owner_id = owner_id
        self.target = target
        self.conversation_id = conversation_id
        self.pending_proposals: List[OpsPendingAction] = []
        self.readonly_execs: List[dict] = []
        self.closed = False

    def tools(self):
        return []

    def prompt_suffix(self):
        return ""

    async def aclose(self):
        self.closed = True


@pytest.fixture()
def agent_env(db, monkeypatch):
    holder: dict = {"error": None}
    captured: dict = {}
    instances: List[StubToolbox] = []
    prefill: dict = {"pending": [], "execs": []}

    def create_agent(client, tools, system_prompt=None):
        captured["system"] = system_prompt or ""
        captured["tools"] = [getattr(t, "name", str(t)) for t in tools]
        return FakeExecutor(holder["events"], holder, captured=captured)

    def make_toolbox(db_, owner_id, *, target=None, conversation_id=None):
        box = StubToolbox(
            db_, owner_id, target=target, conversation_id=conversation_id
        )
        box.pending_proposals = list(prefill["pending"])
        box.readonly_execs = list(prefill["execs"])
        instances.append(box)
        return box

    monkeypatch.setattr("langchain.agents.create_agent", create_agent)
    monkeypatch.setattr(chat_module, "build_chat_model", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(chat_module, "ChatOpsToolbox", make_toolbox)
    # 摘要与记忆提取是自开的后台任务，不属于这条链路的验收点。
    monkeypatch.setattr(chat_module, "fire_and_forget", lambda task: None)
    monkeypatch.setattr(chat_module, "after_answer_task", lambda *a: None)
    holder["events"] = []
    return SimpleNamespace(
        captured=captured, holder=holder, instances=instances, prefill=prefill
    )


def run_answer(db, conv, agent_slug, *, disconnect=None, question="帮我把 nginx 重启一下"):
    """跑一整轮 stream_answer，把 (event, payload) 收成列表。

    提问由路由先落库再问，这里照做，好让「历史里有一条 user 消息」这个前提成立。
    """
    config = db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))
    ChatService(db, OWNER).add_message(conv.id, ROLE_USER, question)
    agent = AgentService(db, OWNER).require_by_slug(agent_slug)

    async def go():
        service = ChatService(db, OWNER)
        out = []
        agen = service.stream_answer(
            conversation_id=conv.id,
            agent=agent,
            question=question,
            config=config,
            is_disconnected=disconnect,
        )
        async for item in agen:
            out.append(item)
        return out

    return wait(go())


def test_ops_answer_emits_step_then_exec_then_confirm_then_done(db, agent_env):
    """提议落库后 confirm 帧要和本轮回答一起出现，而不是等 done——卡片得跟着那条气泡。"""
    conv = make_conversation(db)
    action = make_action(db, conversation_id=conv.id)
    agent_env.prefill["pending"] = [action]
    agent_env.prefill["execs"] = [
        {"command": READONLY_CMD, "exit_status": 0, "output": "none", "elapsed_ms": 12}
    ]
    agent_env.holder["events"] = [
        ("tool", "run_readonly_command", {"command": READONLY_CMD}),
        ("tool_end",),
        ("tool", "propose_command", {"command": WRITE_CMD, "reason": "重启 nginx"}),
        ("tool_end",),
        ("token", "已提交给你确认。"),
    ]

    events = run_answer(db, conv, "ops-assistant")
    names = [name for name, _ in events]

    # 工具开始发 step、返回先补一帧 step_result 再发 citations/exec。
    assert names[:2] == ["step", "step_result"]
    assert names.index("exec") < names.index("confirm") < names.index("done")
    confirm = dict(events[names.index("confirm")][1])
    assert confirm["action_id"] == action.id
    assert confirm["command"] == WRITE_CMD
    assert confirm["timeout_seconds"] == CONFIRM_TIMEOUT
    assert events[names.index("step")][1]["input"] == {"command": READONLY_CMD}
    assert events[names.index("exec")][1]["action_id"] is None, "只读回显不占确认编号"

    db.refresh(action)
    done = dict(events[-1][1])
    assert action.message_id == done["message_id"], "回填的正是那条 assistant 消息"
    assert agent_env.instances[0].closed is True, "回答结束要把这次开的连接收掉"


def test_confirm_frames_are_emitted_once_per_proposal(db, agent_env):
    """两条提议各发一帧，不重复：sent_proposals 的游标漏了就会弹两张一样的卡。"""
    conv = make_conversation(db)
    first = make_action(db, conversation_id=conv.id, command="systemctl restart a")
    second = make_action(db, conversation_id=conv.id, command="systemctl restart b")
    agent_env.prefill["pending"] = [first, second]
    agent_env.holder["events"] = [("tool_end",), ("tool_end",), ("tool_end",)]

    names = [name for name, _ in run_answer(db, conv, "ops-assistant")]

    assert names.count("confirm") == 2
    assert names.count("citations") == 3


def test_ops_answer_raises_the_step_limit_and_tells_the_user_to_continue(
    db, agent_env
):
    """撞上限不算失败：已流出的内容照常落库，但要补一句「回复继续」，
    否则运维排查被掐断看起来就像 AI 说到一半没声了。"""
    from langgraph.errors import GraphRecursionError

    conv = make_conversation(db)
    agent_env.holder["events"] = [("tool", "run_readonly_command", {"command": READONLY_CMD})]
    agent_env.holder["error"] = GraphRecursionError("step limit")

    events = run_answer(db, conv, "ops-assistant")

    assert events[-1][0] == "done"
    assert events[-2] == ("token", chat_module.STEP_LIMIT_SUFFIX)
    saved = db.scalar(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conv.id, ChatMessage.role == ROLE_ASSISTANT)
        .order_by(ChatMessage.id.desc())
    )
    assert saved.content.endswith(chat_module.STEP_LIMIT_SUFFIX.strip())
    assert agent_env.captured["recursion_limit"] == chat_module.OPS_REACT_STEPS


def test_ops_answer_keeps_the_partial_when_the_user_hits_stop(db, agent_env):
    """用户点停止：存他真看到的那半截并打上标记，且不再发 done（客户端已经走了）。"""
    conv = make_conversation(db)
    agent_env.holder["events"] = [("token", "前半句"), ("token", "后半句")]
    asked = []

    async def disconnected():
        # 第一个 token 已经发给用户了，第二次轮询才算他点了停止。
        asked.append(len(asked))
        return len(asked) > 1

    events = run_answer(db, conv, "ops-assistant", disconnect=disconnected)

    assert events == [("token", "前半句")], "落库的要就是用户屏幕上出现过的那些"
    assert "done" not in [name for name, _ in events], "客户端已经走了，别再发收尾帧"
    saved = db.scalars(
        select(ChatMessage).where(ChatMessage.conversation_id == conv.id).order_by(ChatMessage.id)
    ).all()
    assert [row.role for row in saved] == [ROLE_USER, ROLE_ASSISTANT]
    assert saved[1].content == "前半句" + chat_module.ABORTED_SUFFIX


def test_ops_answer_without_a_toolbox_uses_the_search_step_budget(db, agent_env):
    """没挂运维工具箱时仍是 8 步：放宽到 25 只服务于排查，不该顺手抬高普通对话。"""
    conv = ChatConversation(owner_id=OWNER, title="普通会话")
    db.add(conv)
    db.commit()

    run_answer(db, conv, "general-chat")

    assert agent_env.captured["recursion_limit"] == chat_module.MAX_REACT_STEPS
    assert "运维" not in agent_env.captured["system"]
    assert agent_env.instances == [], "非运维会话不装配工具箱"


def test_ops_rules_are_appended_after_the_persona(db, agent_env):
    """「写命令必须经用户确认」是硬约束，不该因为用户改了人设就消失——
    所以它由服务端追加在人设之后，而不是写死在人设里。"""
    conv = make_conversation(db)
    agent = AgentService(db, OWNER).require_by_slug("ops-assistant")

    run_answer(db, conv, "ops-assistant")

    system = agent_env.captured["system"]
    assert system.startswith(agent.system_prompt)
    assert chat_module._OPS_RULES in system
    assert system.index(chat_module._OPS_RULES) > system.index(agent.system_prompt)
