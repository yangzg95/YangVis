"""幻灯片的测试：图片入库、HTML 渲染的注入面、模型输出解析与生成状态机。

分两层（与 AI 网关测试同一形态）：上半是不碰数据库的纯函数——存储的路径校验与
渲染器的白名单/转义，这两块是整份功能唯一的注入面，界面上点不全；下半用
SQLite 内存库跑 ``SlideService`` 的写入路径，不连任何真实服务。生成那几条走
工作流引擎的链路在 ``test_workflow_flows.py`` 里测——那里的模型调用才需要打桩。
"""
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.errors import BusinessError
from app.models.entities import SlideDeck
from app.models.schemas import SlideDeckSave
from app.services import slides as slides_module
from app.services import slides_html
from app.services import slides_store as store
from app.services.agent_run import model_error_text
from app.services.slides import (
    SlideService,
    _generate_inputs,
    _json_candidate,
    _parse_deck_output,
    _truncate,
)
from app.services.workflow_builtin import seed_builtin_workflows
from app.services.workflow_runs import latest_run
from app.services.workflows import RUN_SUCCEEDED, SUBJECT_ANALYZING, SUBJECT_SLIDE_DECK

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
GIF = b"GIF89a" + b"0" * 32
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"0" * 16
NOT_IMAGE = b"<html><script>alert(1)</script></html>"


@pytest.fixture()
def media(tmp_path, monkeypatch):
    """把图片目录与上限指到 pytest 的临时目录：不碰真实部署的 uploads/。"""
    monkeypatch.setattr(
        store,
        "settings",
        SimpleNamespace(
            upload_media_path=tmp_path,
            UPLOAD_MEDIA_URL="/uploads",
        ),
    )
    monkeypatch.setattr(store, "SLIDE_IMAGE_MAX_BYTES", 1024)
    return tmp_path


# ---- 内容嗅探 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_sniff_accepts_real_images(raw, expected):
    assert store.sniff_image(raw)[0] == expected


def test_sniff_rejects_non_image_even_when_named_like_one():
    # 客户端把 HTML 改名成 .png 上传：Content-Type 和文件名都不作数，只看文件头。
    with pytest.raises(store.SlideImageError):
        store.sniff_image(NOT_IMAGE)


def test_sniff_rejects_riff_that_is_not_webp():
    with pytest.raises(store.SlideImageError):
        store.sniff_image(b"RIFF\x10\x00\x00\x00WAVEfmt " + b"0" * 16)


# ---- 落盘与路径校验 -----------------------------------------------------------


def test_store_image_uses_random_name_and_real_extension(media):
    stored = store.store_image(7, "报告截图.PNG", PNG)
    assert stored.rel_path.startswith("slides/7/")
    assert stored.rel_path.endswith(".png")
    assert stored.name == "报告截图.PNG", "用户给的名字只作展示，不参与路径"
    assert stored.url == f"/uploads/{stored.rel_path}"
    assert (media / stored.rel_path).read_bytes() == PNG
    # 文件名是随机 id：两次同名上传不会互相覆盖。
    assert store.store_image(7, "报告截图.PNG", PNG).rel_path != stored.rel_path


def test_store_image_rejects_oversize(media):
    with pytest.raises(store.SlideImageError):
        store.store_image(7, "big.png", PNG + b"x" * 2048)


@pytest.mark.parametrize(
    "rel_path",
    [
        "slides/7/9b873576ec46468f.png",
        "slides/1/deadbeefdeadbeef.jpeg",
        "slides/99999999999999999999/deadbeefdeadbeef.webp",
    ],
)
def test_is_safe_rel_path_accepts_own_layout(rel_path):
    assert store.is_safe_rel_path(rel_path) is True


@pytest.mark.parametrize(
    "rel_path",
    [
        "",
        "slides/7/../../etc/passwd",
        "/slides/7/a.png",
        "other/7/a.png",
        "slides//a.png",
        "slides/7/a.png/extra",
        "slides/7/a.sh",
        "slides/7/a.HTML",
        "slides/7/notes.txt",
        "slides/7/deadbeefdeadbeef.svg",
        "slides/7/a-b_c.webp",
        "slides/7/fffffffffffffffff.png",
        "slides/abc/deadbeefdeadbeef.png",
        "slides/7/" + "x" * 49 + ".png",
    ],
)
def test_is_safe_rel_path_rejects_everything_else(rel_path):
    assert store.is_safe_rel_path(rel_path) is False
    assert store.resolve_url(rel_path) == "", "不合法的路径不能拼出可访问 URL"


def test_delete_image_ignores_unsafe_path_and_missing_file(media):
    (media / "keep.txt").write_text("不要删我")
    store.delete_image("slides/7/../../keep.txt")
    assert (media / "keep.txt").exists()
    store.delete_image("slides/7/ffffffffffffffff.png")  # 不存在也不抛


def test_delete_image_dir_only_removes_own_layout(media):
    stored = store.store_image(9, "a.png", PNG)
    (media / "slides" / "9" / "notes.txt").write_text("别人的文件")
    store.delete_image_dir(9)
    assert not (media / stored.rel_path).exists()
    # 目录里还有不符合命名规则的文件时整目录保留，不硬删。
    assert (media / "slides" / "9" / "notes.txt").exists()


# ---- 渲染器：转义与白名单 ------------------------------------------------------


def _render(**kwargs):
    base = dict(title="测试主题", theme={}, pages=[], assets=[])
    base.update(kwargs)
    return slides_html.render_document(**base)


def _page_layouts(doc: str):
    """文档里真实渲染出来的页面版式。

    断言版式不能用 ``"slide-image" in doc``：CSS 里本来就带着 ``.slide-image``
    选择器，那样永远为真。
    """
    return re.findall(r'<section class="slide slide-([a-z]+)"', doc)


def test_render_escapes_text_everywhere():
    doc = _render(
        pages=[
            {
                "layout": "bullets",
                "title": "<script>alert(1)</script>",
                "bullets": ['"><img src=x onerror=alert(2)>'],
                "notes": "<b>备注</b>",
            }
        ]
    )
    assert "<script>alert(1)</script>" not in doc
    assert "<img src=x" not in doc
    assert "&lt;script&gt;alert(1)" in doc
    # 文档里唯一的脚本是渲染器自带的那段固定导航脚本。
    assert doc.count("<script>") == 1


def test_render_rejects_css_injection_via_theme():
    doc = _render(
        theme={"preset": "evil;", "ratio": "16x9; color: red", "accent": "red;--bg:url(#)"}
    )
    assert "evil" not in doc
    assert "color: red" not in doc
    assert "url(#)" not in doc
    assert "--bg:#ffffff" in doc, "非法 preset 回落默认预设"


def test_render_accepts_valid_accent_and_ratio():
    doc = _render(theme={"preset": "ink", "ratio": "4x3", "accent": "#FF8800"})
    assert "--accent:#FF8800" in doc
    assert "--ar:4 / 3" in doc


def test_render_theme_block_is_parsable_css():
    """自定义属性的值只在 ``;`` 处结束：用换行分隔的话 ``--bg`` 会把后面所有
    声明吞成一个非法值，主题整体失效（背景透明 → 屏幕上是一片底色黑）。"""
    doc = _render(theme={"preset": "ink", "ratio": "16x9", "accent": "ff8800"})
    block = re.search(r"<style>:root\{(.*?)\}</style>", doc, re.DOTALL).group(1)
    declarations = [part.strip() for part in block.split(";") if part.strip()]
    assert all(item.startswith("--") for item in declarations), declarations
    got = dict(item.split(":", 1) for item in declarations)
    assert got["--bg"] == "#0d1117"
    assert got["--fg"] == "#f2f5f7"
    assert got["--accent"] == "#ff8800"
    assert got["--ar"] == "16 / 9"


def test_sanitize_theme_normalizes_hex_without_hash():
    # 手输色号经常不带 #，不能因此「选了颜色没反应」。
    assert slides_html.sanitize_theme({"accent": "ff8800"})["accent"] == "#ff8800"
    assert slides_html.sanitize_theme({"accent": " #abc "})["accent"] == "#abc"
    assert slides_html.sanitize_theme({"accent": "ffa"})["accent"] == "#ffa"
    assert slides_html.sanitize_theme({"accent": "not-a-color"})["accent"] == ""
    assert slides_html.sanitize_theme({"accent": "#gggggg"})["accent"] == ""
    assert "--accent:#ff8800" in _render(theme={"accent": "ff8800"})


# ---- 渲染器：放映时的垂直版面 --------------------------------------------------


def test_content_page_pins_head_and_centers_body_block():
    """内容页的标题钉在上沿，正文在剩下的空间里居中。

    整块居中会让标题随正文条数上下跳，翻页时每一页标题位置都不同 —— 那就是
    「放映时居中怪怪的」。
    """
    css = slides_html._CSS
    assert ".slide-bullets,.slide-image{justify-content:flex-start;gap:0}" in css
    assert ".slide-bullets>.points,.slide-image>.split{margin-top:auto;margin-bottom:auto}" in css
    # gap 会额外压在 auto 外边距之上，正文上下留白就不对称，所以间距由页头出。
    assert ".slide-bullets .page-head,.slide-image .page-head{margin-bottom:" in css


def test_bullet_tier_follows_wrapped_lines_not_item_count():
    budget_16x9 = slides_html._body_budget_u("一个标题", "一句副标题", slides_html._content_height_u("16 / 9"))
    # 5 条以内、每条一行：默认档。
    assert slides_html._bullet_tier(["要点" * 4, "短"], 28, budget_16x9) == ""
    # 7 条就撑不住整页高度，降一档。
    seven = [f"要点{i}：说明一个环节" for i in range(7)]
    assert slides_html._bullet_tier(seven, 28, budget_16x9) == " dense"
    # 3 条但每条都要换行，同样得降档 —— 触发条件是行数，不是条数。
    long_three = ["要点：说明文字" * 9] * 3
    assert slides_html._bullet_tier(long_three, 28, budget_16x9) == " dense"
    # 再挤也只能降到最后一档，字号不能无限缩。
    twelve = [f"第 {i} 条要点，说明一个环节" for i in range(12)]
    assert slides_html._bullet_tier(twelve, 28, budget_16x9) == " denser"


def test_bullet_tier_uses_the_real_aspect_ratio():
    """4:3 的舞台更高，同样的内容不必按 16:9 压字号。"""
    bullets = [f"要点{i}：说明一个环节" for i in range(7)]
    wide = slides_html._body_budget_u("标题", "副标题", slides_html._content_height_u("16 / 9"))
    tall = slides_html._body_budget_u("标题", "副标题", slides_html._content_height_u("4 / 3"))
    assert slides_html._bullet_tier(bullets, 28, wide) == " dense"
    assert slides_html._bullet_tier(bullets, 28, tall) == ""


def test_body_budget_shrinks_when_title_wraps():
    flat = slides_html._body_budget_u("短标题", "", slides_html._content_height_u("16 / 9"))
    wrapped = slides_html._body_budget_u("标题" * 11, "", slides_html._content_height_u("16 / 9"))
    assert abs((flat - wrapped) - 5.04) < 0.01, (flat, wrapped)
    assert slides_html._body_budget_u("标题", "", 42.25) > slides_html._body_budget_u("标题", "副标题", 42.25)


def test_only_full_width_lists_get_tiers():
    # 引用页 / 结尾页的列表由外层版式整块居中，字号本来就更小，不参与降档。
    many = [f"要点{i}：说明一个环节" for i in range(10)]
    assert slides_html._bullet_list(many, class_name="quote-points") == (
        '<ul class="quote-points">' + "".join(f"<li>{item}</li>" for item in many) + "</ul>"
    )
    assert 'class="points denser"' in _render(
        pages=[{"layout": "bullets", "title": "一个标题", "subtitle": "一句副标题", "bullets": many}]
    )


def test_bullet_tier_numbers_match_the_css():
    """降档表里的字号 / 行高 / 间距必须和 CSS 写的一致。

    估算是照着版面算的：改了 ``.points.dense`` 的字号而没改表，降档就会按错的
    高度判断，页面该缩的时候不缩。
    """
    css = slides_html._CSS
    for _suffix, font_u, line_h, gap_u, _factor in slides_html._BULLET_TIERS:
        assert f"font-size:calc(var(--u)*{font_u})" in css, font_u
        assert f"line-height:{line_h}" in css, line_h
        assert f"gap:calc(var(--u)*{gap_u})" in css, gap_u


def test_image_page_bounds_the_shot_height():
    """配图页把正文可用高度交给 CSS 的 ``--body-h``：竖图只会自己缩，不会把整页顶出舞台。"""
    doc = _render(
        pages=[{"layout": "image", "title": "配图页", "asset_id": "aa11", "bullets": ["要点"]}],
        assets=[{"id": "aa11", "rel_path": "slides/7/aa11bb22cc33dd44.png"}],
    )
    assert re.search(r'class="split" style="--body-h:calc\(var\(--u\)\*\d+\.\d{2}\)"', doc), doc
    assert "max-height:var(--body-h" in slides_html._CSS


def test_render_downgrades_image_page_without_asset():
    doc = _render(pages=[{"layout": "image", "title": "配图页", "asset_id": "deadbeef"}])
    assert _page_layouts(doc) == ["bullets"], "引用了不存在的图片时降级成要点页，不留破图"


def test_render_ignores_asset_with_unsafe_rel_path():
    doc = _render(
        pages=[{"layout": "image", "title": "配图页", "asset_id": "aa11", "bullets": ["兜底"]}],
        assets=[{"id": "aa11", "rel_path": "slides/7/../../evil.png"}],
    )
    assert "<img" not in doc
    assert _page_layouts(doc) == ["bullets"]


def test_render_keeps_asset_with_safe_rel_path():
    doc = _render(
        pages=[{"layout": "image", "title": "配图页", "asset_id": "aa11", "bullets": ["要点"]}],
        assets=[{"id": "aa11", "rel_path": "slides/7/aa11bb22cc33dd44.png"}],
    )
    assert '<img src="/uploads/slides/7/aa11bb22cc33dd44.png"' in doc
    assert _page_layouts(doc) == ["image"]


def test_render_falls_back_for_unknown_layout_and_empty_deck():
    assert _page_layouts(_render(pages=[{"layout": "matrix", "title": "非法版式"}])) == ["bullets"]
    assert _page_layouts(_render(title="还没有内容")) == ["cover"], "空 deck 也给一张封面，放映不至于白屏"


def test_sanitize_pages_keeps_legal_sid_and_reissues_others():
    pages = slides_html.sanitize_pages(
        [
            {"layout": "bullets", "sid": "p" + "0" * 16},
            {"layout": "bullets", "sid": "../../x"},
            {"layout": "bullets"},
        ]
    )
    assert pages[0]["sid"] == "p" + "0" * 16
    assert all(slides_html._SID_RE.match(page["sid"]) for page in pages)


def test_sanitize_pages_caps_pages_and_field_length():
    pages = slides_html.sanitize_pages(
        [{"layout": "bullets", "title": "x" * 5000, "bullets": ["y" * 5000] * 30} for _ in range(200)]
    )
    assert len(pages) == slides_html.MAX_DECK_PAGES
    assert len(pages[0]["title"]) == slides_html.MAX_PAGE_TITLE
    assert len(pages[0]["bullets"]) == slides_html.MAX_PAGE_BULLETS
    assert len(pages[0]["bullets"][0]) == slides_html.MAX_PAGE_BULLET


# ---- 模型输出解析 -------------------------------------------------------------


def test_json_candidate_handles_fence_and_prose():
    assert _json_candidate('```json\n{"slides": []}\n```') == '{"slides": []}'
    assert (
        _json_candidate('好的，这是结果：\n{"slides": [{"title": "x"}]}\n希望有用')
        == '{"slides": [{"title": "x"}]}'
    )
    assert _json_candidate('前缀 [{"layout":"cover"}] 后缀') == '[{"layout":"cover"}]'
    assert _json_candidate("模型今天不肯干活") is None


def test_parse_deck_output_shapes():
    theme, pages = _parse_deck_output(
        '{"theme":{"preset":"ink"},"slides":[{"layout":"cover"}]}'
    )
    assert theme == {"preset": "ink"}
    assert pages == [{"layout": "cover"}]

    theme, pages = _parse_deck_output('[{"layout":"cover"}]')
    assert theme == {} and len(pages) == 1

    _, pages = _parse_deck_output('{"pages":[{"layout":"cover"}]}')
    assert len(pages) == 1, "pages 是 slides 的常见别名，容忍"


@pytest.mark.parametrize("text", ["模型今天不肯干活", '{"slides": [未闭合}'])
def test_parse_deck_output_rejects_garbage(text):
    with pytest.raises(ValueError):
        _parse_deck_output(text)


def test_generate_inputs_carries_material_and_asset_ids():
    inputs = _generate_inputs(
        title="季度复盘",
        description=None,
        requirement="面向管理层",
        source_text="正文……",
        asset_names=["aa11bb22cc33dd44: 架构图.png"],
    )
    assert inputs["title"] == "季度复盘"
    assert inputs["requirement_note"] == "用户对这份幻灯片的要求：面向管理层\n\n"
    assert "正文……" in inputs["source_block"]
    assert "aa11bb22cc33dd44: 架构图.png" in inputs["asset_block"], "模型只能引用清单里的 asset_id"
    assert inputs["description_note"] == "", "没填的段落就是空串，模板里那一格自然什么也没有"
    assert inputs["max_pages"] == str(slides_html.MAX_DECK_PAGES)


def test_truncate_marks_over_limit_source():
    limit = slides_module.settings.SLIDE_MAX_SOURCE_CHARS
    assert _truncate("x" * limit) == "x" * limit
    marked = _truncate("x" * (limit + 1))
    assert marked.startswith("x" * limit)
    assert "已截断" in marked


def test_task_error_text_explains_timeout():
    assert "超时" in model_error_text(TimeoutError("Request timed out."))
    assert model_error_text(ValueError("别的错")) == "别的错"


# ---- 服务层（SQLite 内存库） --------------------------------------------------


@pytest.fixture()
def db():
    # StaticPool + 单连接：内存库默认每个连接各有一份数据，测试会看不到自己写的行。
    # 这里只建运行、不跑运行（跑的那几条在 test_workflow_flows.py），所以内存库够用。
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    seed_builtin_workflows(session)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


OWNER = 1


def _service(db) -> SlideService:
    return SlideService(db, OWNER)


def _deck(db) -> SlideDeck:
    return _service(db).create_deck(
        title="季度复盘", description="说明", requirement=None, source_text="材料正文"
    )


def _finished(db, deck: SlideDeck | None = None) -> SlideDeck:
    """造一份已经生成完的 deck：把创建时那条运行就地改成成功。

    状态列退役之后，「生成完了」这件事只存在于 ``workflow_run`` 上，测试要造这个
    现场也只能改它。
    """
    deck = deck or _deck(db)
    run = latest_run(db, OWNER, SUBJECT_SLIDE_DECK, deck.id)
    run.status = RUN_SUCCEEDED
    db.commit()
    return deck


def test_create_deck_requires_material(db):
    with pytest.raises(ValueError):
        _service(db).create_deck(title="空", description=None, requirement=None, source_text="  ")


def test_create_deck_requires_title(db):
    with pytest.raises(ValueError):
        _service(db).create_deck(title="  ", description=None, requirement=None, source_text="有正文")


def test_create_deck_starts_analyzing_with_cover_html(db):
    service = _service(db)
    deck = _deck(db)
    assert service.to_item(deck).status == SUBJECT_ANALYZING, "创建时建好的那条运行就是它的进度"
    assert deck.slides == []
    assert _page_layouts(deck.html) == ["cover"], "先渲染一张封面，未生成完也能打开预览"


def test_deck_is_scoped_to_owner(db):
    deck = _deck(db)
    other = SlideService(db, OWNER + 1)
    assert other.list_decks() == []
    with pytest.raises(LookupError):
        other.get_deck(deck.id)


def test_save_deck_assigns_sid_and_rerenders(db):
    deck = _deck(db)
    saved = _service(db).save_deck(
        deck.id,
        title="改了主题",
        description=None,
        theme={"preset": "violet", "ratio": "4x3", "accent": None},
        slides=[
            {"layout": "cover", "title": "新封面"},
            {"layout": "bullets", "title": "第二页", "bullets": ["要点一"]},
        ],
    )
    assert saved.title == "改了主题"
    assert [page["layout"] for page in saved.slides] == ["cover", "bullets"]
    assert all(slides_html._SID_RE.match(page["sid"]) for page in saved.slides), "新页由服务端发放 sid"
    assert saved.theme["preset"] == "violet"
    assert "新封面" in saved.html and "要点一" in saved.html


def test_save_payload_accepts_pages_without_sid(db):
    """工作台「加一页 → 保存」的形状：新页没有 sid，必须能过校验。"""
    payload = SlideDeckSave.model_validate(
        {"title": "改了主题", "slides": [{"layout": "cover", "title": "新页"}]}
    )
    assert payload.slides[0].sid is None
    kept = SlideDeckSave.model_validate(
        {"slides": [{"layout": "bullets", "sid": "p" + "0" * 16}]}
    )
    assert kept.slides[0].sid == "p" + "0" * 16

    saved = _service(db).save_deck(
        _deck(db).id,
        title=payload.title,
        description=None,
        theme=None,
        slides=[page.model_dump() for page in payload.slides],
    )
    assert slides_html._SID_RE.match(saved.slides[0]["sid"])


def test_save_deck_theme_only_keeps_stored_pages(db):
    """工作台的外观改动只 PUT theme：页面内容不能被顺带覆盖，HTML 要跟着重渲染。"""
    service = _service(db)
    deck = _deck(db)
    service.save_deck(
        deck.id, title=None, description=None, theme=None,
        slides=[{"layout": "bullets", "title": "已有内容", "bullets": ["要点"]}],
    )

    saved = service.save_deck(
        deck.id, title=None, description=None,
        theme={"preset": "paper", "ratio": "16x9", "accent": "ff8800"}, slides=None,
    )
    assert [page["title"] for page in saved.slides] == ["已有内容"]
    assert saved.theme["accent"] == "#ff8800"
    assert "--accent:#ff8800" in saved.html and "已有内容" in saved.html


def test_save_deck_rejects_empty_title(db):
    deck = _deck(db)
    with pytest.raises(ValueError):
        _service(db).save_deck(deck.id, title="   ", description=None, theme=None, slides=None)


def test_save_deck_rejects_text_as_html(db):
    deck = _deck(db)
    saved = _service(db).save_deck(
        deck.id,
        title=None,
        description=None,
        theme=None,
        slides=[{"layout": "bullets", "title": "<img src=x onerror=alert(1)>"}],
    )
    assert "<img src=x" not in saved.html, "手改的 slides 出口仍然只是文本"


def test_delete_deck_refuses_while_analyzing(db):
    deck = _deck(db)
    with pytest.raises(ValueError):
        _service(db).delete_deck(deck.id)


def test_delete_deck_returns_image_paths(db, media):
    service = _service(db)
    deck = _finished(db)
    stored = store.store_image(deck.id, "a.png", PNG)
    service.add_asset(deck.id, stored)

    assert service.delete_deck(deck.id) == [stored.rel_path]
    assert service.list_decks() == []


def test_asset_lifecycle_updates_html_and_unreferences_pages(db, media):
    service = _service(db)
    deck = _deck(db)
    stored = store.store_image(deck.id, "架构图.png", PNG)
    with_images = service.add_asset(deck.id, stored)
    assert with_images.assets[0]["url"] == f"/uploads/{stored.rel_path}"

    service.save_deck(
        deck.id,
        title=None,
        description=None,
        theme=None,
        slides=[
            {
                "layout": "image",
                "title": "架构",
                "asset_id": stored.id,
                "bullets": ["一条兜底要点"],
            }
        ],
    )
    assert stored.rel_path in service.get_deck(deck.id).html

    _, rel_path = service.remove_asset(deck.id, stored.id)
    assert rel_path == stored.rel_path
    reloaded = service.get_deck(deck.id)
    assert reloaded.slides[0]["asset_id"] == ""
    assert _page_layouts(reloaded.html) == ["bullets"], "删图后引用它的页面降级"


def test_add_asset_caps_per_deck(db, media, monkeypatch):
    service = _service(db)
    deck = _deck(db)
    monkeypatch.setattr(slides_module.settings, "SLIDE_MAX_IMAGES_PER_DECK", 1)
    service.add_asset(deck.id, store.store_image(deck.id, "a.png", PNG))
    with pytest.raises(ValueError):
        service.add_asset(deck.id, store.store_image(deck.id, "b.png", PNG))


def test_remove_asset_unknown_id(db):
    with pytest.raises(LookupError):
        _service(db).remove_asset(_deck(db).id, "ffffffffffffffff")


def test_require_chat_config_blocks_without_model(db):
    with pytest.raises(BusinessError):
        _service(db).require_chat_config()


def test_start_generation_blocks_double_trigger(db):
    service = _service(db)
    deck = _finished(db)
    service.start_generation(deck.id, "只要三页")
    assert deck.requirement == "只要三页"
    assert service.to_item(deck).status == SUBJECT_ANALYZING
    with pytest.raises(ValueError):
        service.start_generation(deck.id)


def test_deck_html_backfills_missing_document(db):
    service = _service(db)
    deck = _deck(db)
    deck.html = None
    db.commit()

    html_text = service.deck_html(deck)
    assert html_text.startswith("<!doctype html>")
    assert service.get_deck(deck.id).html == html_text


def test_deck_html_rerenders_stale_document(db):
    """渲染器改了（主题分号那类修复）之后，老记录不能继续发上一版文档。"""
    service = _service(db)
    deck = _deck(db)
    deck.html = "<!doctype html><html><body>上一版渲染结果</body></html>"
    deck.theme = {"preset": "ink", "ratio": "16x9", "accent": ""}
    db.commit()

    fresh = service.deck_html(deck)
    assert "上一版渲染结果" not in fresh
    assert "--bg:#0d1117;" in fresh
    assert service.get_deck(deck.id).html == fresh


def test_to_detail_serializes_clean_shape(db, media):
    service = _service(db)
    deck = _deck(db)
    stored = store.store_image(deck.id, "a.png", PNG)
    service.add_asset(deck.id, stored)
    service.save_deck(
        deck.id,
        title=None,
        description=None,
        theme=None,
        slides=[{"layout": "bullets", "title": "一页", "bullets": ["a"], "sid": "bad"}],
    )

    detail = service.to_detail(service.get_deck(deck.id))
    assert detail.page_count == 1 and detail.asset_count == 1
    assert slides_html._SID_RE.match(detail.slides[0].sid), "脏 sid 在出口重发"
    assert detail.assets[0].url == f"/uploads/{stored.rel_path}"
    assert detail.source_text == "材料正文"
