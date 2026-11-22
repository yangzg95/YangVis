"""创作模块 HTTP 链路：统一信封、列表/详情字段分离、422、跨账号 404、导出响应头。

service 层的清洗/对账细节在 test_writing.py 钉过，这里只钉 Web 边界：
路径、信封结构、状态码与响应头，以及业务拒绝走 ``code=-1`` 而不是抛 500。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.schemas import CurrentUser

OWNER = 1
OTHER = 2


# ---- 环境 -------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'writing_api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    # 生成接口的 BackgroundTasks 自开 session：指到同一文件库，请求与任务才看得见彼此；
    # M1 用例没有后台任务，这个替换对它们是空操作。
    monkeypatch.setattr("app.database.SessionLocal", maker)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client_for(db):
    """同 test_note_api：依赖覆盖是 app 全局字典，用例里不要交错身份发请求。"""

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db

    def make_client(owner_id: int) -> TestClient:
        app.dependency_overrides[require_user] = lambda: CurrentUser(
            user_id=owner_id, username=f"user{owner_id}"
        )
        return TestClient(app)

    try:
        yield make_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def client(client_for):
    return client_for(OWNER)


def _create(client: TestClient, kind: str, title: str) -> dict:
    return client.post(
        "/api/studio/works", json={"kind": kind, "title": title}
    ).json()["data"]


# ---- 列表与详情 ---------------------------------------------------------------


def test_list_empty_envelope(client):
    body = client.get("/api/studio/works").json()
    assert body["code"] == 0
    assert body["data"] == {"items": [], "total": 0}


def test_create_and_kind_filter_and_list_shape(client):
    novel = _create(client, "novel", "星河彼岸")
    article = _create(client, "article", "增长手记")

    rows = client.get("/api/studio/works").json()["data"]
    assert rows["total"] == 2

    novel_rows = client.get("/api/studio/works", params={"kind": "novel"}).json()["data"]
    assert [item["id"] for item in novel_rows["items"]] == [novel["id"]]
    article_rows = client.get("/api/studio/works", params={"kind": "article"}).json()["data"]
    assert [item["id"] for item in article_rows["items"]] == [article["id"]]

    row = novel_rows["items"][0]
    assert row["part_count"] == 0 and row["word_count"] == 0
    # 列表行不拖长字段：brief/outline/parts 只在详情里出现。
    assert "brief" not in row and "outline" not in row and "parts" not in row


def test_detail_contains_outline_and_parts(client):
    work = _create(client, "novel", "雨夜书")
    detail = client.get(f"/api/studio/works/{work['id']}").json()["data"]
    assert detail["outline"] == [] and detail["parts"] == []
    assert detail["brief"] == "" and detail["meta"] == {}


def test_blank_title_and_unknown_kind_are_422(client):
    assert client.post(
        "/api/studio/works", json={"kind": "novel", "title": "   "}
    ).status_code == 422
    assert client.post(
        "/api/studio/works", json={"kind": "poetry", "title": "诗集"}
    ).status_code == 422


# ---- 大纲与正文块链路 ----------------------------------------------------------


def test_outline_save_part_autosave_and_delete_http(client):
    work = _create(client, "novel", "我的书")
    url = f"/api/studio/works/{work['id']}"

    saved = client.put(
        f"{url}/outline",
        json={"nodes": [
            {"kind": "chapter", "title": "第一章"},
            {"kind": "chapter", "title": "第二章"},
        ]},
    ).json()["data"]
    assert saved["part_count"] == 2
    cid = saved["parts"][0]["cid"]
    assert cid.startswith("p")

    # 正文自动保存：字数服务端统计，改名回写大纲叶子。
    detail = client.put(
        f"{url}/parts/{cid}",
        json={"title": "第一章 雨夜", "content": "少年策马下山，city 不相信眼泪。"},
    ).json()["data"]
    part = next(item for item in detail["parts"] if item["cid"] == cid)
    # CJK 11（少年策马下山 + 不相信眼泪）+ 拉丁词 city 1 = 12。
    assert part["title"] == "第一章 雨夜" and part["word_count"] == 12
    assert detail["outline"][0]["title"] == "第一章 雨夜"

    after_delete = client.delete(f"{url}/parts/{cid}").json()["data"]
    assert after_delete["part_count"] == 1
    assert all(item["cid"] != cid for item in after_delete["parts"])


def test_part_cap_returns_business_envelope(client, monkeypatch):
    work = _create(client, "novel", "受限的书")
    monkeypatch.setattr(get_settings(), "WRITING_MAX_PARTS_PER_WORK", 1)
    response = client.put(
        f"/api/studio/works/{work['id']}/outline",
        json={"nodes": [
            {"kind": "chapter", "title": "第一章"},
            {"kind": "chapter", "title": "第二章"},
        ]},
    )
    # 业务拒绝保持 HTTP 200，靠信封里的非零 code 表达，前端走提示而不是错误页。
    body = response.json()
    assert body["code"] == -1
    assert "最多" in body["message"]


# ---- 隔离 ---------------------------------------------------------------------


def test_other_owner_gets_404(client_for):
    owner = client_for(OWNER)
    work = _create(owner, "novel", "私密书稿")
    cid = owner.put(
        f"/api/studio/works/{work['id']}/outline",
        json={"nodes": [{"kind": "chapter", "title": "第一章"}]},
    ).json()["data"]["parts"][0]["cid"]

    other = client_for(OTHER)
    assert other.get(f"/api/studio/works/{work['id']}").status_code == 404
    assert other.get("/api/studio/works").json()["data"]["total"] == 0
    assert other.put(
        f"/api/studio/works/{work['id']}/parts/{cid}", json={"content": "偷改"}
    ).status_code == 404
    assert other.get(
        f"/api/studio/works/{work['id']}/export", params={"format": "md"}
    ).status_code == 404
    assert other.delete(f"/api/studio/works/{work['id']}").status_code == 404


# ---- 导出 ---------------------------------------------------------------------


def test_export_markdown_and_text_headers(client):
    work = _create(client, "novel", "我的书")
    url = f"/api/studio/works/{work['id']}"
    cid = client.put(
        f"{url}/outline", json={"nodes": [{"kind": "chapter", "title": "第一章"}]}
    ).json()["data"]["parts"][0]["cid"]
    client.put(f"{url}/parts/{cid}", json={"content": "第一章正文。"})

    md = client.get(f"{url}/export", params={"format": "md"})
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.headers["x-content-type-options"] == "nosniff"
    assert md.headers["cache-control"] == "no-store"
    disposition = md.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition and disposition.endswith(".md")
    assert "# 我的书" in md.text and "第一章正文。" in md.text

    txt = client.get(f"{url}/export", params={"format": "txt"})
    assert txt.headers["content-type"].startswith("text/plain")
    assert txt.headers["content-disposition"].endswith(".txt")
    assert "#" not in txt.text and "第一章正文。" in txt.text


# ---- 删除 ---------------------------------------------------------------------


def test_delete_work_http(client):
    work = _create(client, "article", "待删稿")
    message = client.delete(f"/api/studio/works/{work['id']}").json()["message"]
    assert message == "deleted"
    assert client.get(f"/api/studio/works/{work['id']}").status_code == 404


# ---- M2：后台大纲生成的 HTTP 链路 ----------------------------------------------


NOVEL_OUTPUT = (
    '{"outline":[{"title":"第一卷 出山","chapters":['
    '{"title":"第一章 雨夜下山","synopsis":"少年奉师命下山","target_words":3000},'
    '{"title":"第二章 初入州城","synopsis":"城里的第一夜","target_words":2500}]},'
    '{"title":"第三章 独行","synopsis":"顶层章","target_words":2000}]}'
)

ARTICLE_OUTPUT = (
    '{"title_candidates":["标题甲","标题乙"],'
    '"sections":[{"title":"开篇","point":"立论","target_words":800}]}'
)


@pytest.fixture()
def generation_env(db, monkeypatch):
    """生成接口专用环境：seed 内置工作流 + 一份测通的对话模型 + 可换装的假模型调用。"""
    from app.models.entities import ModelConfig
    from app.models.schemas import ModelPurpose
    from app.services.notify import app_inbox
    from app.services.workflows import exec as workflow_exec
    from app.services.workflows.builtin import seed_builtin_workflows

    # 站内信锁可能绑在别的用例的事件循环上，清掉避免噪音日志。
    app_inbox._owner_locks.clear()
    seed_builtin_workflows(db)
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

    def install(reply):
        async def fake_run_agent(
            session,
            owner_id,
            agent_slug,
            user_prompt,
            *,
            agent=None,
            config_id=None,
            timeout=None,
            tools=None,
            on_event=None,
        ):
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(workflow_exec, "run_agent", fake_run_agent)

    return install


def _fresh(db):
    """后台任务在另一条连接上提交：本会话先结束旧事务再读，否则拿旧快照。"""
    db.rollback()


def test_generate_outline_returns_analyzing_then_ready(client, db, generation_env):
    from app.services.workflows import SUBJECT_ANALYZING, SUBJECT_READY

    generation_env(NOVEL_OUTPUT)
    work = _create(client, "novel", "雨夜书")

    # TestClient 会在响应返回后把 BackgroundTasks 跑完，但响应体本身是排队时序列化的。
    resp = client.post(f"/api/studio/works/{work['id']}/outline/generate", json={})
    body = resp.json()
    assert body["code"] == 0 and body["message"] == "已开始生成大纲"
    assert body["data"]["status"] == SUBJECT_ANALYZING

    detail = client.get(f"/api/studio/works/{work['id']}").json()["data"]
    assert detail["status"] == SUBJECT_READY and detail["error_msg"] is None
    assert detail["part_count"] == 3
    assert detail["outline"][0]["kind"] == "volume"
    assert [c["title"] for c in detail["outline"][0]["children"]] == [
        "第一章 雨夜下山",
        "第二章 初入州城",
    ]


def test_generate_article_outline_records_title_candidates(client, generation_env):
    generation_env(ARTICLE_OUTPUT)
    work = _create(client, "article", "增长手记")

    client.post(f"/api/studio/works/{work['id']}/outline/generate", json={})
    detail = client.get(f"/api/studio/works/{work['id']}").json()["data"]
    assert [node["kind"] for node in detail["outline"]] == ["section"]
    assert detail["outline"][0]["synopsis"] == "立论"
    assert detail["meta"]["title_candidates"] == ["标题甲", "标题乙"]


def test_generate_failure_surfaces_error_status(client, generation_env):
    from app.services.workflows import SUBJECT_ERROR

    generation_env(RuntimeError("上游 502"))
    work = _create(client, "novel", "雨夜书")

    client.post(f"/api/studio/works/{work['id']}/outline/generate", json={})
    detail = client.get(f"/api/studio/works/{work['id']}").json()["data"]
    assert detail["status"] == SUBJECT_ERROR and "上游 502" in (detail["error_msg"] or "")


def test_generate_without_chat_model_is_4004(client, db):
    from app.errors import CODE_CHAT_NOT_READY
    from app.models.entities import WorkflowRun
    from sqlalchemy import select as sa_select

    work = _create(client, "novel", "没配模型的书")
    body = client.post(
        f"/api/studio/works/{work['id']}/outline/generate", json={}
    ).json()
    # 全局 BusinessError 处理器：HTTP 200 + 信封 code=4004，驱动前端的「去配置」引导。
    assert body["code"] == CODE_CHAT_NOT_READY and body["data"] is None
    _fresh(db)
    assert db.scalar(sa_select(WorkflowRun)) is None
    assert client.get(f"/api/studio/works/{work['id']}").json()["data"]["status"] == "uploaded"


def test_generate_unknown_work_is_404(client, generation_env):
    generation_env(NOVEL_OUTPUT)
    assert client.post("/api/studio/works/999999/outline/generate", json={}).status_code == 404


def test_generate_other_owner_gets_404(client_for, generation_env):
    generation_env(NOVEL_OUTPUT)
    owner = client_for(OWNER)
    work = _create(owner, "novel", "私密书稿")
    other = client_for(OTHER)
    assert other.post(
        f"/api/studio/works/{work['id']}/outline/generate", json={}
    ).status_code == 404


def test_running_gates_return_business_envelope(client, db, generation_env):
    from app.services.writing import WritingService

    generation_env(NOVEL_OUTPUT)
    work = _create(client, "novel", "写作中的书")
    # 直接调 service 建一条运行中的 run（不经过 HTTP，也就没有后台任务执行它）。
    WritingService(db, OWNER).start_outline_generation(work["id"])

    again = client.post(f"/api/studio/works/{work['id']}/outline/generate", json={}).json()
    assert again["code"] == -1 and "重复触发" in again["message"]
    delete = client.delete(f"/api/studio/works/{work['id']}").json()
    assert delete["code"] == -1 and "正在生成" in delete["message"]


def test_generate_payload_validation_is_422(client, generation_env):
    generation_env(NOVEL_OUTPUT)
    work = _create(client, "novel", "雨夜书")
    url = f"/api/studio/works/{work['id']}/outline/generate"
    assert client.post(url, json={"model_config_id": 0}).status_code == 422
    assert client.post(url, json={"requirement": "x" * 513}).status_code == 422


def test_generate_with_chosen_model_stamps_steps(client, db, generation_env):
    from app.models.entities import ModelConfig, WorkflowRun, WorkflowRunStep
    from app.models.schemas import ModelPurpose
    from sqlalchemy import select as sa_select

    generation_env(NOVEL_OUTPUT)
    chosen = ModelConfig(
        owner_id=OWNER,
        purpose=ModelPurpose.CHAT.value,
        title="另一个对话模型",
        model_name="unit-test-pro",
        base_url="http://localhost:2/v1",
        is_default=False,
        last_test_ok=True,
    )
    db.add(chosen)
    db.commit()
    work = _create(client, "novel", "雨夜书")

    ok = client.post(
        f"/api/studio/works/{work['id']}/outline/generate",
        json={"model_config_id": chosen.id},
    ).json()
    assert ok["code"] == 0
    _fresh(db)
    run = db.scalar(sa_select(WorkflowRun))
    steps = list(db.scalars(
        sa_select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
    ).all())
    assert steps and all(step.model_config_id == chosen.id for step in steps)

    # 别人的模型 id 读作不存在：LookupError → 404，而不是越权使用。
    stranger = ModelConfig(
        owner_id=OTHER,
        purpose=ModelPurpose.CHAT.value,
        title="外人的模型",
        model_name="stranger",
        base_url="http://localhost:3/v1",
        last_test_ok=True,
    )
    db.add(stranger)
    db.commit()
    assert client.post(
        f"/api/studio/works/{work['id']}/outline/generate",
        json={"model_config_id": stranger.id},
    ).status_code == 404


# ---- M3：正文生成的 HTTP 链路 ---------------------------------------------------


def _outline_cids(client, work_id: int, *, kind: str) -> list[str]:
    node_kind = "chapter" if kind == "novel" else "section"
    detail = client.put(
        f"/api/studio/works/{work_id}/outline",
        json={"nodes": [
            {"kind": node_kind, "title": "第一段"},
            {"kind": node_kind, "title": "第二段"},
        ]},
    ).json()["data"]
    return [part["cid"] for part in detail["parts"]]


def test_part_generate_returns_analyzing_then_writes_content(client, generation_env):
    generation_env("夜色压着山脊，少年一步踏入雨幕。" * 10)
    work = _create(client, "novel", "雨夜书")
    cid = _outline_cids(client, work["id"], kind="novel")[0]

    resp = client.post(
        f"/api/studio/works/{work['id']}/parts/{cid}/generate",
        json={"action": "draft", "instruction": " 节奏快一点 ", "model_config_id": None},
    ).json()
    assert resp["code"] == 0 and resp["message"] == "已开始生成"
    # 响应体是排队瞬间的快照：该块 analyzing，别的块仍是 idle。
    parts = {part["cid"]: part for part in resp["data"]["parts"]}
    assert parts[cid]["status"] == "analyzing"

    detail = client.get(f"/api/studio/works/{work['id']}").json()["data"]
    target = next(part for part in detail["parts"] if part["cid"] == cid)
    assert target["status"] == "ready" and target["error_msg"] is None
    assert target["word_count"] > 0 and "少年一步踏入雨幕" in target["content"]


def test_part_generate_business_rejections_are_envelopes(client, generation_env):
    generation_env("成稿")
    novel = _create(client, "novel", "我的书")
    novel_cid = _outline_cids(client, novel["id"], kind="novel")[0]
    article = _create(client, "article", "一篇文")
    article_cid = _outline_cids(client, article["id"], kind="article")[0]

    def post(work_id, cid, payload):
        return client.post(
            f"/api/studio/works/{work_id}/parts/{cid}/generate", json=payload
        ).json()

    # 文章不支持起草；空块不能编辑加工；换语气缺 tone；未知 cid 是 404 而不是信封。
    assert post(article["id"], article_cid, {"action": "draft"})["code"] == -1
    assert post(novel["id"], novel_cid, {"action": "polish"})["code"] == -1
    assert post(novel["id"], novel_cid, {"action": "retone"})["code"] == -1
    assert client.post(
        f"/api/studio/works/{novel['id']}/parts/nope/generate", json={"action": "draft"}
    ).status_code == 404


def test_part_generate_payload_validation_is_422(client, generation_env):
    generation_env("成稿")
    work = _create(client, "novel", "我的书")
    cid = _outline_cids(client, work["id"], kind="novel")[0]
    url = f"/api/studio/works/{work['id']}/parts/{cid}/generate"
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json={"action": "x" * 33}).status_code == 422
    assert client.post(url, json={"action": "draft", "instruction": "x" * 513}).status_code == 422
    assert client.post(url, json={"action": "retone", "tone": "x" * 65}).status_code == 422
    assert client.post(url, json={"action": "draft", "model_config_id": 0}).status_code == 422


def test_part_generate_without_chat_model_is_4004(client, db):
    from app.errors import CODE_CHAT_NOT_READY
    from app.models.entities import WorkflowRun
    from sqlalchemy import select as sa_select

    work = _create(client, "novel", "没配模型的书")
    cid = _outline_cids(client, work["id"], kind="novel")[0]
    body = client.post(
        f"/api/studio/works/{work['id']}/parts/{cid}/generate", json={"action": "draft"}
    ).json()
    assert body["code"] == CODE_CHAT_NOT_READY and body["data"] is None
    _fresh(db)
    assert db.scalar(sa_select(WorkflowRun)) is None


def test_part_generate_other_owner_gets_404(client_for, generation_env):
    generation_env("成稿")
    owner = client_for(OWNER)
    work = _create(owner, "novel", "私密书稿")
    cid = _outline_cids(owner, work["id"], kind="novel")[0]
    other = client_for(OTHER)
    assert other.post(
        f"/api/studio/works/{work['id']}/parts/{cid}/generate", json={"action": "draft"}
    ).status_code == 404


def test_article_draft_generate_splits_and_warns_over_http(client, generation_env):
    generation_env("## 开篇\n\n只有一段标题的成稿，模型漏了第二段。")
    work = _create(client, "article", "一篇文")
    cids = _outline_cids(client, work["id"], kind="article")

    resp = client.post(
        f"/api/studio/works/{work['id']}/draft/generate", json={}
    ).json()
    assert resp["code"] == 0 and resp["data"]["status"] == "analyzing"

    detail = client.get(f"/api/studio/works/{work['id']}").json()["data"]
    first = next(part for part in detail["parts"] if part["cid"] == cids[0])
    second = next(part for part in detail["parts"] if part["cid"] == cids[1])
    assert "模型漏了第二段" in first["content"] and second["content"] == ""
    assert "对不上" in detail["meta"]["draft_warning"]


def test_draft_generate_rejections(client, generation_env):
    generation_env("成稿")
    # 小说没有整篇起草；没有大纲的文章要求先生成大纲。
    novel = _create(client, "novel", "我的书")
    _outline_cids(client, novel["id"], kind="novel")
    assert client.post(
        f"/api/studio/works/{novel['id']}/draft/generate", json={}
    ).json()["code"] == -1

    bare = _create(client, "article", "空文")
    assert client.post(
        f"/api/studio/works/{bare['id']}/draft/generate", json={}
    ).json()["code"] == -1
    assert client.post("/api/studio/works/999999/draft/generate", json={}).status_code == 404
    assert client.post(
        f"/api/studio/works/{bare['id']}/draft/generate", json={"model_config_id": 0}
    ).status_code == 422


def test_draft_generate_without_chat_model_is_4004(client, db):
    from app.errors import CODE_CHAT_NOT_READY

    work = _create(client, "article", "没配模型的文")
    _outline_cids(client, work["id"], kind="article")
    body = client.post(
        f"/api/studio/works/{work['id']}/draft/generate", json={}
    ).json()
    assert body["code"] == CODE_CHAT_NOT_READY


def test_draft_generate_other_owner_gets_404(client_for, generation_env):
    generation_env("成稿")
    owner = client_for(OWNER)
    work = _create(owner, "article", "私密文章")
    _outline_cids(owner, work["id"], kind="article")
    other = client_for(OTHER)
    assert other.post(
        f"/api/studio/works/{work['id']}/draft/generate", json={}
    ).status_code == 404


# ---- 块级运行的 part_running 标志（列表删除闸门的后端事实源）----------------------


def test_part_running_flag_in_queue_snapshot_then_clears(client, generation_env):
    generation_env("夜色压着山脊，少年一步踏入雨幕。" * 10)
    work = _create(client, "novel", "雨夜书")
    cid = _outline_cids(client, work["id"], kind="novel")[0]

    resp = client.post(
        f"/api/studio/works/{work['id']}/parts/{cid}/generate",
        json={"action": "draft"},
    ).json()
    # 响应体是排队瞬间的 to_detail 快照：块级运行已建，part_running 立刻置位。
    assert resp["code"] == 0 and resp["data"]["part_running"] is True
    target = next(part for part in resp["data"]["parts"] if part["cid"] == cid)
    assert target["status"] == "analyzing"

    # TestClient 返回前 BackgroundTasks 已跑完：列表（批量路径）与详情都回落到 False。
    listed = client.get("/api/studio/works", params={"kind": "novel"}).json()["data"]["items"]
    assert next(item for item in listed if item["id"] == work["id"])["part_running"] is False
    detail = client.get(f"/api/studio/works/{work['id']}").json()["data"]
    assert detail["part_running"] is False


def test_list_part_running_flag_and_delete_gate_while_part_in_flight(
    client, client_for, db, generation_env, monkeypatch
):
    import threading
    import time

    from sqlalchemy import select as sa_select

    from app.database import SessionLocal
    from app.models.entities import WorkflowRun
    from app.services.workflows import RUN_RUNNING, SUBJECT_WRITING_PART
    from app.services.workflows import exec as workflow_exec

    # 先借 fixture 完成内置工作流/模型 seed，再把 run_agent 换成会堵在闸门上的假调用。
    generation_env("占位回复")
    gate = threading.Event()

    async def blocking_run_agent(
        session,
        owner_id,
        agent_slug,
        user_prompt,
        *,
        agent=None,
        config_id=None,
        timeout=None,
        tools=None,
        on_event=None,
    ):
        # 执行器在调用前已 commit，阻塞期间不持 sqlite 写锁，第二条连接照样能读。
        gate.wait(timeout=15)
        return "闸门放行后的成稿。"

    monkeypatch.setattr(workflow_exec, "run_agent", blocking_run_agent)

    work = _create(client, "novel", "在写的书")
    cid = _outline_cids(client, work["id"], kind="novel")[0]
    url = f"/api/studio/works/{work['id']}/parts/{cid}/generate"

    response_holder: dict = {}

    def fire() -> None:
        response_holder["resp"] = client.post(url, json={"action": "draft"})

    worker = threading.Thread(target=fire)
    worker.start()
    # 轮询专用连接：请求阶段与 viewer 请求都复用 fixture 会话 db，主线程不能在
    # 它们事务进行中 rollback 同一个会话，否则会把人家的事务关掉。
    inspector = SessionLocal()
    try:
        # 只认 RUN_RUNNING：它由后台会话提交（exec 在调模型前 commit），
        # 看到它就说明请求阶段已结束、共享会话 db 已空闲，POST 正堵在闸门上。
        deadline = time.monotonic() + 10
        while True:
            run = inspector.scalar(
                sa_select(WorkflowRun).where(
                    WorkflowRun.subject_kind == SUBJECT_WRITING_PART
                )
            )
            # sqlite 读事务会冻结快照：每轮结束事务，下一轮 SELECT 才看得见后台的提交。
            inspector.rollback()
            if run is not None and run.status == RUN_RUNNING:
                break
            if time.monotonic() > deadline:
                raise AssertionError("后台块级任务没有进入运行态")
            time.sleep(0.02)
        # 及早关连接：别让这里的读事务挡住放行后后台任务的写提交（sqlite 写锁）。
        inspector.close()

        # 第二个 TestClient 走独立 portal，复用全局依赖覆盖（仍是同一个 owner）。
        viewer = client_for(OWNER)
        listed = viewer.get("/api/studio/works", params={"kind": "novel"}).json()["data"]["items"]
        row = next(item for item in listed if item["id"] == work["id"])
        assert row["part_running"] is True
        # 作品级状态只翻译 writing-work 运行：块级在跑不该把列表行也染成「生成中」。
        # 没有作品级运行、大纲已存在，按 subject_state 仍是 ready。
        assert row["status"] == "ready"
        detail = viewer.get(f"/api/studio/works/{work['id']}").json()["data"]
        assert detail["part_running"] is True

        # 删除闸门：前端置灰之外后端同一道墙，返回业务信封而不是 500。
        blocked = viewer.delete(f"/api/studio/works/{work['id']}").json()
        assert blocked["code"] == -1 and "生成" in blocked["message"]
    finally:
        gate.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        inspector.close()  # 正常路径已关过一次，close 幂等，这里兜断言失败的分支。

    # 排队快照与放行后的回落各钉一次。
    resp = response_holder["resp"].json()
    assert resp["code"] == 0 and resp["data"]["part_running"] is True
    db.rollback()
    listed = client.get("/api/studio/works", params={"kind": "novel"}).json()["data"]["items"]
    assert next(item for item in listed if item["id"] == work["id"])["part_running"] is False
