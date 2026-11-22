"""创作模块 AI 生成的后台任务配料：工作流输入拼装与模型产物解析。

与幻灯片那条链路同构：路由/服务只负责建运行（``start_builtin``），真正的模型
调用在后台任务里跑内置工作流，模型返回的原始文本在这里解析成大纲节点，再交给
:mod:`app.services.writing.outline` 的白名单清洗与 service 的对账逻辑落库。

解析器只做「从模型文本里取出结构」这一件事，cid 发放、层级收窄、节点上限、
part 行对账全部复用手工保存那条路径——模型产出与用户在树里手改的大纲过的是
同一道闸门。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.models.entities import WritingPart, WritingWork
from app.services.agent_run import json_span, labeled_section, strip_json_fence
from app.services.writing.outline import (
    KIND_ARTICLE,
    KIND_NOVEL,
    NODE_CHAPTER,
    NODE_SECTION,
    NODE_VOLUME,
    flatten_leaves,
)

logger = logging.getLogger("yangvis.writing")

settings = get_settings()

# 与 builtin.py 里 seed 的 slug 对齐；service 按作品 kind 二选一。
WORKFLOW_NOVEL_OUTLINE = "novel-outline"
WORKFLOW_ARTICLE_OUTLINE = "article-outline"

# 喂模型的参数标签：白名单键在 outline.PARAM_KEYS，这里只负责把它们说成人话。
NOVEL_PARAM_LABELS: Dict[str, str] = {
    "genre": "题材",
    "style": "风格",
    "tone": "基调",
    "viewpoint": "视角",
    "lead": "主角",
    "worldview": "世界观",
    "word_target": "目标总字数",
}
ARTICLE_PARAM_LABELS: Dict[str, str] = {
    "article_type": "文章类型",
    "platform": "发布平台",
    "audience": "目标受众",
    "tone": "语气",
    "viewpoint": "视角",
    "core_points": "核心观点",
    "word_target": "目标总字数",
}

# 标题候选只在文章流程产出，meta 里最多留这么多条，每条多长也卡住。
MAX_TITLE_CANDIDATES = 5
MAX_TITLE_CANDIDATE_CHARS = 128


def _truncate(text: str) -> str:
    """上下文窗口闸门：brief 最长 2 万字，喂模型前按 WRITING_CONTEXT_MAX_CHARS 截。"""
    limit = settings.WRITING_CONTEXT_MAX_CHARS
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n……（内容过长，已截断）"


def _params_block(work: WritingWork) -> str:
    """把落库的白名单参数渲染成「标签：值」清单；没填就是空串，模板里整段不出现。"""
    labels = NOVEL_PARAM_LABELS if work.kind == KIND_NOVEL else ARTICLE_PARAM_LABELS
    lines = [
        f"- {labels.get(key, key)}：{value}"
        for key, value in (work.params or {}).items()
        if value not in (None, "")
    ]
    if not lines:
        return ""
    return "创作参数：\n" + "\n".join(lines) + "\n\n"


def _brief_block(work: WritingWork) -> str:
    """灵感/主题段落：作品间共用的条件块，没填就是空串。"""
    brief_label = "灵感 / 梗概" if work.kind == KIND_NOVEL else "主题 / 素材"
    brief = (work.brief or "").strip()
    return f"{labeled_section(brief_label, _truncate(brief))}\n\n" if brief else ""


def _requirement_note(work: WritingWork) -> str:
    requirement = (work.requirement or "").strip()
    return f"补充要求：{requirement}\n\n" if requirement else ""


def outline_inputs(work: WritingWork) -> Dict[str, str]:
    """把一份作品整理成大纲工作流声明的输入变量（条件段落整块传，模板只做替换）。"""
    return {
        "title": work.title,
        "brief_block": _brief_block(work),
        "params_block": _params_block(work),
        "requirement_note": _requirement_note(work),
        "max_nodes": str(settings.WRITING_OUTLINE_MAX_NODES),
    }


def workflow_slug_for(kind: str) -> str:
    if kind == KIND_ARTICLE:
        return WORKFLOW_ARTICLE_OUTLINE
    return WORKFLOW_NOVEL_OUTLINE


# ---- 模型输出解析 -------------------------------------------------------------


def _json_candidate(text: str) -> Optional[str]:
    """从模型输出里截出最外层 JSON 段（容忍 ```json 栅栏与前后寒暄，同 slides）。

    对象和数组两种跨度都可能存在：裸数组 ``[{...}]`` 里对象跨度是数组的内部，
    先到先得会把内层对象误当最外层契约，所以谁的开括号靠前就用谁。
    """
    stripped = strip_json_fence(text)
    candidates = [
        span
        for span in (
            json_span(stripped, "{", "}"),
            json_span(stripped, "[", "]"),
        )
        if span
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda span: stripped.find(span[0]))


def _load_json(raw_text: str) -> Any:
    candidate = _json_candidate(raw_text)
    if candidate is None:
        raise ValueError("模型没有返回可解析的大纲结构")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型返回的 JSON 无法解析：{exc}") from exc


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _target_words(value: Any) -> Optional[int]:
    try:
        target = int(value)
    except (TypeError, ValueError):
        return None
    return target if target > 0 else None


def _extract_outline_rows(data: Any) -> List[Any]:
    """契约的主形状是 ``{"outline": [...]}``，容忍 ``volumes``/``sections`` 与裸数组。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("outline", "volumes", "sections", "chapters"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def parse_novel_outline(raw_text: str) -> List[Dict[str, Any]]:
    """小说模型文本 → 可喂给 ``sanitize_outline`` 的节点（不带 cid，服务端发放）。

    带非空 ``chapters`` 的对象收成卷，其余按顶层章收；字段缺失不在这里报错，
    清洗器会回落默认标题，最后一个叶子都没有时由调用方判失败。
    """
    data = _load_json(raw_text)
    nodes: List[Dict[str, Any]] = []
    for row in _extract_outline_rows(data):
        if not isinstance(row, dict):
            continue
        chapters = _as_list(row.get("chapters") or row.get("children"))
        if chapters:
            children = [
                {
                    "kind": NODE_CHAPTER,
                    "title": _text(chapter.get("title")),
                    "synopsis": _text(chapter.get("synopsis")),
                    "target_words": _target_words(chapter.get("target_words")),
                }
                for chapter in chapters
                if isinstance(chapter, dict)
            ]
            nodes.append(
                {"kind": NODE_VOLUME, "title": _text(row.get("title")), "children": children}
            )
        else:
            nodes.append(
                {
                    "kind": NODE_CHAPTER,
                    "title": _text(row.get("title")),
                    "synopsis": _text(row.get("synopsis")),
                    "target_words": _target_words(row.get("target_words")),
                }
            )
    return nodes


def parse_article_outline(raw_text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """文章模型文本 → ``(段落节点, 标题候选)``；point 复用节点的 synopsis 字段承载。"""
    data = _load_json(raw_text)
    sections = [
        {
            "kind": NODE_SECTION,
            "title": _text(row.get("title")),
            "synopsis": _text(row.get("point") or row.get("synopsis")),
            "target_words": _target_words(row.get("target_words")),
        }
        for row in _extract_outline_rows(data)
        if isinstance(row, dict)
    ]

    candidates: List[str] = []
    if isinstance(data, dict):
        for item in _as_list(data.get("title_candidates")):
            title = _text(item)
            if title and title not in candidates:
                candidates.append(title[:MAX_TITLE_CANDIDATE_CHARS])
            if len(candidates) >= MAX_TITLE_CANDIDATES:
                break
    return sections, candidates


# ---- M3：正文生成的动作、输入拼装与整篇切分 -------------------------------------

WORKFLOW_NOVEL_CHAPTER = "novel-chapter"
WORKFLOW_ARTICLE_DRAFT = "article-draft"
WORKFLOW_POLISH = "writing-polish"

ACTION_DRAFT = "draft"
ACTION_CONTINUE = "continue"
ACTION_REWRITE = "rewrite"
ACTION_POLISH = "polish"
ACTION_EXPAND = "expand"
ACTION_CONDENSE = "condense"
ACTION_RETONE = "retone"

# 小说支持全部七个动作；文章段落只走编辑类动作，整篇起草是另一条 draft 流程。
NOVEL_ACTIONS: Tuple[str, ...] = (
    ACTION_DRAFT,
    ACTION_CONTINUE,
    ACTION_REWRITE,
    ACTION_POLISH,
    ACTION_EXPAND,
    ACTION_CONDENSE,
    ACTION_RETONE,
)
ARTICLE_ACTIONS: Tuple[str, ...] = (
    ACTION_REWRITE,
    ACTION_POLISH,
    ACTION_EXPAND,
    ACTION_CONDENSE,
    ACTION_RETONE,
)
EDITOR_ACTIONS = ARTICLE_ACTIONS
WRITER_ACTIONS: Tuple[str, ...] = (ACTION_DRAFT, ACTION_CONTINUE)

# 前情只带最近一章的结尾（设计文档 4.4 的 1500 字闸门）。
HISTORY_TAIL_CHARS = 1500

_ACTION_NOTES: Dict[str, str] = {
    ACTION_DRAFT: "任务：首次起草这一章，按本章要求从零写出完整正文。",
    ACTION_CONTINUE: (
        "任务：顺着「本章已有正文」往下续写，不要回抄或重复已有内容，"
        "直接输出续写的新增部分。"
    ),
    ACTION_REWRITE: "任务：重写下面的正文，保留主线与关键信息，改善结构、节奏与表达。",
    ACTION_POLISH: "任务：润色下面的正文，不改变立意，只优化措辞、语序与可读性。",
    ACTION_EXPAND: "任务：扩写下面的正文，在不灌水、不编造事实的前提下补充细节与论证。",
    ACTION_CONDENSE: "任务：缩写下面的正文，压缩冗余，保留全部关键信息与论证链条。",
}


def actions_for_kind(kind: str) -> Tuple[str, ...]:
    return NOVEL_ACTIONS if kind == KIND_NOVEL else ARTICLE_ACTIONS


def workflow_slug_for_action(action: str) -> str:
    """起草/续写走小说写手流程；五个编辑动作两个 kind 共用文字编辑流程。"""
    if action in WRITER_ACTIONS:
        return WORKFLOW_NOVEL_CHAPTER
    if action in EDITOR_ACTIONS:
        return WORKFLOW_POLISH
    raise ValueError(f"未知的生成动作：{action}")


def action_note(
    action: str, *, tone: Optional[str] = None, instruction: Optional[str] = None
) -> str:
    """把动作说成人话指令；换语气的目标语气必填，用户补充要求追加在后面。"""
    if action == ACTION_RETONE:
        base = f"任务：把正文的语气改成「{(tone or '').strip()}」，观点与事实保持不变。"
    else:
        base = _ACTION_NOTES[action]
    extra = instruction.strip() if isinstance(instruction, str) else ""
    if extra:
        base = f"{base}\n用户的额外要求：\n{extra[:512]}"
    return base + "\n\n"


def _leaf_line(leaf: Dict[str, Any], current_cid: Optional[str]) -> str:
    head = "【本章】" if leaf.get("cid") == current_cid else ""
    bits = [f"{head}{leaf.get('title', '')}"]
    synopsis = _text(leaf.get("synopsis"))
    if synopsis:
        bits.append(synopsis)
    target = leaf.get("target_words")
    if target:
        bits.append(f"目标 {target} 字")
    return "- " + " — ".join(bits)


def _outline_block(work: WritingWork, current_cid: Optional[str] = None) -> str:
    """完整大纲只含摘要（很短）；小说保留卷层级，当前章打标记方便写手定位。"""
    tree = work.outline or []
    if not tree:
        return ""
    lines: List[str] = []
    for node in tree:
        if node.get("kind") == NODE_VOLUME:
            lines.append(f"卷：{node.get('title', '')}")
            for child in node.get("children") or []:
                lines.append("  " + _leaf_line(child, current_cid))
        else:
            lines.append(_leaf_line(node, current_cid))
    return labeled_section("完整大纲（只含章节/段落摘要）", "\n".join(lines)) + "\n\n"


def _history_block(
    work: WritingWork, parts: List[WritingPart], current_cid: str
) -> str:
    """前情：更早章节只给标题+摘要清单，最近有正文的一章附 1500 字结尾。"""
    leaves = flatten_leaves(work.outline or [])
    cids = [str(leaf["cid"]) for leaf in leaves]
    if current_cid not in cids or cids.index(current_cid) == 0:
        return ""
    earlier = leaves[: cids.index(current_cid)]
    summary_lines = [
        "- " + " — ".join(
            bit for bit in (str(leaf.get("title", "")), _text(leaf.get("synopsis"))) if bit
        )
        for leaf in earlier
    ]
    blocks = ["前情脉络：\n" + "\n".join(summary_lines)]

    content_map = {part.cid: part.content or "" for part in parts}
    for leaf in reversed(earlier):
        tail_text = content_map.get(str(leaf.get("cid")), "").strip()
        if tail_text:
            blocks.append(
                labeled_section(
                    f"前一章《{leaf.get('title', '')}》正文结尾",
                    tail_text[-HISTORY_TAIL_CHARS:],
                )
            )
            break
    return "\n\n".join(blocks) + "\n\n"


def _chapter_block(leaf: Dict[str, Any]) -> str:
    lines = [f"章节名：{leaf.get('title', '')}"]
    synopsis = _text(leaf.get("synopsis"))
    if synopsis:
        lines.append(f"章节摘要：{synopsis}")
    lines.append(f"目标字数：{leaf.get('target_words') or '随情节需要'}")
    return labeled_section("本章要求", "\n".join(lines)) + "\n\n"


def novel_chapter_inputs(
    work: WritingWork,
    parts: List[WritingPart],
    leaf: Dict[str, Any],
    part: WritingPart,
    action: str,
    instruction: Optional[str],
) -> Dict[str, str]:
    """小说起草/续写（novel-chapter）的输入变量；续写才带本章已有正文。"""
    cid = str(leaf["cid"])
    existing = ""
    if action == ACTION_CONTINUE and (part.content or "").strip():
        existing = labeled_section("本章已有正文", part.content.strip()) + "\n\n"
    return {
        "title": work.title,
        "work_card_block": _params_block(work),
        "outline_block": _outline_block(work, cid),
        "history_block": _history_block(work, parts, cid),
        "chapter_block": _chapter_block(leaf),
        "existing_block": existing,
        "instruction_note": action_note(action, instruction=instruction),
    }


def polish_inputs(
    work: WritingWork,
    leaf: Dict[str, Any],
    part: WritingPart,
    action: str,
    instruction: Optional[str],
    tone: Optional[str],
) -> Dict[str, str]:
    """五个编辑动作共用的 writing-polish 输入（小说与文章同形）。"""
    context_label = "章节摘要" if work.kind == KIND_NOVEL else "段落观点"
    context_text = f"标题：{leaf.get('title', '')}"
    synopsis = _text(leaf.get("synopsis"))
    if synopsis:
        context_text += f"\n{context_label}：{synopsis}"
    return {
        "context_block": labeled_section(context_label, context_text) + "\n\n",
        "content_block": labeled_section("待处理正文", part.content.strip()) + "\n\n",
        "instruction_note": action_note(action, tone=tone, instruction=instruction),
    }


def article_draft_inputs(work: WritingWork) -> Dict[str, str]:
    """文章整篇起草（article-draft）：大纲带段落观点与目标字数。"""
    lines = []
    for index, leaf in enumerate(flatten_leaves(work.outline or []), start=1):
        bits = [f"{index}. {leaf.get('title', '')}"]
        synopsis = _text(leaf.get("synopsis"))
        if synopsis:
            bits.append(synopsis)
        if leaf.get("target_words"):
            bits.append(f"目标 {leaf['target_words']} 字")
        lines.append(" — ".join(bits))
    return {
        "title": work.title,
        "brief_block": _brief_block(work),
        "params_block": _params_block(work),
        "requirement_note": _requirement_note(work),
        "outline_block": labeled_section("段落大纲", "\n".join(lines)) + "\n\n",
    }


_DRAFT_SPLIT_RE = re.compile(r"(?m)^##[ \t]+.*(?:\r?\n|$)")

# markdown 成稿常被 ```md / ```markdown 栅栏整段裹住；共享的 strip_json_fence
# 只认裸栅栏与 json，这里按 markdown 域的实际输出放宽语言标识。
_MD_FENCE = re.compile(r"^```[^\s`]*\s*\n(.*?)\n?```$", re.DOTALL)


def _strip_markdown_fence(raw_text: str) -> str:
    text = (raw_text or "").strip()
    fence = _MD_FENCE.match(text)
    return fence.group(1).strip() if fence else text


def split_article_draft(
    raw_text: str, expected_count: int
) -> Tuple[List[str], Optional[str]]:
    """整篇成稿按二级标题切回各段落。

    切出的块数与大纲叶子数对得上就 1:1 返回；对不上（模型漏标题/合并段落）不静默
    丢内容——整篇落第一个段落并附 warning，由用户在工作台手工拆。
    """
    text = _strip_markdown_fence(raw_text)
    chunks = [chunk.strip() for chunk in _DRAFT_SPLIT_RE.split(text) if chunk.strip()]
    if expected_count > 0 and len(chunks) == expected_count:
        return chunks, None
    warning = (
        f"模型返回了 {len(chunks)} 个二级标题段落，与大纲的 {expected_count} 个段落"
        "对不上，整篇成稿已放入第一个段落，请手工拆分调整。"
    )
    return [text], warning
