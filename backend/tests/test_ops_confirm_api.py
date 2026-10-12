"""``/chat/actions/*`` 这一层的契约：权限闸门的位置、恒 200 的错误帧、SSE 线格式。

服务层的性质（CAS 只赢一次、exec 先于模型解析）在 ``tests/test_ops_confirm.py``
里已经钉过；这里只管 HTTP 这一层才暴露得出来的东西：

- ``ops_write`` 闸门必须排在 CAS **之前**——无权限用户不能把 pending 认领成
  approved，否则下一句就是「已在别人的机器上执行了写命令」。
- 失败要落成 ``error`` 事件帧而不是 HTTP 4xx：确认流的响应头早在第一个帧之前
  就发出去了，此刻改状态码已经来不及。
- 线格式（``event: X\ndata: {json}\n\n``）与 ``X-Accel-Buffering: no``：前端按帧
  解析、Nginx 按头决定是否攒包，两处写错都是「本地好好的、线上转圈」。

SQLite 跑文件库，SSH 与续答模型调用全部打桩。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import ChatConversation, ModelConfig, OpsPendingAction, OpsServer
from app.models.schemas import CurrentUser, ModelPurpose
from app.services.ops import server as server_ops
from app.services.agents import AgentService, seed_builtin_agents
from app.services.chat import ChatService
from app.services.chat_ops import TARGET_SERVER
from app.services.ops.actions import (
    STATUS_PENDING,
    STATUS_REJECTED,
    get_owned,
)
from app.sse import SSE_HEARTBEAT, with_heartbeat

OWNER = 1
CONFIRM_TIMEOUT = int(get_settings().OPS_CONFIRM_TIMEOUT)
WRITE_CMD = "systemctl restart nginx"


# ---- 环境 ------------------------------------------------------------------


class FakeSSH:
    def __init__(self) -> None:
        self.runs: List[str] = []

    async def connect(self, server, service=None):
        class _Conn:
            def close(self):
                pass

        return _Conn()

    async def run_once(self, conn, command, *, timeout=None, limit=None):
        self.runs.append(command)
        return 0, "nginx.service is now active"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """一个只挂 /api/chat 的裸 app：路由顺序、依赖注入、信封都在真实请求下跑。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'confirm.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", maker)
    db = maker()

    seed_builtin_agents(db)
    db.add(
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
    db.add(
        OpsServer(
            owner_id=OWNER,
            name="生产机A",
            host="10.0.0.1",
            port=22,
            username="root",
            auth_type="password",
        )
    )
    db.commit()

    ssh = FakeSSH()
    monkeypatch.setattr(server_ops, "connect", ssh.connect)
    monkeypatch.setattr(server_ops, "run_once", ssh.run_once)

    # 续答要真调服务商，不属于这层的验收点：换成固定的两帧。
    async def fake_stream_answer(self, **kwargs):
        yield "token", "nginx 已经起来了"
        yield "done", {"message_id": 0, "conversation_id": kwargs["conversation_id"]}

    monkeypatch.setattr(ChatService, "stream_answer", fake_stream_answer)

    # 用真实 app：信封怎么套、403 的 error 帧走不走得通全局异常处理器，本身就是
    # 这一层要验的东西，裸 app 里测出来的是另一套接线。
    state = {"user": CurrentUser(user_id=OWNER, username="t", is_admin=True)}

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: state["user"]

    client = TestClient(app)
    try:
        yield SimpleNamespace(db=db, client=client, ssh=ssh, as_user=state.__setitem__)
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def server(db) -> OpsServer:
    return db.scalar(select(OpsServer).where(OpsServer.name == "生产机A"))


def conversation(db) -> ChatConversation:
    row = ChatConversation(
        owner_id=OWNER,
        title="生产机A 运维问答",
        agent_id=AgentService(db, OWNER).require_by_slug("server-ops").id,
        ops_target_type="server",
        ops_target_id=server(db).id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def proposal(
    db,
    *,
    conv: ChatConversation | None = None,
    command: str = WRITE_CMD,
    age_seconds: int = 0,
    status: str = STATUS_PENDING,
) -> OpsPendingAction:
    row = OpsPendingAction(
        owner_id=OWNER,
        conversation_id=(conv or conversation(db)).id,
        target_type=TARGET_SERVER,
        target_id=server(db).id,
        target_name="生产机A",
        command=command,
        reason="重启卡住的 nginx",
        status=status,
        # created_at 显式给：SQLite 的 server_default 落 UTC，见 test_ops_confirm 的说明。
        created_at=datetime.now() - timedelta(seconds=age_seconds),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def frames(body: str) -> List[tuple]:
    """按前端同样的切帧规则把 SSE 文本还原成 ``(event, payload)``，注释帧丢弃。"""
    out = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        lines = block.split("\n")
        name = lines[0].split(":", 1)[1].strip()
        data = lines[1].split(":", 1)[1].strip()
        out.append((name, json.loads(data)))
    return out


# ---- 一、确认流：恒 200 的 SSE，exec 打头 ----------------------------------


def test_confirm_streams_exec_then_token_then_done(env):
    action = proposal(env.db)

    resp = env.client.post(f"/api/chat/actions/{action.id}/confirm")

    assert resp.status_code == 200, "SSE 的状态码在第一个帧之前就发了，永远是 200"
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Nginx 默认会攒完整个响应再吐，漏写这一条就是「本地流式、线上整段等待」。
    assert resp.headers["x-accel-buffering"] == "no"

    events = frames(resp.text)
    assert [name for name, _ in events] == ["exec", "token", "done"]
    assert resp.text.startswith("event: exec\n")
    exec_payload = events[0][1]
    assert exec_payload["action_id"] == action.id
    assert exec_payload["success"] is True
    assert exec_payload["exit_status"] == 0
    assert exec_payload["output"] == "nginx.service is now active"

    env.db.refresh(action)
    assert action.status == "executed"
    assert action.result == "nginx.service is now active"


def test_confirm_actually_runs_the_command_once(env):
    action = proposal(env.db)

    env.client.post(f"/api/chat/actions/{action.id}/confirm")

    assert env.ssh.runs == [WRITE_CMD], "认领成功才执行，且只执行一次"


# ---- 二、ops_write 闸门排在 CAS 之前 ---------------------------------------


def test_confirm_without_write_permission_leaves_the_row_pending(env):
    """无写权限的用户点确认：403 落成 error 帧，且那行必须还是 pending。

    顺序是关键——闸门若排在认领之后，请求虽然报了 403，命令却已经在机器上跑了。
    """
    action = proposal(env.db)
    env.as_user("user", CurrentUser(user_id=OWNER, username="reader"))

    resp = env.client.post(f"/api/chat/actions/{action.id}/confirm")

    assert resp.status_code == 200
    events = frames(resp.text)
    assert events == [("error", {"code": 403, "message": "没有运维写权限，请联系管理员"})]
    assert env.ssh.runs == []
    # 认领没发生过：状态、裁决时间都还是原样
    assert get_owned(env.db, OWNER, action.id).status == STATUS_PENDING
    assert get_owned(env.db, OWNER, action.id).resolved_at is None


def test_ops_write_flag_is_enough_without_being_admin(env):
    action = proposal(env.db)
    env.as_user(
        "user", CurrentUser(user_id=OWNER, username="ops", is_admin=False, ops_write=True)
    )

    resp = env.client.post(f"/api/chat/actions/{action.id}/confirm")

    assert [name for name, _ in frames(resp.text)] == ["exec", "token", "done"]
    assert env.ssh.runs == [WRITE_CMD]


# ---- 三、失败也走 error 帧：409（已处理/超时）、404（不存在）、-1（意外） ----


def test_confirming_twice_reports_409_and_does_not_run_the_command_twice(env):
    action = proposal(env.db)

    first = env.client.post(f"/api/chat/actions/{action.id}/confirm")
    second = env.client.post(f"/api/chat/actions/{action.id}/confirm")

    assert [name for name, _ in frames(first.text)] == ["exec", "token", "done"]
    assert frames(second.text) == [
        ("error", {"code": 409, "message": "该命令已处理或已超时，请让 AI 重新提议"})
    ]
    assert env.ssh.runs == [WRITE_CMD], "CAS 输了就不许再碰机器"


def test_confirming_an_expired_proposal_reports_409(env):
    """超时 409：没人应答的写命令不能因为用户后来想起来点了确认就跑。"""
    action = proposal(env.db, age_seconds=CONFIRM_TIMEOUT + 60)

    resp = env.client.post(f"/api/chat/actions/{action.id}/confirm")

    assert frames(resp.text) == [
        ("error", {"code": 409, "message": "该命令已处理或已超时，请让 AI 重新提议"})
    ]
    assert env.ssh.runs == []
    assert get_owned(env.db, OWNER, action.id).status == STATUS_PENDING


def test_confirming_an_unknown_action_reports_404(env):
    resp = env.client.post("/api/chat/actions/12345/confirm")

    assert frames(resp.text) == [("error", {"code": 404, "message": "待确认项不存在"})]


def test_an_unexpected_failure_still_ends_with_an_error_frame(env, monkeypatch):
    """底层炸了（SSH 之外的意外）也要收尾成一帧，不能把流断在半空。"""

    async def broken(self, **kwargs):
        yield "exec", {"action_id": 1, "success": True}
        raise RuntimeError("ssh 断了")

    monkeypatch.setattr(ChatService, "confirm_action_stream", broken)

    events = frames(env.client.post("/api/chat/actions/1/confirm").text)

    assert [name for name, _ in events] == ["exec", "error"]
    assert events[-1][1]["code"] == -1
    assert "命令执行失败" in events[-1][1]["message"]


# ---- 四、拒绝：走 JSON 信封，不设权限位 ------------------------------------


def test_reject_returns_the_envelope_and_rejects_only_once(env):
    action = proposal(env.db)
    env.as_user("user", CurrentUser(user_id=OWNER, username="reader"))

    first = env.client.post(f"/api/chat/actions/{action.id}/reject").json()
    # 第二次：业务拒绝仍是 HTTP 200 + 非零 code，前端按 code 出文案。
    second = env.client.post(f"/api/chat/actions/{action.id}/reject")

    assert first["code"] == 0
    assert first["data"]["status"] == STATUS_REJECTED
    assert first["data"]["command"] == WRITE_CMD
    assert second.status_code == 200
    assert second.json()["code"] == 409
    assert get_owned(env.db, OWNER, action.id).status == STATUS_REJECTED


def test_reject_of_an_unknown_action_is_http_404(env):
    resp = env.client.post("/api/chat/actions/12345/reject")

    assert resp.status_code == 404
    assert resp.json()["code"] == 404


def test_reject_of_an_expired_proposal_is_allowed(env):
    """过期只挡住「执行」，不挡「拒绝」：用户点拒绝就是答案，只是来晚了。"""
    action = proposal(env.db, age_seconds=CONFIRM_TIMEOUT + 60)

    body = env.client.post(f"/api/chat/actions/{action.id}/reject").json()

    assert body["code"] == 0
    assert body["data"]["status"] == STATUS_REJECTED


# ---- 五、刷新页面后的回放 --------------------------------------------------


def test_list_actions_replays_cards_including_the_expired_one(env):
    """刷新页面后卡片靠它恢复：顺序、倒计时秒数、过期都得说对。"""
    conv = conversation(env.db)
    fresh = proposal(env.db, conv=conv)
    stale = proposal(env.db, conv=conv, command="systemctl restart b",
                     age_seconds=CONFIRM_TIMEOUT + 1)

    items = env.client.get(f"/api/chat/conversations/{conv.id}/actions").json()["data"]["items"]

    assert [item["id"] for item in items] == [fresh.id, stale.id]
    assert [item["status"] for item in items] == [STATUS_PENDING, "expired"]
    assert items[0]["timeout_seconds"] == CONFIRM_TIMEOUT
    assert items[0]["reason"] == "重启卡住的 nginx"
    assert items[0]["command"] == WRITE_CMD


def test_list_actions_of_another_users_conversation_is_404(env):
    conv = conversation(env.db)
    proposal(env.db, conv=conv)

    env.as_user("user", CurrentUser(user_id=OWNER + 1, username="other"))

    assert env.client.get(f"/api/chat/conversations/{conv.id}/actions").status_code == 404


# ---- 六、心跳：确认流不能因为「没人说话」被链路掐断 ------------------------


def test_heartbeat_frames_are_inserted_without_swallowing_the_source():
    """空档超过 interval 就补一帧 ``: ping``，而下层那条等待不能被打断。

    直接 ``wait_for(source.__anext__())`` 会把取消送进下层生成器，等于自己掐断
    正在等 provider 回答的那条流——shield 就是为这一条存在的。
    """
    received: List[str] = []

    async def slow():
        yield "event: a\n\n"
        await asyncio.sleep(0.05)
        yield "event: b\n\n"

    async def go():
        async for chunk in with_heartbeat(slow(), interval=0.01):
            received.append(chunk)

    asyncio.run(go())

    assert SSE_HEARTBEAT in received, "空档里要有一帧 ping 把各方的空闲计时重置掉"
    assert [chunk for chunk in received if chunk != SSE_HEARTBEAT] == [
        "event: a\n\n",
        "event: b\n\n",
    ], "慢出来的那一帧还在，没被心跳挤掉"


def test_abandoning_the_consumer_cancels_the_pending_source_read():
    """客户端断开时挂着的下层读取要收掉，否则它以 pending task destroyed 告警收场。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_ends():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield ": never\n\n"

    async def go():
        stream = with_heartbeat(never_ends(), interval=0.01)
        agen = stream.__aiter__()
        await agen.__anext__()  # 第一帧：等到下层开始挂起
        await agen.aclose()  # 相当于客户端断开

    asyncio.run(go())

    assert started.is_set() and cancelled.is_set()


def test_confirmed_action_is_written_back_even_when_the_answer_never_starts(env):
    """模型配置坏了：命令已经跑完，exec 帧必须先发出去，不能被续答的失败吞掉。"""
    action = proposal(env.db)
    config = env.db.scalar(select(ModelConfig).where(ModelConfig.owner_id == OWNER))
    config.last_test_ok = False
    env.db.commit()

    events = frames(env.client.post(f"/api/chat/actions/{action.id}/confirm").text)

    assert [name for name, _ in events] == ["exec", "error"]
    assert events[1][1]["code"] == 4004
    env.db.refresh(action)
    assert action.status == "executed"
