"""在线简历编辑器：文档存取、保存归一化、富文本消毒与 AI prompt 规则。

与 ``test_resume_service.py`` 的分界：那条线测上传式简历（PDF/Word → 工作流分析），
这里测用户在页面上逐模块搭建的 ``resume_doc`` 单表 JSON 文档。

- 模型流式调用不发真实请求：成功路径用假模型 monkeypatch 掉 ``build_chat_model``，
  只验证 token 搬运（含结构化 content 块、断连中止）与未配置模型的门槛。
- SQLite 文件库，内置人设 seed 进库（ai_stream 按 slug 认领）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.errors import CODE_CHAT_NOT_READY, BusinessError
from app.models.entities import ModelConfig, ResumeDoc
from app.models.schemas import ResumeDocAiRequest
from app.services.agents import seed_builtin_agents
from app.services.resume_builder import (
    DEFAULT_MODULE_ORDER,
    MODULES,
    ResumeBuilderService,
    build_assist_prompt,
    build_check_prompt,
    default_doc,
    html_to_text,
    normalize_doc,
    sanitize_html,
)

OWNER = 1
OTHER = 2


# ---- 环境 -------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'resume_builder.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    monkeypatch.setattr("app.database.SessionLocal", maker)
    # ai_stream 靠 slug 认领内置人设，缺失会被当成初始化损坏。
    seed_builtin_agents(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def service(session, owner_id: int = OWNER) -> ResumeBuilderService:
    return ResumeBuilderService(session, owner_id)


def seed_chat_model(session, owner_id: int = OWNER) -> ModelConfig:
    config = ModelConfig(
        owner_id=owner_id,
        purpose="chat",
        title="测试对话模型",
        model_name="unit-test",
        base_url="http://localhost:1/v1",
        is_default=True,
        last_test_ok=True,
    )
    session.add(config)
    session.commit()
    return config


def collect(agen) -> list[str]:
    async def _run() -> list[str]:
        out: list[str] = []
        async for token in agen:
            out.append(token)
        return out

    return asyncio.run(_run())


# ---- 一、默认骨架与 CRUD ------------------------------------------------------


def test_create_doc_stores_default_skeleton(db):
    row = service(db).create_doc("我的简历")
    detail = service(db).to_detail(row)
    assert detail["title"] == "我的简历"
    assert detail["doc"] == default_doc()
    assert detail["doc"]["module_order"] == list(DEFAULT_MODULE_ORDER)
    # 列表项不带 doc 大字段。
    item = service(db).to_item(row)
    assert "doc" not in item and item["title"] == "我的简历"


def test_blank_title_falls_back(db):
    assert service(db).create_doc("   ").title == "未命名简历"


def test_list_orders_by_recency_and_is_owner_scoped(db):
    first = service(db).create_doc("第一份")
    second = service(db).create_doc("第二份")

    # updated_at 倒序（同秒时由 id 倒序兜底），第二份必定排在前面。
    mine = service(db).list_docs()
    assert [row.id for row in mine] == [second.id, first.id]
    # 列表序列化不带 doc 大字段（to_item 单独钉过，这里再钉一遍调用点）。
    assert all("doc" not in service(db).to_item(row) for row in mine)
    assert service(db, OTHER).list_docs() == []


def test_foreign_owner_only_sees_not_found(db):
    row = service(db).create_doc("我的")
    with pytest.raises(LookupError):
        service(db, OTHER).get_doc(row.id)
    with pytest.raises(LookupError):
        service(db, OTHER).update_doc(row.id, title="x", doc=None)
    with pytest.raises(LookupError):
        service(db, OTHER).delete_doc(row.id)
    # 别人的失败操作没有碰到这条文档。
    assert service(db).get_doc(row.id).title == "我的"


def test_delete_removes_doc(db):
    row = service(db).create_doc("待删")
    service(db).delete_doc(row.id)
    with pytest.raises(LookupError):
        service(db).get_doc(row.id)


def test_update_title_ignores_blank(db):
    row = service(db).create_doc("原标题")
    service(db).update_doc(row.id, title="   ", doc=None)
    assert service(db).get_doc(row.id).title == "原标题"


def test_legacy_row_with_missing_keys_is_repaired_on_read(db):
    # 模拟早期版本/手改库：只有 template，其余键全缺。
    row = ResumeDoc(owner_id=OWNER, title="老简历", doc={"template": "classic"})
    db.add(row)
    db.commit()
    detail = service(db).to_detail(service(db).get_doc(row.id))
    assert detail["doc"] == default_doc()


# ---- 二、normalize_doc：保存时的唯一收口 --------------------------------------


def test_normalize_fills_every_missing_key():
    assert normalize_doc(None) == default_doc()
    assert normalize_doc("nope") == default_doc()
    assert normalize_doc({}) == default_doc()


def test_normalize_keeps_a_valid_permutation_and_rejects_bad_order():
    reversed_order = list(reversed(MODULES))
    doc = normalize_doc({"module_order": reversed_order})
    assert doc["module_order"] == reversed_order

    # 缺键、多键、重复、元素类型不对都退回默认顺序。
    for bad in (
        ["profile", "companies", "projects", "educations"],
        [*MODULES, "skills"],
        ["profile"] * 5,
        ["profile", "companies", "projects", "educations", 3],
        "profile,companies",
    ):
        assert normalize_doc({"module_order": bad})["module_order"] == list(DEFAULT_MODULE_ORDER)


def test_normalize_template_is_whitelisted():
    assert normalize_doc({"template": "classic"})["template"] == "classic"
    assert normalize_doc({"template": "sidebar"})["template"] == "sidebar"
    assert normalize_doc({"template": "minimal"})["template"] == "minimal"
    assert normalize_doc({"template": "future-shiny"})["template"] == "classic"


def test_normalize_theme_is_whitelisted():
    assert normalize_doc({})["theme"] == "teal"
    assert normalize_doc({"theme": "navy"})["theme"] == "navy"
    assert normalize_doc({"theme": "hotpink"})["theme"] == "teal"
    assert normalize_doc({"theme": 7})["theme"] == "teal"


def test_normalize_profile_keeps_website_and_intro():
    doc = normalize_doc(
        {"profile": {"website": "https://me.dev", "intro": "8 年 Java 后端", "name": 123}}
    )
    profile = doc["profile"]
    assert profile["website"] == "https://me.dev"
    assert profile["intro"] == "8 年 Java 后端"
    # 旧文档缺键会补齐。
    assert normalize_doc({})["profile"]["website"] == ""
    assert normalize_doc({})["profile"]["intro"] == ""
    # 非字符串丢弃；介绍长度收口到 300。
    bad = normalize_doc({"profile": {"website": 9, "intro": "x" * 500}})["profile"]
    assert bad["website"] == ""
    assert len(bad["intro"]) == 300


def test_normalize_entries_validates_fields():
    raw = {
        "module_order": list(DEFAULT_MODULE_ORDER),
        "profile": {"name": 123, "gender": " 女 ", "summary_html": "<p>ok</p>", "unknown": "x"},
        "companies": [
            "not-a-dict",
            {
                "cid": "bad id!",
                "company": " 阿里云\n智能 ",
                "position": "运营" * 100,
                "start": "2019-13",
                "end": "2020-02",
                "current": 1,
                "description_html": "<p>desc</p>",
                "extra": 1,
            },
        ],
        "projects": [{"pid": "p1", "name": "项目甲", "start": "2021", "end": "2022-00"}],
        "educations": [
            {
                "eid": "e-1_2",
                "school": "某某大学",
                "degree": "博士后",
                "study_mode": "函授",
                "current": True,
            }
        ],
    }
    doc = normalize_doc(raw)

    # 非字符串短字段丢弃；空白收掉；未知键不留。
    profile = doc["profile"]
    assert profile["name"] == "" and profile["gender"] == "女"
    assert "unknown" not in profile

    company = doc["companies"][0]
    assert len(doc["companies"]) == 1  # 非 dict 条目直接丢
    assert company["cid"] != "bad id!" and len(company["cid"]) == 12  # 不合规补发 token_hex(6)
    assert company["company"] == "阿里云 智能"  # 换行收成空格
    assert len(company["position"]) == 128
    assert company["start"] == ""  # 非法月份
    assert company["end"] == "2020-02"
    assert company["current"] is False  # 只认真 bool
    assert "extra" not in company

    project = doc["projects"][0]
    assert project["start"] == "" and project["end"] == ""

    education = doc["educations"][0]
    assert education["eid"] == "e-1_2"
    assert education["degree"] == ""  # 白名单外
    assert education["study_mode"] == ""
    assert education["current"] is True


def test_normalize_round_trips_a_valid_doc():
    doc = normalize_doc(
        {
            "template": "classic",
            "module_order": ["skills", "educations", "projects", "companies", "profile"],
            "profile": {"name": "张三", "job_target": "后端工程师"},
            "companies": [
                {
                    "cid": "c1",
                    "company": "A 公司",
                    "position": "工程师",
                    "department": "平台部",
                    "start": "2018-07",
                    "end": "",
                    "current": True,
                    "description_html": "<ul><li>做了 X</li></ul>",
                }
            ],
            "skills_html": "<p>Python</p>",
        }
    )
    assert normalize_doc(doc) == doc


# ---- 三、富文本消毒 -----------------------------------------------------------


def test_sanitize_drops_script_content_and_event_handlers():
    evil = (
        "<p>hi<script>alert(1)</script>"
        '<img src=x onerror="alert(1)">'
        '<b onclick="x()">bold</b></p>'
    )
    out = sanitize_html(evil)
    assert "<script" not in out and "alert" not in out
    assert "onerror" not in out and "onclick" not in out
    assert "<img" not in out  # 非白名单标签整对丢弃
    assert "<b>bold</b>" in out and out.startswith("<p>hi")


def test_sanitize_links_keep_safe_protocols_only():
    assert sanitize_html('<a href="https://a.com">x</a>') == '<a href="https://a.com">x</a>'
    out = sanitize_html('<a href="javascript:alert(1)">点我</a>')
    assert "<a" not in out and "javascript" not in out
    assert "点我" in out  # 危险链接只丢标签，文本保留


def test_sanitize_repairs_unclosed_tags_and_escapes_text():
    out = sanitize_html("<p>a<b>b<ul><li>c")
    assert out.count("<") == out.count(">")
    assert out.endswith("</li></ul></b></p>")
    assert sanitize_html("<p>a & b > c</p>") == "<p>a &amp; b &gt; c</p>"


def test_sanitize_non_string_is_empty():
    assert sanitize_html(None) == ""
    assert sanitize_html(123) == ""


def test_sanitize_drops_style_class_attributes():
    assert sanitize_html('<p style="position:fixed" class="x">a</p>') == "<p>a</p>"


# ---- 四、AI prompt 拼装 -------------------------------------------------------


def _full_doc() -> dict:
    return normalize_doc(
        {
            "profile": {"name": "李四", "job_target": "产品经理", "city": "上海"},
            "companies": [
                {
                    "cid": "c1",
                    "company": "星河科技",
                    "position": "高级产品运营",
                    "start": "2019-03",
                    "current": True,
                    "description_html": "<p>负责增长</p>",
                }
            ],
            "projects": [{"pid": "p1", "name": "增长平台", "role": "负责人", "start": "2020-01", "end": "2021-06"}],
            "educations": [{"eid": "e1", "school": "某某大学", "degree": "本科", "major": "市场营销"}],
            "skills_html": "<ul><li>数据分析</li></ul>",
        }
    )


def test_assist_prompt_contains_entry_facts_and_full_resume():
    doc = _full_doc()
    prompt = build_assist_prompt(
        doc, ResumeDocAiRequest(module="companies", action="write", item_id="c1")
    )
    # 当前条目聚焦 + 整份事实边界都在。
    assert "星河科技" in prompt and "高级产品运营" in prompt
    assert "2019-03 至今" in prompt
    assert "增长平台" in prompt and "数据分析" in prompt
    assert "<li>" in prompt  # HTML 片段格式约束


def test_assist_prompt_unknown_item_is_an_error():
    with pytest.raises(ValueError, match="找不到对应的经历条目"):
        build_assist_prompt(
            _full_doc(),
            ResumeDocAiRequest(module="projects", action="write", item_id="gone"),
        )


def test_polish_requires_existing_or_selected_text():
    doc = normalize_doc({})
    with pytest.raises(ValueError, match="还没有可润色"):
        build_assist_prompt(doc, ResumeDocAiRequest(module="skills", action="polish"))
    prompt = build_assist_prompt(
        doc,
        ResumeDocAiRequest(module="skills", action="polish", selection="我会 Python"),
    )
    assert "我会 Python" in prompt and "润色" in prompt


def test_skills_and_profile_write_prompts_mention_their_target():
    assert "专业技能" in build_assist_prompt(
        _full_doc(), ResumeDocAiRequest(module="skills", action="write")
    )
    assert "个人优势" in build_assist_prompt(
        _full_doc(), ResumeDocAiRequest(module="profile", action="write")
    )


def test_check_prompt_includes_all_modules_and_rejects_empty_resume():
    with pytest.raises(ValueError, match="简历还是空的"):
        build_check_prompt(default_doc())
    prompt = build_check_prompt(_full_doc())
    for fact in ("李四", "星河科技", "增长平台", "某某大学", "数据分析"):
        assert fact in prompt
    assert "0-100" in prompt


def test_html_to_text_flattens_fragment():
    assert html_to_text("<ul><li>a</li><li>b</li></ul>") == "ab"
    assert html_to_text("<p> a\n\n b </p>") == "a b"


# ---- 五、流式 AI：门槛、搬运与断连 ---------------------------------------------


def _assist_request(**over) -> ResumeDocAiRequest:
    fields = {"module": "skills", "action": "write"}
    fields.update(over)
    return ResumeDocAiRequest(**fields)


def test_ai_stream_without_chat_model_raises_not_ready(db):
    async def _run():
        with pytest.raises(BusinessError) as caught:
            async for _ in service(db).ai_stream(_assist_request()):
                pass
        return caught.value

    exc = asyncio.run(_run())
    assert exc.code == CODE_CHAT_NOT_READY


def test_ai_stream_foreign_doc_is_not_found(db):
    row = service(db).create_doc("别人的")
    seed_chat_model(db)
    request = ResumeDocAiRequest(doc_id=row.id, module="skills", action="write")

    async def _run():
        with pytest.raises(LookupError):
            async for _ in service(db, OTHER).ai_stream(request):
                pass

    asyncio.run(_run())


class _FakeChunk:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    """按序列吐 chunk 的假流式模型；content 故意混入结构化块。"""

    def __init__(self, chunks):
        self._chunks = chunks
        self.messages = None

    async def astream(self, messages):
        self.messages = messages
        for chunk in self._chunks:
            yield _FakeChunk(chunk)


def test_ai_stream_forwards_text_and_structured_blocks(db, monkeypatch):
    doc_id = service(db).create_doc("简历").id
    seed_chat_model(db)
    fake = _FakeModel(["第一段", [{"text": "结构化"}, "尾巴"], ""])
    monkeypatch.setattr("app.services.resume_builder.build_chat_model", lambda *a, **k: fake)

    tokens = collect(
        service(db).ai_stream(ResumeDocAiRequest(doc_id=doc_id, module="skills", action="write"))
    )
    assert "".join(tokens) == "第一段结构化尾巴"
    # 人设 system prompt 与用户事实提示都传进了模型。
    assert fake.messages and fake.messages[0].content  # SystemMessage 非空


def test_ai_stream_stops_when_client_disconnects(db, monkeypatch):
    doc_id = service(db).create_doc("简历").id
    seed_chat_model(db)
    monkeypatch.setattr(
        "app.services.resume_builder.build_chat_model",
        lambda *a, **k: _FakeModel(["a", "b", "c"]),
    )
    checks = {"n": 0}

    async def disconnected():
        checks["n"] += 1
        return checks["n"] >= 2  # 第二个 chunk 前判定已断连

    tokens = collect(
        service(db).ai_stream(_assist_request(doc_id=doc_id), is_disconnected=disconnected)
    )
    assert tokens == ["a"]
