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
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.models.entities import WritingWork
from app.services.agent_run import json_span, labeled_section, strip_json_fence
from app.services.writing.outline import (
    KIND_ARTICLE,
    KIND_NOVEL,
    NODE_CHAPTER,
    NODE_SECTION,
    NODE_VOLUME,
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


def outline_inputs(work: WritingWork) -> Dict[str, str]:
    """把一份作品整理成大纲工作流声明的输入变量（条件段落整块传，模板只做替换）。"""
    brief_label = "灵感 / 梗概" if work.kind == KIND_NOVEL else "主题 / 素材"
    brief = (work.brief or "").strip()
    return {
        "title": work.title,
        "brief_block": (
            f"{labeled_section(brief_label, _truncate(brief))}\n\n" if brief else ""
        ),
        "params_block": _params_block(work),
        "requirement_note": (
            f"补充要求：{work.requirement.strip()}\n\n" if work.requirement else ""
        ),
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
