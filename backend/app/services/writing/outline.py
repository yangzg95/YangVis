"""大纲树的清洗、cid 发放与参数白名单。

大纲是 ``writing_work.outline`` 那个 JSON 列的唯一事实源，前端与（M2 起的）模型
都会提交整棵树进来。所有输入先过 :func:`sanitize_outline`：

- 层级按作品 ``kind`` 收窄：小说允许 卷→章 / 顶层章，文章只允许扁平段落，模型给的
  多余层级不会进库；
- ``cid`` 与幻灯片的 sid 同形（``p`` + 16 位 hex），非法或重名一律重发，数组下标
  永不参与寻址；
- 节点总数有硬上限，超了直接抛错而不是悄悄截断——截掉的是用户的章节。
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings

settings = get_settings()

KIND_NOVEL = "novel"
KIND_ARTICLE = "article"
KINDS = (KIND_NOVEL, KIND_ARTICLE)

NODE_VOLUME = "volume"
NODE_CHAPTER = "chapter"
NODE_SECTION = "section"

# 各作品 kind 允许的顶层节点；卷的子节点只允许章。
TOP_LEVEL_KINDS: Dict[str, frozenset[str]] = {
    KIND_NOVEL: frozenset({NODE_VOLUME, NODE_CHAPTER}),
    KIND_ARTICLE: frozenset({NODE_SECTION}),
}
# 叶子（有正文块的节点）的 kind。
LEAF_KIND: Dict[str, str] = {KIND_NOVEL: NODE_CHAPTER, KIND_ARTICLE: NODE_SECTION}

# 每种 kind 允许出现在 params 里的键；未知键入库前丢弃，避免前端/模型塞任意结构。
PARAM_KEYS: Dict[str, frozenset[str]] = {
    KIND_NOVEL: frozenset(
        {"genre", "style", "tone", "viewpoint", "lead", "worldview", "word_target"}
    ),
    KIND_ARTICLE: frozenset(
        {"article_type", "platform", "audience", "tone", "viewpoint", "core_points",
         "word_target"}
    ),
}
_PARAM_TEXT_MAX = 512

_CID_RE = re.compile(r"^p[0-9a-f]{16}$")
_TITLE_MAX = 128
_SYNOPSIS_MAX = 2000

_DEFAULT_TITLES = {
    NODE_VOLUME: "未命名卷",
    NODE_CHAPTER: "未命名章节",
    NODE_SECTION: "未命名段落",
}


def new_cid() -> str:
    """发一个新的节点/正文块 cid，与 slides sid 同形。"""
    return f"p{uuid.uuid4().hex[:16]}"


def sanitize_params(kind: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """按 kind 白名单收敛创作参数：未知键丢弃，文本截断，word_target 收正整数。"""
    if not isinstance(params, dict):
        return {}
    allowed = PARAM_KEYS.get(kind, frozenset())
    cleaned: Dict[str, Any] = {}
    for key in allowed:
        if key not in params or params[key] is None:
            continue
        if key == "word_target":
            try:
                value = int(params[key])
            except (TypeError, ValueError):
                continue
            if 0 < value <= settings.WRITING_PART_TARGET_MAX_WORDS:
                cleaned[key] = value
            continue
        value = params[key]
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            cleaned[key] = value[:_PARAM_TEXT_MAX]
    return cleaned


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _clean_target_words(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        target = int(value)
    except (TypeError, ValueError):
        return None
    if target <= 0:
        return None
    return min(target, settings.WRITING_PART_TARGET_MAX_WORDS)


def _take_cid(raw: Any, used: set[str]) -> str:
    """合法且未重名的 cid 原样保留，否则发新的。"""
    if isinstance(raw, str) and _CID_RE.match(raw) and raw not in used:
        used.add(raw)
        return raw
    cid = new_cid()
    used.add(cid)
    return cid


def sanitize_outline(kind: str, nodes: List[Any]) -> List[Dict[str, Any]]:
    """把外部提交的大纲收敛成合法树。

    小说：顶层收窄成卷/章，卷的 children 收窄成章且只保留一层；章不允许再嵌套。
    文章：所有顶层节点收窄成段落，children 整体丢弃。
    节点总数超过 ``WRITING_OUTLINE_MAX_NODES`` 抛 ValueError——由路由翻译成 envelope 错误。
    """
    if kind not in KINDS:
        raise ValueError("未知的作品类型")
    raw_nodes = nodes if isinstance(nodes, list) else []
    used: set[str] = set()
    total = 0
    limit = settings.WRITING_OUTLINE_MAX_NODES

    def make_leaf(raw: Any, node_kind: str) -> Dict[str, Any]:
        nonlocal total
        total += 1
        if total > limit:
            raise ValueError(f"大纲节点超过上限 {limit}，请先删减再保存")
        raw = raw if isinstance(raw, dict) else {}
        title = _clean_text(raw.get("title"), _TITLE_MAX) or _DEFAULT_TITLES[node_kind]
        return {
            "cid": _take_cid(raw.get("cid"), used),
            "kind": node_kind,
            "title": title,
            "synopsis": _clean_text(raw.get("synopsis"), _SYNOPSIS_MAX),
            "target_words": _clean_target_words(raw.get("target_words")),
        }

    result: List[Dict[str, Any]] = []
    leaf_kind = LEAF_KIND[kind]
    allowed_top = TOP_LEVEL_KINDS[kind]

    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        raw_kind = raw.get("kind")
        if kind == KIND_ARTICLE:
            # 文章只有一层段落：任何输入都归一成 section，嵌套直接丢。
            result.append(make_leaf(raw, NODE_SECTION))
            continue

        if raw_kind == NODE_VOLUME:
            volume = make_leaf(raw, NODE_VOLUME)
            children: List[Dict[str, Any]] = []
            for child in raw.get("children") or []:
                # 卷下只收章；模型若再嵌一层，孙节点被忽略。
                children.append(make_leaf(child, NODE_CHAPTER))
            volume["children"] = children
            result.append(volume)
        else:
            # 顶层章（显式标 chapter 或没标 kind 的都按章收）；章上的 children 丢弃。
            if raw_kind not in allowed_top:
                # 不认识的 kind 不静默改名成卷，按章处理最贴近用户在写小说的意图。
                pass
            result.append(make_leaf(raw, NODE_CHAPTER))

    return result


def flatten_leaves(tree: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按阅读顺序展平叶子节点（小说的章、文章的段落），卷节点跳过。"""
    leaves: List[Dict[str, Any]] = []
    for node in tree:
        kind = node.get("kind")
        if kind == NODE_VOLUME:
            leaves.extend(child for child in node.get("children", []) if isinstance(child, dict))
        else:
            leaves.append(node)
    return leaves


def find_leaf(tree: List[Dict[str, Any]], cid: str) -> Optional[Dict[str, Any]]:
    """按 cid 找叶子节点，找不到返回 None。"""
    for leaf in flatten_leaves(tree):
        if leaf.get("cid") == cid:
            return leaf
    return None


def remove_leaf(tree: List[Dict[str, Any]], cid: str) -> bool:
    """从树里摘掉一个叶子节点（卷内章或顶层叶子）。返回是否真的摘到了。"""
    for node in tree:
        if node.get("kind") == NODE_VOLUME:
            children = node.get("children") or []
            for index, child in enumerate(children):
                if child.get("cid") == cid:
                    children.pop(index)
                    return True
        elif node.get("cid") == cid:
            tree.remove(node)
            return True
    return False


def insert_leaf(
    tree: List[Dict[str, Any]], leaf: Dict[str, Any], after_cid: Optional[str]
) -> None:
    """把新叶子插进树里。

    小说：给了 after_cid 就插在它后面（同层）；没给时末尾卷存在就进末尾卷，否则
    作为顶层章追加。文章：顶层段落，after_cid 控制插入位置。
    """
    kind = leaf["kind"]
    if kind == NODE_SECTION:
        _insert_after_top(tree, leaf, after_cid)
        return

    if after_cid:
        # 先找卷内位置，再找顶层位置。
        for node in tree:
            if node.get("kind") == NODE_VOLUME:
                children = node.setdefault("children", [])
                for index, child in enumerate(children):
                    if child.get("cid") == after_cid:
                        children.insert(index + 1, leaf)
                        return
        _insert_after_top(tree, leaf, after_cid)
        return

    if tree and tree[-1].get("kind") == NODE_VOLUME:
        tree[-1].setdefault("children", []).append(leaf)
    else:
        tree.append(leaf)


def _insert_after_top(tree: List[Dict[str, Any]], leaf: Dict[str, Any], after_cid: Optional[str]) -> None:
    if not after_cid:
        tree.append(leaf)
        return
    for index, node in enumerate(tree):
        if node.get("cid") == after_cid:
            tree.insert(index + 1, leaf)
            return
    # 锚点已不存在（可能被别的操作删了）：退化成追加，不报错挡用户。
    tree.append(leaf)


def sync_leaf_title(tree: List[Dict[str, Any]], cid: str, title: str) -> bool:
    """正文块改名时同步大纲叶子的标题，维持「大纲即目录」。"""
    leaf = find_leaf(tree, cid)
    if leaf is None:
        return False
    leaf["title"] = title
    return True


def leaf_titles(tree: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """展平后的 ``(cid, title)`` 清单，导出与上下文拼装用。"""
    return [(str(leaf.get("cid", "")), str(leaf.get("title", ""))) for leaf in flatten_leaves(tree)]
