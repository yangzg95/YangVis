"""幻灯片的测试：图片入库、HTML 渲染的注入面、模型输出解析与生成状态机。

分两层（与 AI 网关测试同一形态）：上半是不碰数据库的纯函数——存储的路径校验与
渲染器的白名单/转义，这两块是整份功能唯一的注入面，界面上点不全；下半用
SQLite 内存库跑 ``SlideService`` 的写入路径，模型调用一律 monkeypatch 掉，
不连任何真实服务。
"""
import asyncio
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
from app.services.slides import (
    STATUS_ANALYZING,
    STATUS_ERROR,
    STATUS_READY,
    SlideService,
    _generate_prompt,
    _json_candidate,
    _parse_deck_output,
    _task_error_text,
    _truncate,
    reset_stale_slides,
)

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


def test_render_downgrades_image_page_without_asset():
    doc = _render(pages=[{"layout": "image", "title": "配图页", "asset_id": "deadbeef"}])
    assert "slide-image" not in doc
    assert "slide-bullets" in doc, "引用了不存在的图片时降级成要点页，不留破图"


def test_render_ignores_asset_with_unsafe_rel_path():
    doc = _render(
        pages=[{"layout": "image", "title": "配图页", "asset_id": "aa11", "bullets": ["兜底"]}],
        assets=[{"id": "aa11", "rel_path": "slides/7/../../evil.png"}],
    )
    assert "<img" not in doc
    assert "slide-bullets" in doc


def test_render_keeps_asset_with_safe_rel_path():
    doc = _render(
        pages=[{"layout": "image", "title": "配图页", "asset_id": "aa11", "bullets": ["要点"]}],
        assets=[{"id": "aa11", "rel_path": "slides/7/aa11bb22cc33dd44.png"}],
    )
    assert '<img src="/uploads/slides/7/aa11bb22cc33dd44.png"' in doc


def test_render_falls_back_for_unknown_layout_and_empty_deck():
    assert "slide-bullets" in _render(pages=[{"layout": "matrix", "title": "非法版式"}])
    assert "slide-cover" in _render(title="还没有内容"), "空 deck 也给一张封面，放映不至于白屏"


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


def test_generate_prompt_carries_material_and_asset_ids():
    prompt = _generate_prompt(
        title="季度复盘",
        description=None,
        requirement="面向管理层",
        source_text="正文……",
        asset_names=["aa11bb22cc33dd44: 架构图.png"],
    )
    assert "面向管理层" in prompt
    assert "正文……" in prompt
    assert "aa11bb22cc33dd44: 架构图.png" in prompt, "模型只能引用清单里的 asset_id"


def test_truncate_marks_over_limit_source():
    limit = slides_module.settings.SLIDE_MAX_SOURCE_CHARS
    assert _truncate("x" * limit) == "x" * limit
    marked = _truncate("x" * (limit + 1))
    assert marked.startswith("x" * limit)
    assert "已截断" in marked


def test_task_error_text_explains_timeout():
    assert "超时" in _task_error_text(TimeoutError("Request timed out."))
    assert _task_error_text(ValueError("别的错")) == "别的错"


# ---- 服务层（SQLite 内存库） --------------------------------------------------


@pytest.fixture()
def db():
    # StaticPool + 单连接：内存库默认每个连接各有一份数据，测试会看不到自己写的行。
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
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


def _finished(db, **kwargs) -> SlideDeck:
    """造一份已经生成完的 deck：create_deck 落的是 analyzing。"""
    deck = _deck(db)
    deck.status = STATUS_READY
    db.commit()
    return deck


def _await(value):
    async def run():
        return value
    return run()


def test_create_deck_requires_material(db):
    with pytest.raises(ValueError):
        _service(db).create_deck(title="空", description=None, requirement=None, source_text="  ")


def test_create_deck_requires_title(db):
    with pytest.raises(ValueError):
        _service(db).create_deck(title="  ", description=None, requirement=None, source_text="有正文")


def test_create_deck_starts_analyzing_with_cover_html(db):
    deck = _deck(db)
    assert deck.status == STATUS_ANALYZING
    assert deck.slides == []
    assert "slide-cover" in deck.html, "先渲染一张封面，未生成完也能打开预览"


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
    assert "slide-image" not in reloaded.html, "删图后引用它的页面降级"


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


def test_run_generation_writes_ready_state(db, monkeypatch):
    service = _service(db)
    deck = _deck(db)
    output = (
        '```json\n{"theme":{"preset":"paper","ratio":"16x9","accent":"#123456"},'
        '"slides":[{"layout":"cover","title":"复盘封面"},'
        '{"layout":"bullets","title":"结论","bullets":["增长 12%"]}]}\n```'
    )

    async def fake_ask(self, slug, user_prompt):
        assert slug == "slide-architect"
        return output

    monkeypatch.setattr(SlideService, "_ask", fake_ask)
    asyncio.run(service.run_generation(deck.id))

    done = service.get_deck(deck.id)
    assert done.status == STATUS_READY
    assert done.error_msg is None
    assert [page["layout"] for page in done.slides] == ["cover", "bullets"]
    assert done.theme["accent"] == "#123456"
    assert "增长 12%" in done.html


def test_run_generation_falls_back_when_model_gives_no_pages(db, monkeypatch):
    service = _service(db)
    deck = _deck(db)
    monkeypatch.setattr(SlideService, "_ask", lambda self, slug, prompt: _await('{"slides": []}'))
    asyncio.run(service.run_generation(deck.id))

    done = service.get_deck(deck.id)
    assert done.status == STATUS_ERROR
    assert "合法页面" in done.error_msg


def test_run_generation_records_model_exception(db, monkeypatch):
    service = _service(db)
    deck = _deck(db)

    async def fake_ask(self, slug, user_prompt):
        raise RuntimeError("上游 502")

    monkeypatch.setattr(SlideService, "_ask", fake_ask)
    asyncio.run(service.run_generation(deck.id))

    done = service.get_deck(deck.id)
    assert done.status == STATUS_ERROR
    assert "上游 502" in done.error_msg


def test_run_generation_error_on_unparsable_output(db, monkeypatch):
    service = _service(db)
    deck = _deck(db)
    monkeypatch.setattr(
        SlideService, "_ask", lambda self, slug, prompt: _await("我今天不返回 JSON")
    )
    asyncio.run(service.run_generation(deck.id))

    done = service.get_deck(deck.id)
    assert done.status == STATUS_ERROR
    assert "可解析" in done.error_msg


def test_start_generation_blocks_double_trigger(db):
    service = _service(db)
    deck = _finished(db)
    service.start_generation(deck.id, "只要三页")
    assert deck.requirement == "只要三页"
    assert deck.status == STATUS_ANALYZING
    with pytest.raises(ValueError):
        service.start_generation(deck.id)


def test_reset_stale_slides_marks_interrupted(db):
    deck = _deck(db)
    reset_stale_slides(db)
    db.expire_all()  # 批量 update 不会刷新 session 里的旧对象
    assert db.get(SlideDeck, deck.id).status == STATUS_ERROR
    assert "重启" in db.get(SlideDeck, deck.id).error_msg


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
