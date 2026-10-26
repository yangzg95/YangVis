"""手动对话压缩 HTTP 层契约：四个端点的信封、SSE 线格式、权限隔离。

服务层的切点/锚点语义在 ``test_chat_compaction.py`` 已钉死；这里只管接线才
暴露得出来的东西：

- 预览是恒 200 的 SSE（``compact_meta → token* → compact_done``），业务拒绝
  落成 ``error`` 帧而不是 HTTP 4xx——响应头在第一个帧之前就发出去了；
- 应用/取消/查状态走统一信封，``LookupError`` 一律 HTTP 404；
- 只有会话属主能碰这四个端点，别人眼里这个会话不存在。

SQLite 跑文件库，摘要模型在 ``build_chat_model`` 那一层打桩，不连服务商。
"""
from __future__ import annotations

import json
from itertools import count
from types import SimpleNamespace
from typing import List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import ChatConversation, ChatMessage, ModelConfig
from app.models.schemas import CurrentUser, ModelPurpose
from app.services.chat import memory as mem

OWNER = 1
OTHER = 2
_SEQ = count()


# ---- 打桩：流式摘要模型 ------------------------------------------------------


class FakeChatModel:
    """与 langchain 同形：``astream`` 是异步生成器函数，调用即得异步生成器。"""

    def __init__(self, reply: str = "这是压缩摘要：只保留未决事项。") -> None:
        self.reply = reply
        self.calls: list = []

    async def astream(self, messages):
        self.calls.append(messages)
        for i in range(0, len(self.reply), 2):
            yield SimpleNamespace(content=self.reply[i : i + 2])

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.reply)


# ---- 环境 -------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'compaction_api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", maker)
    db = maker()
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
    db.commit()

    model = FakeChatModel()
    monkeypatch.setattr(mem, "build_chat_model", lambda config, **kw: model)

    state = {"user": CurrentUser(user_id=OWNER, username="t", is_admin=True)}

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: state["user"]

    client = TestClient(app)
    try:
        yield SimpleNamespace(db=db, client=client, model=model, as_user=state.__setitem__)
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def make_conversation(db) -> ChatConversation:
    row = ChatConversation(owner_id=OWNER, title=f"压缩接口会-{next(_SEQ)}")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_pairs(db, conversation_id: int, pairs: int) -> List[ChatMessage]:
    rows: List[ChatMessage] = []
    for turn in range(pairs):
        for role in ("user", "assistant"):
            row = ChatMessage(
                owner_id=OWNER,
                conversation_id=conversation_id,
                role=role,
                content=f"第 {turn} 轮{'提问' if role == 'user' else '回答'}",
            )
            db.add(row)
            rows.append(row)
    db.commit()
    return rows


def frames(body: str) -> List[tuple]:
    """按前端同样的切帧规则还原 ``(event, payload)``，心跳/注释帧丢弃。"""
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


def preview(client, conversation_id: int, **payload):
    body = {"keep_turns": payload.pop("keep_turns", 1)}
    body.update(payload)
    return client.post(
        f"/api/chat/conversations/{conversation_id}/compaction/preview", json=body
    )


# ---- 一、查状态：没有摘要时 data 为 null ------------------------------------


def test_get_state_is_null_before_anything(env):
    conv = make_conversation(env.db)

    resp = env.client.get(f"/api/chat/conversations/{conv.id}/compaction")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] is None


def test_get_state_of_unknown_conversation_is_404(env):
    resp = env.client.get("/api/chat/conversations/999999/compaction")

    assert resp.status_code == 404
    assert resp.json()["code"] == 404


# ---- 二、预览 SSE：线格式、计数、不落库 --------------------------------------


def test_preview_streams_meta_tokens_done_and_persists_nothing(env):
    conv = make_conversation(env.db)
    add_pairs(env.db, conv.id, 4)

    resp = preview(env.client, conv.id, keep_turns=1, instruction="保留未决错误")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Nginx 默认攒完整个响应再吐，漏写这一条就是「本地流式、线上整段等待」。
    assert resp.headers["x-accel-buffering"] == "no"

    events = frames(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "compact_meta"
    assert names[-1] == "compact_done"
    assert "token" in names

    meta = events[0][1]
    assert meta["covered_count"] == 6, "8 条消息留最后一轮 2 条，摘要段 6 条"
    assert meta["kept_count"] == 2
    assert events[-1][1] == {"cut_message_id": meta["cut_message_id"]}
    assert "".join(data for name, data in events if name == "token") == env.model.reply

    # 用户的压缩要求要进提示词；预览全程不写库。
    assert "保留未决错误" in env.model.calls[0][1].content
    assert env.client.get(f"/api/chat/conversations/{conv.id}/compaction").json()[
        "data"
    ] is None


def test_preview_with_nothing_to_compact_is_an_error_frame(env):
    """没有老化消息时拒绝压缩：恒 200 的 error 帧，不是 HTTP 4xx。"""
    conv = make_conversation(env.db)
    add_pairs(env.db, conv.id, 2)

    resp = preview(env.client, conv.id, keep_turns=6)

    assert resp.status_code == 200
    assert frames(resp.text) == [
        ("error", {"code": 400, "message": "更早的对话都还在回放窗口内，没有需要压缩的内容。"})
    ]


def test_preview_with_unusable_model_is_an_error_frame(env):
    """模型没测通（require_chat_config 抛业务错）也要收尾成一帧，不能断在半空。"""
    conv = make_conversation(env.db)
    add_pairs(env.db, conv.id, 4)
    config = env.db.query(ModelConfig).one()
    config.last_test_ok = False
    env.db.commit()

    resp = preview(env.client, conv.id)

    events = frames(resp.text)
    assert resp.status_code == 200
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["code"] != 0


def test_preview_validates_keep_turns_range(env):
    conv = make_conversation(env.db)

    assert preview(env.client, conv.id, keep_turns=0).status_code == 422
    assert preview(env.client, conv.id, keep_turns=21).status_code == 422
    assert preview(
        env.client, conv.id, instruction="压" * 501
    ).status_code == 422


# ---- 三、应用：信封成功，非法切点被拒 ----------------------------------------


def test_apply_returns_the_new_state_and_get_reflects_it(env):
    conv = make_conversation(env.db)
    add_pairs(env.db, conv.id, 4)

    meta = frames(preview(env.client, conv.id).text)[0][1]
    summary = "".join(
        data
        for name, data in frames(preview(env.client, conv.id).text)
        if name == "token"
    )

    resp = env.client.post(
        f"/api/chat/conversations/{conv.id}/compaction/apply",
        json={
            "cut_message_id": meta["cut_message_id"],
            "summary": summary,
            "instruction": "  保留未决错误  ",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == "applied"
    data = body["data"]
    assert data["cut_message_id"] == meta["cut_message_id"]
    assert data["covered_count"] == 6
    assert data["instruction"] == "保留未决错误"
    assert data["summary"] == summary
    assert data["updated_at"]

    fetched = env.client.get(f"/api/chat/conversations/{conv.id}/compaction").json()
    assert fetched["data"]["cut_message_id"] == meta["cut_message_id"]


def test_apply_with_unknown_cut_is_404(env):
    conv = make_conversation(env.db)
    add_pairs(env.db, conv.id, 4)

    resp = env.client.post(
        f"/api/chat/conversations/{conv.id}/compaction/apply",
        json={"cut_message_id": 999999, "summary": "摘要"},
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == 404


def test_apply_that_leaves_no_replay_returns_code_minus_one(env):
    """切点压到最后一条消息：HTTP 200 + code=-1，业务错误不走异常状态码。"""
    conv = make_conversation(env.db)
    rows = add_pairs(env.db, conv.id, 4)

    resp = env.client.post(
        f"/api/chat/conversations/{conv.id}/compaction/apply",
        json={"cut_message_id": rows[-1].id, "summary": "摘要"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == -1
    assert body["data"] is None


def test_apply_validates_summary_length(env):
    conv = make_conversation(env.db)
    add_pairs(env.db, conv.id, 4)
    meta = frames(preview(env.client, conv.id).text)[0][1]

    # min_length 不剥空白：空白串能过 schema，但服务层 strip 后判空 → code=-1。
    empty = env.client.post(
        f"/api/chat/conversations/{conv.id}/compaction/apply",
        json={"cut_message_id": meta["cut_message_id"], "summary": "   "},
    )
    too_long = env.client.post(
        f"/api/chat/conversations/{conv.id}/compaction/apply",
        json={"cut_message_id": meta["cut_message_id"], "summary": "摘" * 2001},
    )

    assert empty.status_code == 200
    assert empty.json()["code"] == -1
    assert too_long.status_code == 422


# ---- 四、取消：幂等，删摘要不删消息 ------------------------------------------


def test_clear_is_idempotent_and_goes_back_to_null_state(env):
    conv = make_conversation(env.db)
    add_pairs(env.db, conv.id, 4)
    meta = frames(preview(env.client, conv.id).text)[0][1]
    env.client.post(
        f"/api/chat/conversations/{conv.id}/compaction/apply",
        json={"cut_message_id": meta["cut_message_id"], "summary": "摘要"},
    )

    first = env.client.delete(f"/api/chat/conversations/{conv.id}/compaction")
    second = env.client.delete(f"/api/chat/conversations/{conv.id}/compaction")

    assert first.json()["message"] == "cleared"
    assert second.json()["code"] == 0, "本来就没有压缩行也算取消成功"
    assert env.client.get(f"/api/chat/conversations/{conv.id}/compaction").json()[
        "data"
    ] is None
    assert env.db.query(ChatMessage).count() == 8, "取消压缩不删消息原文"


# ---- 五、属主隔离：别人眼里会话不存在 ----------------------------------------


def test_other_owner_cannot_touch_compaction(env):
    conv = make_conversation(env.db)
    add_pairs(env.db, conv.id, 4)
    env.as_user("user", CurrentUser(user_id=OTHER, username="other"))

    assert env.client.get(f"/api/chat/conversations/{conv.id}/compaction").status_code == 404
    assert env.client.delete(f"/api/chat/conversations/{conv.id}/compaction").status_code == 404

    apply_resp = env.client.post(
        f"/api/chat/conversations/{conv.id}/compaction/apply",
        json={"cut_message_id": 1, "summary": "摘要"},
    )
    assert apply_resp.status_code == 404

    # SSE 恒 200：越权落成 error 帧，code 仍是 404。
    preview_resp = preview(env.client, conv.id)
    assert preview_resp.status_code == 200
    assert frames(preview_resp.text) == [
        ("error", {"code": 404, "message": "会话不存在"})
    ]
