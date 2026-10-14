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

from app.services.slides.store import resolve_url

# 单字段长度上限：与 schemas 里的声明一致，在这里再裁一次是因为模型输出不归
# Pydantic 管（它给什么我们收什么），而渲染器不能相信长度。
MAX_PAGE_KICKER = 24
MAX_PAGE_TITLE = 200
MAX_PAGE_SUBTITLE = 400
MAX_PAGE_BULLET = 300
MAX_PAGE_BULLETS = 12
MAX_PAGE_NOTES = 4000
MAX_TAKEAWAY = 160
MAX_DECK_PAGES = 80

# 对比表与步骤条的硬上限：这两个版式不参与字号降档（表格缩字号会糊），装不下
# 就是模型该拆页，渲染器只负责裁掉越界的部分。
MAX_TABLE_COLUMNS = 4
MAX_TABLE_ROWS = 6
MAX_TABLE_CELL = 40
MAX_STEPS = 5
MAX_STEP_TITLE = 40
MAX_STEP_DESC = 80

# 链路与分层图：条数与字数都卡死，因为这两个版式的格子宽高是渲染器自己按版面算的，
# 内容越长格子越窄，超出去就不是「挤」而是糊掉。环节名字是框里的标签不是句子，
# 所以比步骤条的标题还短。
# 分层图的字数是倒着算出来的：最紧的是方块那一格 —— 末档 1.3u 字号下一格只塞得下
# 10 个字，第 11 个字换成两行，5 层 × 4 块的整块高度就把底部结论条挤出可见区。字号
# 不能再往下缩（1.3u 是能读的底线），所以宁可裁字。两处边界都由
# test_structured_pages_fit_their_own_limits 钉住。
MAX_FLOW = 5
MAX_FLOW_TITLE = 16
MAX_FLOW_DESC = 32
MAX_FLOW_VIA = 10
FLOW_MIN_NODES = 2
MAX_ARCH_LAYERS = 5
MAX_ARCH_BLOCKS = 4
MAX_ARCH_NAME = 10
MAX_ARCH_BLOCK = 10
ARCH_MIN_LAYERS = 2

LAYOUTS = frozenset(
    {
        "cover",
        "section",
        "bullets",
        "image",
        "quote",
        "closing",
        "table",
        "steps",
        "flow",
        "arch",
    }
)

# 标题钉在上沿、正文在剩下的空间里居中的版式；其余版式整块居中。
PINNED_LAYOUTS = frozenset(
    {"bullets", "image", "table", "steps", "flow", "arch"}
)
# 底部结论句的适用范围：封面是标识页，不放正文性的结论条。
TAKEAWAY_LAYOUTS = frozenset(LAYOUTS - {"cover"})

# 要点列表的降档表：(类名后缀, 字号 u, 行高倍数, 条目间距 u, 每行字数相对系数)。
# 单行高度 = 字号 × 行高；系数是该档字号相对默认 2.4u 的倍数（字越小一行塞得越
# 多）。这些数字必须和 _CSS 里 .points / .points.dense / .points.denser 写的一模
# 一样，否则估算是照着另一套版面算的 —— test_bullet_tier_numbers_match_the_css
# 就是盯这件事的。垂直预算由 :func:`_body_budget_u` 按页头的真实行数算出来。
_BULLET_TIERS = (
    ("", 2.4, 1.5, 1.7, 1.0),
    (" dense", 1.95, 1.4, 1.15, 1.23),
    (" denser", 1.6, 1.35, 0.9, 1.5),
)

# 底部结论句在垂直方向上的开销：字号 × 行高 + 上下内边距 + 与正文的间距。
# 它和 _BULLET_TIERS 同一个道理 —— 预算要按版面真实占掉的高度扣，改了 CSS
# 就得改这里，否则结论条会把最后一条要点顶出舞台。
_TAKEAWAY_FONT_U = 1.9
_TAKEAWAY_LINE_H = 1.35
_TAKEAWAY_PAD_U = 0.95
_TAKEAWAY_GAP_U = 2.2
_TAKEAWAY_U = (
    _TAKEAWAY_FONT_U * _TAKEAWAY_LINE_H + _TAKEAWAY_PAD_U * 2 + _TAKEAWAY_GAP_U
)

# ---- 链路与分层图的版面数（改了 CSS 就要改这里，同 _BULLET_TIERS 一个道理）-----
#
# 这两个版式的格子宽高是渲染器按版面算出来的，「装不装得下」只能在这儿估：每档给
# (类名后缀, 主字号 u, 行高, 内容上下内边距 u, ...)，估出来的高度超过正文预算就降一档。
# 箭头画在格子之间的留白里，不额外占高度。
#
# 版面的可用宽 = 100u 减左右各 8u 内边距（见 .slide 的 padding）。这里的数字要写
# 成 ``84.0`` 而不是 ``84``，因为测试是拿 ``repr`` 去 CSS 里找同一段字符串的。
_CONTENT_W_U = 84.0

_FLOW_TIERS = (
    ("", 1.9, 1.3, 1.0),
    (" dense", 1.65, 1.3, 0.85),
    (" denser", 1.45, 1.3, 0.7),
)
# 说明文字相对环节名字号的倍数（决定一行塞得下几个字）。
_FLOW_DESC_RATIO = 0.8
# 一个箭头槽（含左右留白）占的宽度、格子的左右内边距、名字与说明的行间距。
_FLOW_LINK_W_U = 6.5
_FLOW_PAD_X_U = 0.9
_FLOW_LABEL_GAP_U = 0.55

# 档位里第 4 个数是方块的上下内边距，第 5 个是层与层之间的间距。
_ARCH_TIERS = (
    ("", 1.8, 1.3, 0.85, 1.9),
    (" dense", 1.6, 1.3, 0.7, 1.5),
    (" denser", 1.3, 1.3, 0.5, 1.0),
)
# 层名列宽、列间与方块间距、方块左右内边距：都不随档位变，省一处对不上的风险。
_ARCH_NAME_W_U = 15.0
_ARCH_COL_GAP_U = 2.0
_ARCH_BLOCK_GAP_U = 1.0
_ARCH_PAD_X_U = 0.7
# 格子的 1px 描边：CSS 里它是像素单位，不跟着 --u 缩放，一边折成 100/舞台宽 的 u。
# 0.2u 取的是 500px 舞台。实测最紧的一页（16:9、五层各四块、文案全顶上限）在
# 360px 舞台还剩 0.5u、320px 剩 0.3u，到 280px 才溢出；1920px 放映时它只值 0.05u，
# 于是整页估算偏保守约 1.7u —— 挑出来的档位已是最矮的一档，降无可降，所以不动。
# 改描边、内边距或档位字号后要重测，就照这四个舞台宽度量。
_BOX_BORDER_U = 0.2

# 主题预设：只放颜色。尺寸一律用 --u（容器宽度的 1%），换预设不会撑破版面。
# ``t2/t3/t4`` 是对比表表头和步骤条用的第二组色：它们跟 accent 一起按预设给，
# 深色预设下要提亮，否则小字号的彩色文字在白底够、在黑底糊。
PRESETS: Dict[str, Dict[str, str]] = {
    # amber 是默认，也是放映时的基准长相：白纸 + 橙色强调，正文近黑。
    # 演示文稿要在投影上看，浅底高对比比深色主题更稳。
    "amber": {
        "bg": "#ffffff",
        "surface": "#f7f5f2",
        "fg": "#14181d",
        "muted": "#6b7280",
        "accent": "#ea580c",
        "line": "rgba(20,24,29,0.10)",
        "t2": "#2563eb",
        "t3": "#0d9488",
        "t4": "#7c3aed",
    },
    "ink": {
        "bg": "#0d1117",
        "surface": "#161b22",
        "fg": "#f2f5f7",
        "muted": "#93a1af",
        "accent": "#4c8dff",
        "line": "rgba(255,255,255,0.14)",
        "t2": "#60a5fa",
        "t3": "#2dd4bf",
        "t4": "#a78bfa",
    },
    "teal": {
        "bg": "#ffffff",
        "surface": "#f4f7f7",
        "fg": "#12222a",
        "muted": "#5a6b74",
        "accent": "#0a7e86",
        "line": "rgba(17,24,31,0.12)",
        "t2": "#2563eb",
        "t3": "#7c3aed",
        "t4": "#0891b2",
    },
    "paper": {
        "bg": "#f7f4ee",
        "surface": "#efe9df",
        "fg": "#241f19",
        "muted": "#6b6157",
        "accent": "#b4632b",
        "line": "rgba(36,31,25,0.14)",
        "t2": "#1e40af",
        "t3": "#166534",
        "t4": "#6d28d9",
    },
    "violet": {
        "bg": "#120a1f",
        "surface": "#1c1230",
        "fg": "#f4eefc",
        "muted": "#a58cc0",
        "accent": "#8b5cf6",
        "line": "rgba(255,255,255,0.14)",
        "t2": "#60a5fa",
        "t3": "#2dd4bf",
        "t4": "#f472b6",
    },
}
DEFAULT_PRESET = "amber"

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


# 要点里的「**引导词**：说明」是这份版面的主要层次手段（参考课件就是这么排的）。
# 星号本身不会被 html.escape 改动，所以转义之后再替换是安全的：能被包进
# ``<strong>`` 的只有已经转义过的内容。
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _rich(value: Any, limit: int) -> str:
    """转义后把成对的 ``**…**`` 变成 ``<strong>``。"""
    return _BOLD_RE.sub(
        lambda match: f"<strong>{match.group(1)}</strong>", _e(_text(value, limit))
    )


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


def sanitize_table(raw: Any) -> Dict[str, Any]:
    """对比表清洗：``{columns: [...], rows: [[...], ...]}``。

    行内格子数不足按空串补齐、超出裁掉，保证渲染时每一行都和表头列数对齐；
    整行全空（模型偶尔留分隔行）直接丢掉。表头缺失时按列数给空串，版式仍成立。
    """
    data = raw if isinstance(raw, dict) else {}
    columns = [
        _text(item, MAX_TABLE_CELL)
        for item in (data.get("columns") or [])[:MAX_TABLE_COLUMNS]
    ][:MAX_TABLE_COLUMNS]
    width = len(columns)
    rows: List[List[str]] = []
    source = data.get("rows")
    if width and isinstance(source, list):
        for row in source[:MAX_TABLE_ROWS]:
            if not isinstance(row, list):
                continue
            cells = [_text(cell, MAX_TABLE_CELL) for cell in row[:width]]
            cells += [""] * (width - len(cells))
            if any(cells):
                rows.append(cells)
    return {"columns": columns, "rows": rows}


def sanitize_steps(raw: Any) -> List[Dict[str, str]]:
    """步骤条清洗：``[{title, desc}, ...]``，``desc`` 可以空。"""
    source = raw if isinstance(raw, list) else []
    steps: List[Dict[str, str]] = []
    for item in source[:MAX_STEPS]:
        if isinstance(item, dict):
            title = _text(item.get("title"), MAX_STEP_TITLE)
            desc = _text(item.get("desc"), MAX_STEP_DESC)
        else:
            # 允许直接给一串字符串：只有标题的步骤同样成立。
            title = _text(item, MAX_STEP_TITLE)
            desc = ""
        if title:
            steps.append({"title": title, "desc": desc})
    return steps


def sanitize_flow(raw: Any) -> List[Dict[str, str]]:
    """链路清洗：``[{title, desc, via}, ...]``，``desc`` / ``via`` 可以空。

    ``via`` 是「从这个环节走出去」的条件文字，画在环节右侧的箭头上，所以最后一
    格的 ``via`` 没有箭头可挂，直接清掉——库里不留渲染不出来的字段。
    """
    source = raw if isinstance(raw, list) else []
    nodes: List[Dict[str, str]] = []
    for item in source[:MAX_FLOW]:
        if isinstance(item, dict):
            title = _text(item.get("title"), MAX_FLOW_TITLE)
            desc = _text(item.get("desc"), MAX_FLOW_DESC)
            via = _text(item.get("via"), MAX_FLOW_VIA)
        else:
            # 允许直接给一串字符串：只有名字的链路同样成立。
            title = _text(item, MAX_FLOW_TITLE)
            desc = via = ""
        if title:
            nodes.append({"title": title, "desc": desc, "via": via})
    if nodes:
        nodes[-1]["via"] = ""
    return nodes


def sanitize_layers(raw: Any) -> List[Dict[str, Any]]:
    """分层图清洗：``[{name, blocks: [...]}, ...]``，自上而下的顺序就是数组顺序。

    层名和方块至少得有一个：一层既没名字又没方块，在屏幕上就是一条空带。
    """
    source = raw if isinstance(raw, list) else []
    layers: List[Dict[str, Any]] = []
    for item in source[:MAX_ARCH_LAYERS]:
        if not isinstance(item, dict):
            continue
        raw_blocks = item.get("blocks")
        blocks: List[str] = []
        for block in (raw_blocks if isinstance(raw_blocks, list) else [])[:MAX_ARCH_BLOCKS]:
            text = _text(block, MAX_ARCH_BLOCK)
            if text:
                blocks.append(text)
        name = _text(item.get("name"), MAX_ARCH_NAME)
        if name or blocks:
            layers.append({"name": name, "blocks": blocks})
    return layers


def sanitize_pages(
    raw: Any,
    asset_ids: Iterable[str] = (),
    *,
    drop_empty_content: bool = False,
) -> List[Dict[str, Any]]:
    """把模型或前端给的一组页面收敛成合法结构，顺手补齐 ``sid``。

    ``sid`` 是页面级编辑的寻址键（与面试题的 ``qid`` 同一理由：数组下标在
    「一边删一边改」时会漂移）。合法形状的沿用，其余重发。

    引用了不存在图片的 ``image`` 页降级成 ``bullets`` 页 —— 放映时宁可少一张
    图，也不要留一个破图框。``table`` / ``steps`` / ``flow`` / ``arch`` 同理：
    结构没填全（表格只有表头、链路只剩一个环节、分层图只有一层）就退回要点页，
    比在屏幕上留一个空壳好；链路和分层退回来时把格子里的话搬进要点，内容不跟着
    版式一起消失。

    ``drop_empty_content`` 只给生成侧：模型偶尔交来一个「什么都没填」的正文页
    （步骤数组空、要点也空），那种页在屏幕上就是一行标题，收进来不如不要。工作台
    保存时不开这个开关——用户把一页清空是编辑途中的常态，那页得留着继续改。
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
        layout_data = sanitize_table(item.get("table"))
        steps_data = sanitize_steps(item.get("steps"))
        flow_data = sanitize_flow(item.get("flow"))
        layers_data = sanitize_layers(item.get("layers"))
        bullets = sanitize_bullets(item.get("bullets"))
        if layout == "table" and not (layout_data["columns"] and layout_data["rows"]):
            layout = "bullets"
        if layout == "steps" and not steps_data:
            layout = "bullets"
        # 一格链路、一层架构在屏幕上不成图，退回要点页；但格子里那句话是模型想说
        # 的内容，跟着版式一起丢掉就变成空页了，所以搬进要点再退。
        if layout == "flow" and len(flow_data) < FLOW_MIN_NODES:
            layout = "bullets"
            bullets = bullets or [
                f"{node['title']}：{node['desc']}" if node["desc"] else node["title"]
                for node in flow_data
            ]
        if layout == "arch" and len(layers_data) < ARCH_MIN_LAYERS:
            layout = "bullets"
            bullets = bullets or [
                "：".join(
                    part for part in (layer["name"], "、".join(layer["blocks"])) if part
                )
                for layer in layers_data
            ]
        if drop_empty_content and layout in PINNED_LAYOUTS:
            if not (
                bullets
                or layout_data["columns"]
                or steps_data
                or flow_data
                or layers_data
                or asset_id
            ):
                continue
        pages.append(
            {
                "sid": sid if _SID_RE.match(sid) else f"p{uuid.uuid4().hex[:16]}",
                "layout": layout,
                "kicker": _text(item.get("kicker"), MAX_PAGE_KICKER),
                "title": _text(item.get("title"), MAX_PAGE_TITLE),
                "subtitle": _text(item.get("subtitle"), MAX_PAGE_SUBTITLE),
                "bullets": bullets,
                "table": layout_data,
                "steps": steps_data,
                "flow": flow_data,
                "layers": layers_data,
                "takeaway": _text(item.get("takeaway"), MAX_TAKEAWAY),
                "asset_id": asset_id,
                "notes": _text(item.get("notes"), MAX_PAGE_NOTES),
            }
        )
    return pages


def asset_urls(assets: Any) -> Dict[str, str]:
    """把库里的 assets 列转成 ``{asset_id: 可访问 URL}``。

    只认 ``slides.store.is_safe_rel_path`` 认可的路径形状：这一列可能被手工改
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
    if not safe_pages:
        # 还没生成 / 生成失败：给一张只有标题的封面页，放映页不至于白屏。这张兜底
        # 页也走 sanitize_pages，免得在这里手抄一份页面结构、加字段时漏掉一边。
        safe_pages = sanitize_pages([{"layout": "cover", "title": title or "幻灯片"}])
    total = len(safe_pages)
    ratio = RATIOS[safe_theme["ratio"]]
    # 画幅决定正文能占多高：要点降档与配图的高度上限都用它算，4:3 不该按 16:9 压字。
    content_h_u = _content_height_u(ratio)
    # 页眉那条 running header 用整份的主题名；已经转义过，页面里直接拼。
    deck_label = _e(title or "幻灯片")

    body = "\n".join(
        _render_page(index, page, urls, total, content_h_u, deck_label)
        for index, page in enumerate(safe_pages)
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

    parts = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        f"<title>{deck_label}</title>",
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


def _render_page(
    index: int,
    page: Dict[str, Any],
    urls: Dict[str, str],
    total: int,
    content_h_u: float,
    deck_label: str,
) -> str:
    layout = page["layout"]
    title = _e(page["title"])
    subtitle = _e(page["subtitle"])
    bullets = page["bullets"]
    notes = _e(page["notes"])
    chapter = f"{index:02d}"
    image_url = _e(urls.get(page["asset_id"], ""))
    # 页头吃掉多少垂直空间决定要点要不要降档，用未转义的原值估算。
    budget_u = _body_budget_u(page, content_h_u)
    head = _head(title, subtitle, chapter, page["kicker"])

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
        # 左列要点和配图共用同一份剩余高度：把预算以 --body-h 交给 CSS，图片再高
        # 也只长到这一步，越高的图只会自己缩下去，不会把整页顶出舞台。
        # 配图页的要点只占左列（约整页宽的三分之一），同样的字数在这里更早溢出。
        split_style = f' style="--body-h:calc(var(--u)*{budget_u:.2f})"'
        body = _wrap_body(
            head,
            f'<div class="split"{split_style}>'
            f"{_bullet_list(bullets, per_line=15, budget_u=budget_u)}{figure}</div>",
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
    elif layout == "table":
        body = _wrap_body(head, _table(page["table"]))
    elif layout == "steps":
        body = _wrap_body(head, _steps(page["steps"]))
    elif layout == "flow":
        body = _wrap_body(head, _flow(page["flow"], budget_u))
    elif layout == "arch":
        body = _wrap_body(head, _arch(page["layers"], budget_u))
    else:  # bullets
        body = _wrap_body(
            head, _bullet_list(bullets, per_line=28, budget_u=budget_u)
        )

    parts = [_rail(deck_label, index, total), body]
    if layout in TAKEAWAY_LAYOUTS:
        parts.append(_takeaway(page))
    body = "".join(parts)

    note_tag = f'<aside class="notes">{notes}</aside>' if notes else ""
    return (
        f'<section class="slide slide-{layout}" id="{_e(page["sid"])}"'
        f' data-index="{index}" data-total="{total}">{body}{note_tag}</section>'
    )


def _rail(deck_label: str, index: int, total: int) -> str:
    """页眉那条 hairline：左边整份的主题名，右边页码（封面不标页码）。"""
    page_no = f'<span class="rail-no">{index + 1:02d}<i>/</i>{total:02d}</span>' if index else ""
    return f'<header class="rail"><span class="rail-tag">{deck_label}</span>{page_no}</header>'


def _wrap_body(head: str, inner: str) -> str:
    """``页头 + .body``：正文包进 ``.body`` 才拿得到上下 auto 外边距，
    底部那条结论句也就有了贴着下沿的地方。"""
    return head + f'<div class="body">{inner}</div>'


def _takeaway(page: Dict[str, Any]) -> str:
    text = _rich(page["takeaway"], MAX_TAKEAWAY)
    return f'<p class="takeaway">{text}</p>' if text else ""


def _table(data: Dict[str, Any]) -> str:
    """对比表：表头按列循环配色，首列是深色标签列。"""
    head = "".join(f"<th>{_e(cell)}</th>" for cell in data["columns"])
    rows = "".join(
        "<tr>" + "".join(f"<td>{_rich(cell, MAX_TABLE_CELL)}</td>" for cell in row) + "</tr>"
        for row in data["rows"]
    )
    return (
        f'<div class="cmp-wrap"><table class="cmp">'
        f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _steps(items: List[Dict[str, str]]) -> str:
    rows = "".join(
        '<li><span class="no">{no:02d}</span><span class="st">{title}</span>{desc}</li>'.format(
            no=i + 1,
            title=_e(step["title"]),
            desc=(
                f'<span class="sd">{_rich(step["desc"], MAX_STEP_DESC)}</span>'
                if step["desc"]
                else ""
            ),
        )
        for i, step in enumerate(items)
    )
    return f'<ol class="steps">{rows}</ol>'


def _flow(nodes: List[Dict[str, str]], budget_u: float) -> str:
    """链路图：方框横向串成一条线，箭头上的小字是走到下一格的条件。

    最后一格的 ``via`` 在清洗时已经清掉了，所以箭头永远比格子少一条。
    """
    if not nodes:
        return ""
    cells: List[str] = []
    for index, node in enumerate(nodes):
        if index:
            via = f'<em>{_rich(node["via"], MAX_FLOW_VIA)}</em>' if node["via"] else ""
            cells.append(f'<span class="link">\u2192{via}</span>')
        desc = (
            f'<span class="nd">{_rich(node["desc"], MAX_FLOW_DESC)}</span>'
            if node["desc"]
            else ""
        )
        cells.append(
            f'<span class="node"><span class="nt">{_e(node["title"])}</span>{desc}</span>'
        )
    return f'<div class="flow{_flow_tier(nodes, budget_u)}">{"".join(cells)}</div>'


def _arch(layers: List[Dict[str, Any]], budget_u: float) -> str:
    """分层图：一列层名 + 该层的模块方块，数组顺序自上而下。"""
    if not layers:
        return ""
    rows = "".join(
        '<div class="layer">'
        f'<span class="lname">{_e(layer["name"])}</span>'
        '<span class="lblocks">'
        + "".join(
            f'<span class="blk">{_rich(block, MAX_ARCH_BLOCK)}</span>'
            for block in layer["blocks"]
        )
        + "</span></div>"
        for layer in layers
    )
    return f'<div class="arch{_arch_tier(layers, budget_u)}">{rows}</div>'


def _stack(class_name: str, parts: List[str]) -> str:
    return f'<div class="stack {class_name}">{"".join(part for part in parts if part)}</div>'


def _head(title: str, subtitle: str, chapter: str, kicker: str) -> str:
    """页头。有小标签时用标签（强调色），没有时退回章节序号。"""
    label = (
        f'<span class="kicker">{_e(kicker)}</span>'
        if kicker
        else f'<span class="chapter">{chapter}</span>'
    )
    return (
        '<header class="page-head">'
        + label
        + f'<h2 class="page-title">{title}</h2>'
        + _para("page-sub", subtitle)
        + "</header>"
    )


def _bullet_list(
    bullets: List[str],
    *,
    class_name: str = "points",
    per_line: int = 0,
    budget_u: float = 0.0,
) -> str:
    """渲染要点列表。

    ``per_line`` 是默认字号下一行放得下的字数，``budget_u`` 是正文还能占的垂直
    空间（单位 ``--u``）；两者都给了才参与降档，``per_line=0`` 表示不降档（引用
    页 / 结尾页的列表本来就小，外层还是整块居中的版式）。
    """
    if not bullets:
        return ""
    items = "".join(f"<li>{_rich(item, MAX_PAGE_BULLET)}</li>" for item in bullets)
    tier = _bullet_tier(bullets, per_line, budget_u)
    return f'<ul class="{class_name}{tier}">{items}</ul>'


def _bullet_tier(bullets: List[str], per_line: int, budget_u: float) -> str:
    """按估算的占行数挑一档字号：生成侧要求每页不超过 5 条，这里是超出时的兜底。"""
    if per_line <= 0 or budget_u <= 0:
        return ""
    for suffix, font_u, line_h, gap_u, width_factor in _BULLET_TIERS:
        chars = max(6, int(per_line * width_factor))
        lines = sum(max(1, -(-len(item) // chars)) for item in bullets)
        if lines * font_u * line_h + (len(bullets) - 1) * gap_u <= budget_u:
            return suffix
    return _BULLET_TIERS[-1][0]


def _wrapped_lines(text: str, chars_per_line: float) -> int:
    """估一段文字占几行：版面宽度除以字号就是一行塞得下的字数（按一字宽算）。

    只在估算里用，所以宁可把字数算少一点：中文标点与英文混排时真实宽度更窄，
    算少了只会多降一档，不会把内容顶出舞台。
    """
    return max(1, -(-len(text) // max(4, int(chars_per_line))))


def _flow_stack_height(
    nodes: List[Dict[str, str]], font_u: float, line_h: float, pad_y_u: float
) -> float:
    """链路图整行的高度（单位 ``--u``）：格子横向摊开，行高就是最高那格的高。"""
    wide = max(
        4.0,
        (_CONTENT_W_U - (len(nodes) - 1) * _FLOW_LINK_W_U) / len(nodes)
        - 2 * (_FLOW_PAD_X_U + _BOX_BORDER_U),
    )
    height = 0.0
    for node in nodes:
        lines = _wrapped_lines(node["title"], wide / font_u) * font_u * line_h
        if node["desc"]:
            desc_font = font_u * _FLOW_DESC_RATIO
            lines += _FLOW_LABEL_GAP_U + _wrapped_lines(
                node["desc"], wide / desc_font
            ) * desc_font * line_h
        height = max(height, lines + 2 * (pad_y_u + _BOX_BORDER_U))
    return height


def _flow_tier(nodes: List[Dict[str, str]], budget_u: float) -> str:
    """链路图的字号档位：格子是横向排的，这一行的高就是最高那格的高。"""
    if budget_u <= 0:
        return ""
    for suffix, font_u, line_h, pad_y_u in _FLOW_TIERS:
        if _flow_stack_height(nodes, font_u, line_h, pad_y_u) <= budget_u:
            return suffix
    return _FLOW_TIERS[-1][0]


def _arch_stack_height(
    layers: List[Dict[str, Any]],
    font_u: float,
    line_h: float,
    pad_y_u: float,
    gap_u: float,
) -> float:
    """分层图整块的高度（单位 ``--u``）：逐层估高再叠起来，层间距另算。"""
    track = _CONTENT_W_U - _ARCH_NAME_W_U - _ARCH_COL_GAP_U
    total = gap_u * (len(layers) - 1)
    for layer in layers:
        # 层名和方块抢的是同一行的高度，取高的那个算。
        height = (
            _wrapped_lines(layer["name"], _ARCH_NAME_W_U / font_u) * font_u * line_h
        )
        blocks = layer["blocks"]
        if blocks:
            wide = max(
                4.0,
                (track - (len(blocks) - 1) * _ARCH_BLOCK_GAP_U) / len(blocks)
                - 2 * (_ARCH_PAD_X_U + _BOX_BORDER_U),
            )
            block_lines = max(_wrapped_lines(block, wide / font_u) for block in blocks)
            height = max(
                height,
                block_lines * font_u * line_h + 2 * (pad_y_u + _BOX_BORDER_U),
            )
        total += height
    return total


def _arch_tier(layers: List[Dict[str, Any]], budget_u: float) -> str:
    """分层图的字号档位：逐层估高再叠起来，层间距按那一档的数算。"""
    if budget_u <= 0:
        return ""
    for suffix, font_u, line_h, pad_y_u, gap_u in _ARCH_TIERS:
        if _arch_stack_height(layers, font_u, line_h, pad_y_u, gap_u) <= budget_u:
            return suffix
    return _ARCH_TIERS[-1][0]


def _body_budget_u(page: Dict[str, Any], content_h_u: float) -> float:
    """内容页的正文还能占多少垂直空间（单位 ``--u``，即舞台宽度的 1%）。

    ``content_h_u`` 由 :func:`_content_height_u` 按画幅给出（16:9 是 42.25u，
    4:3 更高，同样的要点在 4:3 下就不必降档）。页头按它的真实行数扣：小标签
    2.4u、标题 4.2u * 1.2 = 5.04u 一行、副标题 2u * 1.5 = 3u 一行，段间 gap
    两处 1.1u，页头下边距 2.6u。标题多换一行，要点就要再降一档 —— 行数只是
    估算，宁可扣得比实际多一点。底部有结论句时再扣掉它自己那一条的高度。
    """
    title = page["title"]
    subtitle = page["subtitle"]
    title_lines = max(1, -(-len(title) // 20))
    head = 2.4 + 1.1 + title_lines * 5.04
    if subtitle:
        head += 1.1 + max(1, -(-len(subtitle) // 42)) * 3.0
    budget = content_h_u - head - 2.6
    if page["takeaway"]:
        budget -= _TAKEAWAY_U
    return budget


def _content_height_u(ratio: str) -> float:
    """舞台内容区的高度（单位 ``--u``）：宽度定死是 100u，高度按画幅换算，
    再减上下各 7u 的内边距。"""
    width, _, height = ratio.partition("/")
    return 100.0 * float(height) / float(width) - 14.0


# 尺寸一律以 --u 为单位，而 --u 是舞台宽度的百分之一：预览面板里 300px 宽和
# 全屏 1920px 宽下版面比例完全一致，不需要两套字号。容器查询单元拿不到时退回
# 按视口宽度计（老浏览器上小预览会偏大，但不会散架）。
_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{--u:1vw;--bg:#fff;--fg:#14181d;--muted:#6b7280;--accent:#ea580c;
--surface:#f7f5f2;--line:rgba(20,24,29,.10);--t2:#2563eb;--t3:#0d9488;--t4:#7c3aed;--ar:16/9}
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
/* 内容页（要点 / 配图 / 表格 / 步骤 / 链路 / 分层）的标题钉在上边带，正文在剩下
   的空间里垂直居中。整块居中会让标题随正文多少上下跳 —— 翻页时每一页的标题位置
   都不一样，看上去就是「没对齐」；短正文还会整页悬在中间，下面空一大块。 */
.slide-bullets,.slide-image,.slide-table,
.slide-steps,.slide-flow,.slide-arch{justify-content:flex-start;gap:0}
/* 上面把 gap 收了，标题与正文的间距改由页头出：gap 会额外压在 auto 外边距
   之上，正文的上下留白就不对称（看着像往上贴）。 */
.slide-bullets .page-head,.slide-image .page-head,.slide-table .page-head,
.slide-steps .page-head,.slide-flow .page-head,.slide-arch .page-head{
margin-bottom:calc(var(--u)*2.6)}
/* 正文包在 .body 里居中，底部结论句才有地方贴着下沿。 */
.slide-bullets .body,.slide-image .body,.slide-table .body,
.slide-steps .body,.slide-flow .body,.slide-arch .body{
margin-top:auto;margin-bottom:auto}
.body{display:flex;flex-direction:column;gap:calc(var(--u)*2)}
/* 页眉那条 hairline 走绝对定位，落在 .slide 的上内边距带里：它不参与 flex，
   也就不改动 _content_height_u 算出来的正文预算。 */
.rail{position:absolute;left:calc(var(--u)*8);right:calc(var(--u)*8);
top:calc(var(--u)*2.6);display:flex;align-items:baseline;justify-content:space-between;
gap:calc(var(--u)*2);padding-bottom:calc(var(--u)*1);border-bottom:1px solid var(--line);
font-size:calc(var(--u)*1.4);letter-spacing:.14em;color:var(--muted)}
.rail::before{content:"";position:absolute;left:0;bottom:-1px;width:calc(var(--u)*7);
height:calc(var(--u)*.28);background:var(--accent)}
.rail-tag{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rail-no{flex-shrink:0;font-variant-numeric:tabular-nums}
.rail-no i{font-style:normal;opacity:.5;margin:0 .35em}
/* 结论句：一句「所以呢」贴在页面底部，左边一段强调色竖条把它和正文分开。
   字号 / 行高 / 内边距 / 与正文的间距这四个数写在 _TAKEAWAY_* 里，两边必须一致。 */
.takeaway{margin:0;padding:calc(var(--u)*0.95) calc(var(--u)*1.6);
border-left:calc(var(--u)*.45) solid var(--accent);border-radius:calc(var(--u)*.9);
background:var(--surface);background:color-mix(in srgb,var(--accent) 9%,var(--bg));
font-size:calc(var(--u)*1.9);line-height:1.35;font-weight:600}
.slide-bullets .takeaway,.slide-image .takeaway,.slide-table .takeaway,
.slide-steps .takeaway,.slide-flow .takeaway,.slide-arch .takeaway{margin-top:calc(var(--u)*2.2)}
@keyframes fade{from{opacity:0;transform:translateY(calc(var(--u)*.8))}to{opacity:1;transform:none}}
.stack{display:flex;flex-direction:column;gap:calc(var(--u)*2.4)}
.chapter{font-variant-numeric:tabular-nums;font-size:calc(var(--u)*1.6);
color:var(--muted);letter-spacing:.16em}
/* 页头的小标签（「核心概念」「第一个操作」这类类别词）：比章节号更该被看见，
   所以用强调色而不是灰色。 */
.kicker{font-size:calc(var(--u)*1.7);font-weight:700;letter-spacing:.1em;
color:var(--accent)}
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
/* 引导词加粗：要点 / 表格 / 结论句里的 **关键词** 是页面的第一层视觉层次。 */
.points li strong,.cmp td strong,.takeaway strong,
.flow .nd strong,.arch .blk strong{color:var(--accent);font-weight:700}
/* 对比表：表头按列循环预设里的第二组色，首列是深色标签列，隔行铺底色。 */
.cmp-wrap{border:1px solid var(--line);border-radius:calc(var(--u)*1);overflow:hidden}
.cmp{width:100%;border-collapse:collapse;table-layout:fixed;font-size:calc(var(--u)*1.9)}
.cmp th,.cmp td{padding:calc(var(--u)*.95) calc(var(--u)*1.5);text-align:left;
vertical-align:top;overflow-wrap:anywhere}
.cmp th{font-size:calc(var(--u)*1.7);font-weight:700;letter-spacing:.06em;
color:var(--muted);border-bottom:1px solid var(--line)}
.cmp th:nth-child(2){color:var(--accent)}
.cmp th:nth-child(3){color:var(--t2)}
.cmp th:nth-child(4){color:var(--t3)}
.cmp tbody tr:nth-child(odd) td{background:var(--surface)}
.cmp td{border-bottom:1px solid var(--line);color:var(--muted);line-height:1.45}
.cmp tbody tr:last-child td{border-bottom:0}
.cmp td:first-child{color:var(--fg);font-weight:600}
/* 步骤条：编号圆点 + 左标题 + 右说明，一行一步。 */
.steps{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;
gap:calc(var(--u)*1.1)}
.steps li{display:flex;align-items:center;gap:calc(var(--u)*1.6);
border:1px solid var(--line);border-radius:calc(var(--u)*.9);background:var(--surface);
padding:calc(var(--u)*1) calc(var(--u)*1.5)}
.steps .no{flex-shrink:0;min-width:calc(var(--u)*3);height:calc(var(--u)*3);
display:inline-flex;align-items:center;justify-content:center;border-radius:999px;
border:1px solid var(--accent);color:var(--accent);font-size:calc(var(--u)*1.5);
font-variant-numeric:tabular-nums}
.steps .st{font-size:calc(var(--u)*2.1);font-weight:700}
.steps .sd{margin-left:auto;color:var(--muted);font-size:calc(var(--u)*1.8);
text-align:right}
/* 链路图：等宽的方框横向串起来，箭头画在框间那条固定宽度的留白里，条件小字挂在
   箭头下面。格子宽度由 flex 分出来，和 _flow_tier 里 (84u - 箭头×n-1) / n 同一个
   算式，两边对不上就会估错行数 —— 见 test_flow_tier_numbers_match_the_css。 */
.flow{display:flex;align-items:stretch}
.flow .node{flex:1 1 0;min-width:0;display:flex;flex-direction:column;
justify-content:center;align-items:center;gap:calc(var(--u)*0.55);text-align:center;
padding:calc(var(--u)*1.0) calc(var(--u)*0.9);border:1px solid var(--line);
border-radius:calc(var(--u)*1);background:var(--surface)}
.flow .nt{font-size:calc(var(--u)*1.9);line-height:1.3;font-weight:700;
overflow-wrap:anywhere}
.flow .nd{font-size:calc(var(--u)*1.52);line-height:1.3;color:var(--muted);
overflow-wrap:anywhere}
/* 最后一格是链路要去的地方，用强调色收口。 */
.flow .node:last-child{border-color:color-mix(in srgb,var(--accent) 45%,var(--line));
background:color-mix(in srgb,var(--accent) 7%,var(--bg))}
.flow .node:last-child .nt{color:var(--accent)}
.flow .link{flex:0 0 calc(var(--u)*6.5);display:flex;flex-direction:column;
align-items:center;justify-content:center;gap:calc(var(--u)*0.3);line-height:1;
color:var(--accent);font-size:calc(var(--u)*2.2)}
.flow .link em{font-style:normal;line-height:1.25;color:var(--muted);text-align:center;
font-size:calc(var(--u)*1.0);letter-spacing:0;overflow-wrap:anywhere}
/* 分层图：左列层名（右对齐，贴着方块），右侧该层的模块方块等宽铺开。 */
.arch{display:flex;flex-direction:column;gap:calc(var(--u)*1.9)}
.arch .layer{display:grid;grid-template-columns:calc(var(--u)*15.0) 1fr;
column-gap:calc(var(--u)*2.0);align-items:stretch}
.arch .lname{display:flex;align-items:center;justify-content:flex-end;text-align:right;
font-size:calc(var(--u)*1.8);line-height:1.3;font-weight:700;color:var(--accent);
overflow-wrap:anywhere}
.arch .lblocks{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;
grid-column-gap:calc(var(--u)*1.0)}
.arch .blk{display:flex;align-items:center;justify-content:center;text-align:center;
padding:calc(var(--u)*0.85) calc(var(--u)*0.7);border:1px solid var(--line);
border-radius:calc(var(--u)*.8);background:var(--surface);font-size:calc(var(--u)*1.8);
line-height:1.3;overflow-wrap:anywhere}
.arch .layer:first-child .blk{background:color-mix(in srgb,var(--accent) 7%,var(--bg));
border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
/* 装不下时的两档降级，数字与 _FLOW_TIERS / _ARCH_TIERS 一一对应。链路和分层图的字
   本来就小，再往下缩有极限，所以到 denser 就停：真塞不下是模型该拆页。 */
.flow.dense .node{padding:calc(var(--u)*0.85) calc(var(--u)*0.9)}
.flow.dense .nt{font-size:calc(var(--u)*1.65);line-height:1.3}
.flow.dense .nd{font-size:calc(var(--u)*1.32);line-height:1.3}
.flow.denser .node{padding:calc(var(--u)*0.7) calc(var(--u)*0.9)}
.flow.denser .nt{font-size:calc(var(--u)*1.45);line-height:1.3}
.flow.denser .nd{font-size:calc(var(--u)*1.16);line-height:1.3}
.arch.dense{gap:calc(var(--u)*1.5)}
.arch.dense .lname,.arch.dense .blk{font-size:calc(var(--u)*1.6);line-height:1.3}
.arch.dense .blk{padding:calc(var(--u)*0.7) calc(var(--u)*0.7)}
.arch.denser{gap:calc(var(--u)*1.0)}
.arch.denser .lname,.arch.denser .blk{font-size:calc(var(--u)*1.3);line-height:1.3}
.arch.denser .blk{padding:calc(var(--u)*0.5) calc(var(--u)*0.7)}
.slide-cover,.slide-closing,.slide-section{align-items:flex-start;
background:radial-gradient(120% 140% at 8% 0%,var(--surface) 0%,var(--bg) 60%)}
.cover-inner,.closing-inner{gap:calc(var(--u)*3)}
.cover-title{margin:0;font-size:calc(var(--u)*7.4);line-height:1.1;font-weight:800;
letter-spacing:-.02em;max-width:24em}
.cover-title::after{content:"";display:block;width:calc(var(--u)*9);
height:calc(var(--u)*.6);margin-top:calc(var(--u)*2.4);background:var(--accent);
border-radius:999px}
.cover-sub,.closing-sub{margin:0;color:var(--muted);font-size:calc(var(--u)*2.6);
line-height:1.5;max-width:30em;white-space:pre-line}
.section-inner{gap:calc(var(--u)*2)}
.section-title{margin:0;font-size:calc(var(--u)*6);line-height:1.15;font-weight:800;max-width:22em}
.section-sub{margin:0;color:var(--muted);font-size:calc(var(--u)*2.4);max-width:30em}
.split{display:grid;grid-template-columns:1fr calc(var(--u)*42);gap:calc(var(--u)*5);
align-items:center}
.shot{margin:0;display:flex;align-items:center;justify-content:center;
background:var(--surface);border:1px solid var(--line);border-radius:calc(var(--u)*1.2);
padding:calc(var(--u)*1.6);max-height:var(--body-h,calc(var(--u)*30))}
.shot img{max-width:100%;max-height:calc(var(--body-h,calc(var(--u)*30)) - var(--u)*4);
display:block;border-radius:calc(var(--u)*.6)}
.quote-inner{gap:calc(var(--u)*3)}
.quote{margin:0;padding-left:calc(var(--u)*3);border-left:calc(var(--u)*.8) solid var(--accent);
font-size:calc(var(--u)*4.6);line-height:1.3;font-weight:700;max-width:26em}
.quote-by{margin:0;color:var(--muted);font-size:calc(var(--u)*2.2)}
.quote-points li{font-size:calc(var(--u)*2.2);color:var(--muted)}
/* 要点超出预算时的两档降级，与 _BULLET_TIERS 一一对应：少一行比字小一点更难
   接受，所以宁肯降字号也要让最后一条留在舞台上。 */
.points.dense{gap:calc(var(--u)*1.15)}
.points.dense li{font-size:calc(var(--u)*1.95);line-height:1.4}
.points.dense li::before{top:calc(var(--u)*.75);width:calc(var(--u)*.8);height:calc(var(--u)*.8)}
.points.denser{gap:calc(var(--u)*0.9)}
.points.denser li{font-size:calc(var(--u)*1.6);line-height:1.35}
.points.denser li::before{top:calc(var(--u)*.6);width:calc(var(--u)*.65);height:calc(var(--u)*.65)}
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
