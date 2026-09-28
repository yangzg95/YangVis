"""智能办公 · 幻灯片 HTML 渲染器。

一份幻灯片在库里有两份表示：``slides``（结构化页面，唯一事实源，用户改的就是
它）和 ``html``（本模块从 slides 确定性渲染出来的自包含文档，预览 / 放映 /
导出三个入口共用）。之所以要把 HTML 也落库而不是让前端各渲染一遍：导出文件和
屏幕上看到的内容必须逐字一致，两套渲染器迟早会漂移。

安全前提 —— 文本全部来自模型输出与用户输入，属于不可信内容：

* 所有文本走 :func:`_e`（``html.escape``）；
* 版式 / 主题预设 / 比例 / 强调色 / 图片路径全部过白名单，非法值回落默认，
  任何字符串都不会原样拼进 CSS 或属性里；
* 文档里唯一的脚本是下面这段固定的导航脚本，不含任何插值。

正因为脚本是自带的、内容是转义的，前端才敢给预览和放映的 iframe 开
``sandbox="allow-scripts"``（翻页逻辑得在文档内部跑），而不是把交互全押在
父页面上。
"""
from __future__ import annotations

import html
import re
import uuid
from typing import Any, Dict, Iterable, List

from app.services.slides_store import resolve_url

# 单字段长度上限：与 schemas 里的声明一致，在这里再裁一次是因为模型输出不归
# Pydantic 管（它给什么我们收什么），而渲染器不能相信长度。
MAX_PAGE_TITLE = 200
MAX_PAGE_SUBTITLE = 400
MAX_PAGE_BULLET = 300
MAX_PAGE_BULLETS = 12
MAX_PAGE_NOTES = 4000
MAX_DECK_PAGES = 80

LAYOUTS = frozenset({"cover", "section", "bullets", "image", "quote", "closing"})

# 主题预设：只放颜色。尺寸一律用 --u（容器宽度的 1%），换预设不会撑破版面。
PRESETS: Dict[str, Dict[str, str]] = {
    "ink": {
        "bg": "#0d1117",
        "surface": "#161b22",
        "fg": "#f2f5f7",
        "muted": "#93a1af",
        "accent": "#4c8dff",
        "line": "rgba(255,255,255,0.14)",
    },
    "teal": {
        "bg": "#ffffff",
        "surface": "#f4f7f7",
        "fg": "#12222a",
        "muted": "#5a6b74",
        "accent": "#0a7e86",
        "line": "rgba(17,24,31,0.12)",
    },
    "paper": {
        "bg": "#f7f4ee",
        "surface": "#efe9df",
        "fg": "#241f19",
        "muted": "#6b6157",
        "accent": "#b4632b",
        "line": "rgba(36,31,25,0.14)",
    },
    "violet": {
        "bg": "#120a1f",
        "surface": "#1c1230",
        "fg": "#f4eefc",
        "muted": "#a58cc0",
        "accent": "#8b5cf6",
        "line": "rgba(255,255,255,0.14)",
    },
}
DEFAULT_PRESET = "teal"

RATIOS: Dict[str, str] = {"16x9": "16 / 9", "4x3": "4 / 3"}
DEFAULT_RATIO = "16x9"

_HEX_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_SID_RE = re.compile(r"^p[0-9a-f]{16}$")


def _e(value: Any) -> str:
    """转义一切要进 HTML 的东西。``quote=True``：属性值也走这个函数。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _text(value: Any, limit: int) -> str:
    """取一段纯文本：压掉首尾空白、限长，``None`` 变空串。"""
    if value is None:
        return ""
    return str(value).strip()[:limit]


# ---- 输入清洗 ----------------------------------------------------------------


def sanitize_theme(raw: Any) -> Dict[str, Any]:
    """把任意来源的主题字典收敛成 ``{preset, ratio, accent}``。"""
    data = raw if isinstance(raw, dict) else {}
    preset = data.get("preset")
    ratio = data.get("ratio")
    accent = data.get("accent")
    # 色号补上 # 再落库：省略 # 是手输的常态，而拼进 CSS 的值必须是我们自己
    # 组装的那一种形状，不能是用户给的原样字符串。
    accent_text = accent.strip() if isinstance(accent, str) else ""
    if accent_text and _HEX_RE.match(accent_text):
        accent_text = f"#{accent_text.lstrip('#')}"
    else:
        accent_text = ""
    return {
        "preset": preset if preset in PRESETS else DEFAULT_PRESET,
        "ratio": ratio if ratio in RATIOS else DEFAULT_RATIO,
        # 强调色是唯一允许自定义的样式值，而它要拼进 CSS 声明，所以只收
        # #rgb / #rrggbb；其余一律丢弃，回落预设色。
        "accent": accent_text,
    }


def sanitize_bullets(raw: Any) -> List[str]:
    """要点列表清洗：只收非空字符串、限条数、限每条长度。"""
    if not isinstance(raw, list):
        return []
    bullets: List[str] = []
    for item in raw[:MAX_PAGE_BULLETS]:
        text = _text(item, MAX_PAGE_BULLET)
        if text:
            bullets.append(text)
    return bullets


def sanitize_pages(raw: Any, asset_ids: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """把模型或前端给的一组页面收敛成合法结构，顺手补齐 ``sid``。

    ``sid`` 是页面级编辑的寻址键（与面试题的 ``qid`` 同一理由：数组下标在
    「一边删一边改」时会漂移）。合法形状的沿用，其余重发。

    引用了不存在图片的 ``image`` 页降级成 ``bullets`` 页 —— 放映时宁可少一张
    图，也不要留一个破图框。
    """
    known = {str(item) for item in asset_ids}
    source = raw if isinstance(raw, list) else []
    pages: List[Dict[str, Any]] = []
    for item in source:
        if len(pages) >= MAX_DECK_PAGES:
            break
        if not isinstance(item, dict):
            continue
        layout = item.get("layout")
        if layout not in LAYOUTS:
            layout = "bullets"
        asset_id = _text(item.get("asset_id"), 32)
        if asset_id and asset_id not in known:
            asset_id = ""
        if layout == "image" and not asset_id:
            layout = "bullets"
        sid = _text(item.get("sid"), 32)
        pages.append(
            {
                "sid": sid if _SID_RE.match(sid) else f"p{uuid.uuid4().hex[:16]}",
                "layout": layout,
                "title": _text(item.get("title"), MAX_PAGE_TITLE),
                "subtitle": _text(item.get("subtitle"), MAX_PAGE_SUBTITLE),
                "bullets": sanitize_bullets(item.get("bullets")),
                "asset_id": asset_id,
                "notes": _text(item.get("notes"), MAX_PAGE_NOTES),
            }
        )
    return pages


def asset_urls(assets: Any) -> Dict[str, str]:
    """把库里的 assets 列转成 ``{asset_id: 可访问 URL}``。

    只认 ``slides_store.is_safe_rel_path`` 认可的路径形状：这一列可能被手工改
    过，渲染器不该相信它干净。
    """
    urls: Dict[str, str] = {}
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict) or not asset.get("id"):
            continue
        url = resolve_url(str(asset.get("rel_path") or ""))
        if url:
            urls[str(asset["id"])] = url
    return urls


# ---- 渲染 --------------------------------------------------------------------


def render_document(
    *,
    title: str,
    theme: Any,
    pages: Any,
    assets: Any,
) -> str:
    """渲染整份可放映的自包含 HTML 文档。

    ``pages`` / ``assets`` 直接吃数据库里的 JSON 列，渲染器自己是最后一道白
    名单，调用方不需要预先清洗。
    """
    safe_theme = sanitize_theme(theme)
    urls = asset_urls(assets)
    safe_pages = sanitize_pages(pages, urls.keys())
    total = len(safe_pages) or 1

    if safe_pages:
        body = "\n".join(
            _render_page(index, page, urls, total)
            for index, page in enumerate(safe_pages)
        )
    else:
        # 还没生成 / 生成失败：给一张只有标题的封面页，放映页不至于白屏。
        body = _render_page(
            0,
            {"sid": "p0", "layout": "cover", "title": title or "幻灯片",
             "subtitle": "", "bullets": [], "asset_id": "", "notes": ""},
            urls,
            total,
        )

    colors = dict(PRESETS[safe_theme["preset"]])
    if safe_theme["accent"]:
        colors["accent"] = safe_theme["accent"]
    # 主题色单独一个 style：值全部来自 PRESETS 白名单或校验过的 hex，
    # 写在 _CSS 之后就能覆盖默认值，而 _CSS 本身保持常量、可缓存。
    # 必须用 ; 分隔：自定义属性（``--x``）的值是一段 token 流，只在 ``;`` 处
    # 结束，用换行分隔的话 ``--bg`` 会把后面所有声明整个吞进去，变成一个非法
    # 值 —— 于是 body 背景透明、主题永不生效，屏幕上就是一片底色黑。
    theme_css = ";".join(f"--{name}:{value}" for name, value in colors.items())
    ratio = RATIOS[safe_theme["ratio"]]
    doc_title = _e(title or "幻灯片")

    parts = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        f"<title>{doc_title}</title>",
        f"<style>{_CSS}</style>",
        f'<style>:root{{{theme_css};--ar:{ratio}}}</style>',
        "</head>",
        f'<body data-total="{total}">',
        f'<div class="stage" id="stage">\n{body}',
        '<button class="nav nav-prev" id="prev" type="button" aria-label="上一页">\u2039</button>',
        '<button class="nav nav-next" id="next" type="button" aria-label="下一页">\u203a</button>',
        f'<div class="hud" id="hud"><span id="page">1 / {total}</span>',
        '<button id="fs" type="button">全屏 (F)</button></div>',
        '<div class="bar"><i id="bar"></i></div>',
        "</div>",
        f"<script>{_SCRIPT}</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


def _para(class_name: str, text: str) -> str:
    """``<p>`` 的可选版：空文本不产出节点，免得留一堆空元素。"""
    return f'<p class="{class_name}">{text}</p>' if text else ""


def _render_page(index: int, page: Dict[str, Any], urls: Dict[str, str], total: int) -> str:
    layout = page["layout"]
    title = _e(page["title"])
    subtitle = _e(page["subtitle"])
    bullets = page["bullets"]
    notes = _e(page["notes"])
    chapter = f"{index:02d}"
    image_url = _e(urls.get(page["asset_id"], ""))

    if layout == "cover":
        body = _stack(
            "cover-inner",
            [
                f'<h1 class="cover-title">{title or "幻灯片"}</h1>',
                _para("cover-sub", subtitle),
            ],
        )
    elif layout == "section":
        body = _stack(
            "section-inner",
            [
                f'<span class="chapter">{chapter}</span>',
                f'<h2 class="section-title">{title}</h2>',
                _para("section-sub", subtitle),
            ],
        )
    elif layout == "image":
        figure = ""
        if image_url:
            figure = f'<figure class="shot"><img src="{image_url}" alt="{title}"></figure>'
        body = (
            _head(title, subtitle, chapter)
            + f'<div class="split">{_bullet_list(bullets)}{figure}</div>'
        )
    elif layout == "quote":
        body = _stack(
            "quote-inner",
            [
                f'<blockquote class="quote">{title or subtitle}</blockquote>',
                _para("quote-by", subtitle if title else ""),
                _bullet_list(bullets, class_name="quote-points"),
            ],
        )
    elif layout == "closing":
        body = _stack(
            "closing-inner",
            [
                f'<h2 class="closing-title">{title}</h2>',
                _para("closing-sub", subtitle),
                _bullet_list(bullets, class_name="closing-points"),
            ],
        )
    else:  # bullets
        body = _head(title, subtitle, chapter) + _bullet_list(bullets)

    note_tag = f'<aside class="notes">{notes}</aside>' if notes else ""
    return (
        f'<section class="slide slide-{layout}" id="{_e(page["sid"])}"'
        f' data-index="{index}" data-total="{total}">{body}{note_tag}</section>'
    )


def _stack(class_name: str, parts: List[str]) -> str:
    return f'<div class="stack {class_name}">{"".join(part for part in parts if part)}</div>'


def _head(title: str, subtitle: str, chapter: str) -> str:
    return (
        '<header class="page-head">'
        f'<span class="chapter">{chapter}</span>'
        f'<h2 class="page-title">{title}</h2>'
        + _para("page-sub", subtitle)
        + "</header>"
    )


def _bullet_list(bullets: List[str], *, class_name: str = "points") -> str:
    if not bullets:
        return ""
    items = "".join(f"<li>{_e(item)}</li>" for item in bullets)
    return f'<ul class="{class_name}">{items}</ul>'


# 尺寸一律以 --u 为单位，而 --u 是舞台宽度的百分之一：预览面板里 300px 宽和
# 全屏 1920px 宽下版面比例完全一致，不需要两套字号。容器查询单元拿不到时退回
# 按视口宽度计（老浏览器上小预览会偏大，但不会散架）。
_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{--u:1vw;--bg:#fff;--fg:#12222a;--muted:#5a6b74;--accent:#0a7e86;
--surface:#f4f7f7;--line:rgba(17,24,31,.12);--ar:16/9}
@supports (container-type:size){:root{--u:1cqw}}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);overflow:hidden;display:flex;
align-items:center;justify-content:center;font-family:"PingFang SC","Microsoft YaHei",
"Noto Sans SC","IBM Plex Sans",system-ui,-apple-system,sans-serif;
-webkit-font-smoothing:antialiased}
.stage{position:relative;width:min(100vw,calc(100vh*var(--ar)));aspect-ratio:var(--ar);
container-type:size;overflow:hidden}
.slide{position:absolute;inset:0;padding:calc(var(--u)*7) calc(var(--u)*8);
display:none;flex-direction:column;justify-content:center;gap:calc(var(--u)*2.6);
animation:fade .28s ease}
.slide.is-active{display:flex}
@keyframes fade{from{opacity:0;transform:translateY(calc(var(--u)*.8))}to{opacity:1;transform:none}}
.stack{display:flex;flex-direction:column;gap:calc(var(--u)*2.4)}
.chapter{font-variant-numeric:tabular-nums;font-size:calc(var(--u)*1.6);
color:var(--muted);letter-spacing:.16em}
.page-head{display:flex;flex-direction:column;gap:calc(var(--u)*1.1)}
.page-title{margin:0;font-size:calc(var(--u)*4.2);line-height:1.2;font-weight:700;
letter-spacing:-.01em;max-width:32em}
.page-sub{margin:0;color:var(--muted);font-size:calc(var(--u)*2);line-height:1.5}
.points,.quote-points,.closing-points{margin:0;padding:0;list-style:none;
display:flex;flex-direction:column;gap:calc(var(--u)*1.7)}
.points li,.closing-points li{position:relative;padding-left:calc(var(--u)*3);
font-size:calc(var(--u)*2.4);line-height:1.5}
.points li::before,.closing-points li::before{content:"";position:absolute;
left:0;top:calc(var(--u)*.95);width:calc(var(--u)*1);height:calc(var(--u)*1);
border-radius:50%;background:var(--accent)}
.slide-cover,.slide-closing,.slide-section{align-items:flex-start;
background:radial-gradient(120% 140% at 8% 0%,var(--surface) 0%,var(--bg) 60%)}
.cover-inner,.closing-inner{gap:calc(var(--u)*3)}
.cover-title{margin:0;font-size:calc(var(--u)*7.4);line-height:1.1;font-weight:800;
letter-spacing:-.02em;max-width:24em}
.cover-title::after{content:"";display:block;width:calc(var(--u)*9);
height:calc(var(--u)*.6);margin-top:calc(var(--u)*2.4);background:var(--accent);
border-radius:999px}
.cover-sub,.closing-sub{margin:0;color:var(--muted);font-size:calc(var(--u)*2.6);
line-height:1.5;max-width:30em}
.section-inner{gap:calc(var(--u)*2)}
.section-title{margin:0;font-size:calc(var(--u)*6);line-height:1.15;font-weight:800;max-width:22em}
.section-sub{margin:0;color:var(--muted);font-size:calc(var(--u)*2.4);max-width:30em}
.split{display:grid;grid-template-columns:1fr calc(var(--u)*42);gap:calc(var(--u)*5);
align-items:center}
.shot{margin:0;display:flex;align-items:center;justify-content:center;
background:var(--surface);border:1px solid var(--line);border-radius:calc(var(--u)*1.2);
padding:calc(var(--u)*1.6);max-height:calc(var(--u)*54)}
.shot img{max-width:100%;max-height:calc(var(--u)*50);display:block;border-radius:calc(var(--u)*.6)}
.quote-inner{gap:calc(var(--u)*3)}
.quote{margin:0;padding-left:calc(var(--u)*3);border-left:calc(var(--u)*.8) solid var(--accent);
font-size:calc(var(--u)*4.6);line-height:1.3;font-weight:700;max-width:26em}
.quote-by{margin:0;color:var(--muted);font-size:calc(var(--u)*2.2)}
.quote-points li{font-size:calc(var(--u)*2.2);color:var(--muted)}
.notes{position:absolute;bottom:calc(var(--u)*6);left:calc(var(--u)*8);
right:calc(var(--u)*8);display:none;font-size:calc(var(--u)*1.7);color:var(--muted)}
.slide.show-notes .notes{display:block}
.nav{position:absolute;top:0;bottom:0;width:18%;border:0;background:transparent;
color:var(--muted);font:inherit;font-size:calc(var(--u)*4);cursor:pointer;opacity:0;
transition:opacity .2s;display:flex;align-items:center;z-index:3}
.nav-prev{left:0;justify-content:flex-start;padding-left:calc(var(--u)*2)}
.nav-next{right:0;justify-content:flex-end;padding-right:calc(var(--u)*2)}
.stage:hover .nav,.stage:hover .hud{opacity:.9}
.nav:hover{opacity:1}
.hud{position:absolute;right:calc(var(--u)*3);bottom:calc(var(--u)*2.4);z-index:4;
display:flex;align-items:center;gap:calc(var(--u)*1.6);font-size:calc(var(--u)*1.7);
color:var(--muted);opacity:0;transition:opacity .2s}
.hud button{border:1px solid var(--line);background:var(--surface);color:inherit;
border-radius:999px;padding:calc(var(--u)*.5) calc(var(--u)*1.4);font:inherit;cursor:pointer}
.bar{position:absolute;left:0;right:0;bottom:0;height:calc(var(--u)*.4);
background:var(--line);z-index:4}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}
@media print{
  html,body{height:auto;overflow:visible}
  body{display:block;background:#fff}
  .stage{width:100%;aspect-ratio:auto;container-type:normal;--u:1vw;overflow:visible}
  .nav,.hud,.bar{display:none!important}
  .slide{position:relative;inset:auto;display:flex!important;aspect-ratio:var(--ar);
  width:100%;page-break-after:always;animation:none}
  .notes{display:block}
}
"""

# 固定的导航脚本：不含任何插值，页面内容对它只读。翻页（键盘 / 点击 / 滚轮 /
# 触摸）、全屏、URL hash 定位（刷新停在同一页）、备注开关。
_SCRIPT = r"""
(function () {
  'use strict';
  var stage = document.getElementById('stage');
  var slides = Array.prototype.slice.call(document.querySelectorAll('.slide'));
  var pageLabel = document.getElementById('page');
  var bar = document.getElementById('bar');
  var index = 0;
  var digits = '';
  var digitTimer = null;

  function clamp(value) {
    return Math.max(0, Math.min(slides.length - 1, value));
  }

  // 父页面（放映页的工具条）需要知道停在第几页，改完内容也要把画面送回同一页。
  // 双向的都是下面这几种固定类型的消息，内容不参与：文档是 sandbox 出来的独立
  // 源，父页面读不到它的 location，只能走 postMessage。
  function report() {
    try {
      window.parent.postMessage({ __slide: 'page', index: index, total: slides.length }, '*');
    } catch (err) {}
  }

  function show(next, writeHash) {
    index = clamp(next);
    slides.forEach(function (el, i) { el.classList.toggle('is-active', i === index); });
    if (pageLabel) { pageLabel.textContent = (index + 1) + ' / ' + slides.length; }
    if (bar) { bar.style.width = ((index + 1) / slides.length * 100) + '%'; }
    if (writeHash !== false) {
      try { history.replaceState(null, '', '#' + (index + 1)); } catch (err) {}
    }
    report();
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) { document.exitFullscreen(); }
    else if (stage.requestFullscreen) { stage.requestFullscreen(); }
  }

  function toggleNotes() {
    slides.forEach(function (el) { el.classList.toggle('show-notes'); });
  }

  // 数字键是「跳到第 N 页」：停顿 400ms 再落定，否则按 1 会被当成第一页立即生效。
  function commitDigits() {
    if (!digits) { return; }
    show(Number(digits) - 1);
    digits = '';
  }

  document.addEventListener('keydown', function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) { return; }
    var key = event.key;
    if (key === 'ArrowRight' || key === 'ArrowDown' || key === 'PageDown' || key === ' ') {
      event.preventDefault(); show(index + 1);
    } else if (key === 'ArrowLeft' || key === 'ArrowUp' || key === 'PageUp') {
      event.preventDefault(); show(index - 1);
    } else if (key === 'Home') { event.preventDefault(); show(0); }
    else if (key === 'End') { event.preventDefault(); show(slides.length - 1); }
    else if (key === 'f' || key === 'F') { event.preventDefault(); toggleFullscreen(); }
    else if (key === 's' || key === 'S') { toggleNotes(); }
    else if (key === 'Escape' && document.fullscreenElement) { toggleFullscreen(); }
    else if (/^[0-9]$/.test(key)) {
      digits = digits + key;
      clearTimeout(digitTimer);
      digitTimer = setTimeout(commitDigits, 400);
    }
  }, true);

  document.getElementById('next').addEventListener('click', function () { show(index + 1); });
  document.getElementById('prev').addEventListener('click', function () { show(index - 1); });
  document.getElementById('fs').addEventListener('click', toggleFullscreen);

  var wheelLock = 0;
  stage.addEventListener('wheel', function (event) {
    if (Math.abs(event.deltaY) < 24) { return; }
    var now = Date.now();
    if (now - wheelLock < 320) { return; }
    wheelLock = now;
    show(index + (event.deltaY > 0 ? 1 : -1));
  }, { passive: true });

  var touchX = null;
  stage.addEventListener('touchstart', function (event) {
    touchX = event.touches[0].clientX;
  }, { passive: true });
  stage.addEventListener('touchend', function (event) {
    if (touchX === null) { return; }
    var delta = event.changedTouches[0].clientX - touchX;
    if (Math.abs(delta) > 40) { show(index + (delta < 0 ? 1 : -1)); }
    touchX = null;
  }, { passive: true });

  window.addEventListener('hashchange', function () {
    var value = parseInt((location.hash || '').replace('#', ''), 10);
    show(isNaN(value) ? 0 : value - 1, false);
  });

  // 来自父页面的翻页指令。只认这几种 action，且都映射到上面的内部函数，
  // 消息里的任何字符串都不会被当成 HTML 或选择器执行。
  window.addEventListener('message', function (event) {
    var data = event.data;
    if (!data || data.__slide !== 'command') { return; }
    if (data.action === 'goto') { show(Number(data.index) || 0); }
    else if (data.action === 'next') { show(index + 1); }
    else if (data.action === 'prev') { show(index - 1); }
    else if (data.action === 'fullscreen') { toggleFullscreen(); }
  });

  // Ctrl+P 导出 PDF：浏览器打印只收当前可见页，先把所有页点亮。
  var media = window.matchMedia('(print)');
  function forPrint(matched) {
    if (matched) { slides.forEach(function (el) { el.classList.add('is-active'); }); }
    else { show(index, false); }
  }
  if (media.addEventListener) { media.addEventListener('change', function (e) { forPrint(e.matches); }); }
  else if (media.addListener) { media.addListener(function (e) { forPrint(e.matches); }); }

  var initial = parseInt((location.hash || '').replace('#', ''), 10);
  show(isNaN(initial) ? 0 : initial - 1, false);
})();
"""
