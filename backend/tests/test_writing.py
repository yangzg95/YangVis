"""创作模块 service 层：作品 CRUD、大纲清洗/对账、正文块存取与导出。

- 大纲：cid 发放与复用、层级按 kind 收窄、节点上限、叶子与 part 行对账；
- 正文块：标题同步大纲、字数服务端统计、超长闸门、删除连带摘节点；
- 隔离：他人作品 id 读作 LookupError；
- 导出：卷章标题分级，txt 去掉 markdown 标记。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base
from app.models.entities import WritingPart
from app.models.schemas import (
    OutlineNodeIn,
    WritingWorkCreate,
    WritingWorkUpdate,
)
from app.services.writing import WritingService
from app.services.writing.words import count_words

OWNER = 1
OTHER = 2


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'writing.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def service(db):
    return WritingService(db, OWNER)


def _novel(service: WritingService, title: str = "雨夜书"):
    return service.create_work(WritingWorkCreate(kind="novel", title=title))


def _article(service: WritingService, title: str = "公众号一篇"):
    return service.create_work(WritingWorkCreate(kind="article", title=title))


# ---- 作品 CRUD ---------------------------------------------------------------


def test_create_sanitizes_params_by_kind(service):
    work = service.create_work(
        WritingWorkCreate(
            kind="novel",
            title="  仙舟  ",
            brief="  一句话灵感 ",
            requirement="  别写感情戏 ",
            params={
                "genre": "玄幻",
                "lead": "少年修士",
                # 文章专属键与未知键都要被丢掉。
                "article_type": "公众号",
                "evil": "<script>",
                "word_target": 3000,
            },
        )
    )
    assert work.title == "仙舟"
    assert work.brief == "一句话灵感"
    assert work.requirement == "别写感情戏"
    assert work.params == {"genre": "玄幻", "lead": "少年修士", "word_target": 3000}
    assert work.outline == [] and work.meta == {}


def test_word_target_out_of_range_dropped(service):
    work = service.create_work(
        WritingWorkCreate(
            kind="article",
            title="T",
            params={"word_target": 999999, "audience": "产品经理"},
        )
    )
    assert work.params == {"audience": "产品经理"}


def test_owner_count_cap(service, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "WRITING_MAX_WORKS_PER_OWNER", 1)
    service.create_work(WritingWorkCreate(kind="novel", title="第一本"))
    with pytest.raises(ValueError, match="最多"):
        service.create_work(WritingWorkCreate(kind="novel", title="第二本"))


def test_list_filters_by_kind_and_keyword(service):
    novel = _novel(service, "星河彼岸")
    article = _article(service, "星河公众号")
    service.update_work(article.id, WritingWorkUpdate(brief="讲增长的"))

    assert [w.id for w in service.list_works(kind="novel")] == [novel.id]
    assert [w.id for w in service.list_works(kind="article")] == [article.id]
    assert {w.id for w in service.list_works(keyword="星河")} == {novel.id, article.id}
    assert [w.id for w in service.list_works(keyword="增长")] == [article.id]
    # LIKE 通配符按字面量搜。
    assert service.list_works(keyword="%") == []


def test_update_is_partial_and_null_clears_requirement(service):
    work = _novel(service)
    service.update_work(work.id, WritingWorkUpdate(requirement="节奏快一点"))
    updated = service.update_work(work.id, WritingWorkUpdate(requirement=None))
    assert updated.requirement is None
    assert updated.title == work.title


def test_owner_isolation(service, db):
    work = _novel(service)
    intruder = WritingService(db, OTHER)
    assert intruder.list_works() == []
    with pytest.raises(LookupError):
        intruder.get_work(work.id)
    with pytest.raises(LookupError):
        intruder.delete_work(work.id)


def test_delete_work_cascades_parts(service, db):
    work = _novel(service)
    service.save_outline(
        work.id, [OutlineNodeIn(kind="chapter", title="第一章").model_dump()]
    )
    assert service.list_parts(work)

    service.delete_work(work.id)
    with pytest.raises(LookupError):
        service.get_work(work.id)
    # 从属正文块同事务删除，不留孤儿行。
    orphan = db.scalars(select(WritingPart).where(WritingPart.work_id == work.id)).all()
    assert orphan == []


# ---- 大纲清洗与对账 ------------------------------------------------------------


def test_outline_issues_cid_and_creates_parts(service):
    work = _novel(service)
    saved = service.save_outline(
        work.id,
        [
            {"kind": "volume", "title": "第一卷", "children": [
                {"kind": "chapter", "title": "第一章 雨夜"},
                {"kind": "chapter", "title": "", "synopsis": "孤儿回城"},
            ]},
            {"kind": "chapter", "title": "第三章 独行"},
        ],
    )
    leaves = [leaf for leaf in saved.outline[0]["children"]] + [saved.outline[1]]
    assert all(leaf["cid"].startswith("p") and len(leaf["cid"]) == 17 for leaf in leaves)
    # 空标题回落默认名。
    assert saved.outline[0]["children"][1]["title"] == "未命名章节"

    detail = service.to_detail(saved)
    assert detail.part_count == 3
    assert [p.title for p in detail.parts] == ["第一章 雨夜", "未命名章节", "第三章 独行"]
    assert [p.sort_order for p in detail.parts] == [0, 1, 2]
    assert all(p.content == "" and p.word_count == 0 for p in detail.parts)


def test_outline_reuses_known_cid_and_reissues_illegal(service):
    work = _novel(service)
    first = service.save_outline(
        work.id, [{"kind": "chapter", "title": "第一章"}]
    )
    good_cid = first.outline[0]["cid"]

    second = service.save_outline(
        work.id,
        [
            {"cid": good_cid, "kind": "chapter", "title": "第一章（改）"},
            {"cid": "garbage", "kind": "chapter", "title": "第二章"},
            {"cid": good_cid, "kind": "chapter", "title": "冒名的第三章"},
        ],
    )
    cids = [node["cid"] for node in second.outline]
    assert cids[0] == good_cid  # 合法 cid 原样复用
    assert cids[1] != "garbage" and cids[1].startswith("p")  # 非法重发
    assert cids[2] != good_cid  # 重名也重发，不允许两个节点抢一个 cid


def test_article_outline_is_flat_and_nested_children_dropped(service):
    work = _article(service)
    saved = service.save_outline(
        work.id,
        [
            {"kind": "section", "title": "开头", "children": [{"kind": "section", "title": "嵌套层"}]},
            {"kind": "volume", "title": "乱入的卷"},  # 文章里一律归一成 section
        ],
    )
    assert all(node["kind"] == "section" for node in saved.outline)
    assert all("children" not in node or not node["children"] for node in saved.outline)
    assert service.to_detail(saved).part_count == 2


def test_novel_chapter_children_are_dropped(service):
    work = _novel(service)
    saved = service.save_outline(
        work.id,
        [{"kind": "chapter", "title": "章", "children": [{"kind": "chapter", "title": "孙节点"}]}],
    )
    assert saved.outline[0]["kind"] == "chapter"
    assert not saved.outline[0].get("children")


def test_outline_node_cap(service, monkeypatch):
    monkeypatch.setattr(get_settings(), "WRITING_OUTLINE_MAX_NODES", 2)
    work = _novel(service)
    with pytest.raises(ValueError, match="上限"):
        service.save_outline(
            work.id,
            [{"kind": "chapter", "title": str(i)} for i in range(3)],
        )


def test_outline_reconcile_deletes_orphan_parts_and_reorders(service):
    work = _novel(service)
    saved = service.save_outline(
        work.id,
        [{"kind": "chapter", "title": "第一章"}, {"kind": "chapter", "title": "第二章"}],
    )
    first_cid, second_cid = (node["cid"] for node in saved.outline)
    service.update_part(work.id, first_cid, title="第一章", content="已经写了三千字")

    # 删掉第二章、把第一章改名并整体重存：孤儿 part 连删，标题同步。
    reordered = service.save_outline(
        work.id, [{"cid": first_cid, "kind": "chapter", "title": "第一章（改名）"}]
    )
    detail = service.to_detail(reordered)
    assert detail.part_count == 1
    assert detail.parts[0].cid == first_cid
    assert detail.parts[0].title == "第一章（改名）"
    # 正文没动。
    assert detail.parts[0].content == "已经写了三千字"
    with pytest.raises(LookupError):
        service._get_part(reordered, second_cid)


def test_part_cap_enforced(service, monkeypatch):
    monkeypatch.setattr(get_settings(), "WRITING_MAX_PARTS_PER_WORK", 1)
    work = _novel(service)
    service.save_outline(work.id, [{"kind": "chapter", "title": "第一章"}])
    with pytest.raises(ValueError, match="最多"):
        service.save_outline(
            work.id,
            [
                {"cid": None, "kind": "chapter", "title": "第一章"},
                {"kind": "chapter", "title": "第二章"},
            ],
        )


# ---- 正文块 -------------------------------------------------------------------


def test_add_part_appends_into_last_volume_and_after_cid(service):
    work = _novel(service)
    service.save_outline(
        work.id,
        [{"kind": "volume", "title": "第一卷", "children": [
            {"kind": "chapter", "title": "第一章"},
        ]}],
    )
    work, new_cid = service.add_part(work.id, "第二章", after_cid=None)
    # 末尾卷存在时新章进卷，而不是挂成顶层章。
    assert work.outline[0]["children"][-1]["cid"] == new_cid

    first_cid = work.outline[0]["children"][0]["cid"]
    work, inserted_cid = service.add_part(work.id, "插队章", after_cid=first_cid)
    children = work.outline[0]["children"]
    assert children[1]["cid"] == inserted_cid and children[1]["title"] == "插队章"
    assert service.to_detail(work).part_count == 3


def test_add_part_article_uses_section_and_default_title(service):
    work = _article(service)
    work, cid = service.add_part(work.id, "  ", after_cid=None)
    assert work.outline[0]["kind"] == "section"
    assert work.outline[0]["title"] == "未命名段落"
    assert cid


def test_update_part_counts_words_and_syncs_outline_title(service):
    work = _novel(service)
    work, cid = service.add_part(work.id, "原章名", None)
    part = service.update_part(
        work.id,
        cid,
        title="  新章名  ",
        content="少年策马下山，city 不相信眼泪，word_count 测试。",
    )
    # CJK：少年策马下山（6）+ 不相信眼泪（5）+ 测试（2）= 13；
    # 拉丁正则不含下划线，word_count 拆成 word/count 两个 token，加 city 共 3。
    assert part.word_count == 16
    assert part.title == "新章名"
    leaf = next(node for node in work.outline if node.get("cid") == cid)
    assert leaf["title"] == "新章名"


def test_update_part_rejects_oversized_content(service, monkeypatch):
    monkeypatch.setattr(get_settings(), "WRITING_PART_CONTENT_MAX_CHARS", 10)
    work = _novel(service)
    work, cid = service.add_part(work.id, "章", None)
    with pytest.raises(ValueError, match="上限"):
        service.update_part(work.id, cid, title=None, content="x" * 11)


def test_update_part_blank_title_rejected(service):
    work = _novel(service)
    work, cid = service.add_part(work.id, "章", None)
    with pytest.raises(ValueError, match="标题"):
        service.update_part(work.id, cid, title="   ", content=None)


def test_delete_part_removes_leaf_and_row(service):
    work = _novel(service)
    work, first = service.add_part(work.id, "第一章", None)
    work, second = service.add_part(work.id, "第二章", None)
    work = service.delete_part(work.id, first)
    detail = service.to_detail(work)
    assert [p.cid for p in detail.parts] == [second]
    with pytest.raises(LookupError):
        service._get_part(work, first)


def test_part_unknown_cid_is_lookup_error(service):
    work = _novel(service)
    with pytest.raises(LookupError):
        service.update_part(work.id, "p0000000000000000", title="x", content=None)


# ---- 导出 ---------------------------------------------------------------------


def test_export_markdown_and_text(service):
    work = _novel(service, "我的书")
    service.save_outline(
        work.id,
        [
            {"kind": "volume", "title": "第一卷", "children": [
                {"kind": "chapter", "title": "第一章"},
            ]},
            {"kind": "chapter", "title": "第二章"},
        ],
    )
    leaves = service.to_detail(work).parts
    service.update_part(work.id, leaves[0].cid, title=None, content="第一章正文。")
    service.update_part(work.id, leaves[1].cid, title=None, content="第二章正文。")

    markdown = service.export_markdown(service.get_work(work.id))
    assert markdown.startswith("# 我的书\n")
    assert "## 第一卷\n" in markdown
    assert "### 第一章\n" in markdown
    assert "## 第二章\n" in markdown
    assert "第一章正文。" in markdown

    text = service.export_text(service.get_work(work.id))
    assert "#" not in text
    assert "第一卷" in text and "第一章正文。" in text


def test_article_export_uses_level_two_headings(service):
    work = _article(service, "一篇文章")
    work, cid = service.add_part(work.id, "开头", None)
    service.update_part(work.id, cid, title=None, content="开头正文")
    markdown = service.export_markdown(service.get_work(work.id))
    assert "## 开头\n" in markdown


# ---- 字数纯函数 ---------------------------------------------------------------


def test_count_words_handles_cjk_latin_and_blank():
    assert count_words(None) == 0
    assert count_words("") == 0
    assert count_words("## 标题 **加粗**") == 4  # 标题加粗四个 CJK，标记不计
    assert count_words("hello world2 v2") == 3
    assert count_words("你好 world") == 3


# ---- M2：大纲生成的输入拼装与产物解析（纯函数，不碰库）---------------------------

from app.models.entities import WritingWork as _WritingWork  # noqa: E402
from app.services.writing import generate as gen  # noqa: E402


def _detached_work(kind: str = "novel", **kw) -> _WritingWork:
    return _WritingWork(
        kind=kind,
        title=kw.get("title", "雨夜书"),
        brief=kw.get("brief", ""),
        requirement=kw.get("requirement"),
        params=kw.get("params", {}),
        outline=[],
        meta={},
    )


def test_workflow_slug_follows_kind():
    assert gen.workflow_slug_for("novel") == "novel-outline"
    assert gen.workflow_slug_for("article") == "article-outline"


def test_outline_inputs_omit_empty_optional_blocks():
    inputs = gen.outline_inputs(_detached_work())
    assert inputs["title"] == "雨夜书"
    assert inputs["brief_block"] == ""
    assert inputs["params_block"] == ""
    assert inputs["requirement_note"] == ""
    assert inputs["max_nodes"] == str(get_settings().WRITING_OUTLINE_MAX_NODES)


def test_outline_inputs_render_labeled_blocks():
    # 落库参数已由写入侧 sanitize_params 白名单洗过（见 test_create_sanitizes_params_by_kind），
    # generate 只负责把白名单键说成人话，不再做二次过滤。
    work = _detached_work(
        brief=" 少年下山 ",
        requirement="别写感情戏",
        params={"genre": "玄幻", "word_target": 3000},
    )
    inputs = gen.outline_inputs(work)
    assert "灵感 / 梗概" in inputs["brief_block"] and "少年下山" in inputs["brief_block"]
    assert "题材：玄幻" in inputs["params_block"]
    assert "目标总字数：3000" in inputs["params_block"]
    assert "补充要求：别写感情戏" in inputs["requirement_note"]


def test_article_inputs_use_article_param_labels():
    work = _detached_work("article", title="一篇文", params={"article_type": "知乎", "audience": "产品经理"})
    inputs = gen.outline_inputs(work)
    assert "文章类型：知乎" in inputs["params_block"]
    assert "目标受众：产品经理" in inputs["params_block"]
    assert "主题 / 素材" in gen.outline_inputs(_detached_work("article", brief="x"))["brief_block"]


def test_parse_novel_outline_accepts_fenced_json_and_volumes():
    raw = (
        '```json\n{"outline":[{"title":"第一卷","chapters":['
        '{"title":"第一章","synopsis":"下山","target_words":3000}]},'
        '{"title":"第二章","synopsis":"独行"}]}\n```'
    )
    nodes = gen.parse_novel_outline(raw)
    assert nodes[0]["kind"] == "volume" and nodes[0]["title"] == "第一卷"
    assert nodes[0]["children"][0] == {
        "kind": "chapter",
        "title": "第一章",
        "synopsis": "下山",
        "target_words": 3000,
    }
    # 裸章节收成顶层章，缺字段回落空值而不是报错。
    assert nodes[1]["kind"] == "chapter" and nodes[1]["target_words"] is None


def test_parse_novel_outline_tolerates_bare_array():
    nodes = gen.parse_novel_outline('[{"title":"第一章","synopsis":"x"}]')
    assert len(nodes) == 1 and nodes[0]["kind"] == "chapter"


def test_parse_novel_outline_rejects_text_without_json():
    with pytest.raises(ValueError, match="可解析"):
        gen.parse_novel_outline("没有结构的寒暄文本")
    # 括号平衡但语法非法：截得出跨度、json.loads 失败，走「无法解析」分支。
    with pytest.raises(ValueError, match="无法解析"):
        gen.parse_novel_outline('{"outline": [}')


def test_parse_article_outline_keeps_sections_and_candidates():
    sections, candidates = gen.parse_article_outline(
        '{"title_candidates":["甲","甲","乙","丙","丁","戊"],'
        '"sections":[{"title":"开篇","point":"立论","target_words":800},'
        '{"title":"次段","synopsis":"用 synopsis 兜底"}]}'
    )
    assert [s["kind"] for s in sections] == ["section", "section"]
    assert sections[0]["synopsis"] == "立论" and sections[0]["target_words"] == 800
    assert sections[1]["synopsis"] == "用 synopsis 兜底"
    # 去重且最多 5 条。
    assert candidates == ["甲", "乙", "丙", "丁", "戊"]


def test_parse_article_outline_without_candidates_returns_empty_list():
    sections, candidates = gen.parse_article_outline('{"sections":[{"title":"开篇"}]}')
    assert len(sections) == 1 and candidates == []
