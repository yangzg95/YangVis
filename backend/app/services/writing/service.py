"""创作模块的存取：作品、大纲与正文块的 CRUD、对账和导出。

数据访问按单个用户隔离（与 :class:`NoteService` 同一约定）：所有作品查询都经过
:meth:`WritingService._scope`，正文块先过作品归属这道门，他人的 id 一律读作 404。

大纲（``writing_work.outline`` 的 JSON 树）是目录的唯一事实源，正文块行与大纲叶子
按 cid 1:1 对账：保存大纲时新叶子建空块、被删的叶子连带删块、顺序变化改
``sort_order``。标题在两边同步——大纲改名与正文块改名走的是同一棵树。

M1 没有 AI 生成，所以这里还没有运行态；M2 起状态由认领作品/正文块的
``workflow_run`` 翻译，本表仍只存产物（同 slide_deck 的取舍）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import WritingPart, WritingWork
from app.models.schemas import (
    OutlineNode,
    WritingPartOut,
    WritingWorkCreate,
    WritingWorkDetail,
    WritingWorkItem,
    WritingWorkUpdate,
)
from app.services.writing.outline import (
    KIND_NOVEL,
    KINDS,
    LEAF_KIND,
    NODE_VOLUME,
    flatten_leaves,
    insert_leaf,
    new_cid,
    remove_leaf,
    sanitize_outline,
    sanitize_params,
    sync_leaf_title,
)
from app.services.writing.words import count_words

logger = logging.getLogger("yangvis.writing")

settings = get_settings()

# LIKE 通配符转义（同笔记服务）：用户搜的 % / _ 是普通字符。
_LIKE_ESCAPE = str.maketrans({"/": "//", "%": "/%", "_": "/_"})
_WS_RE = re.compile(r"\s+")


def _like_term(keyword: str) -> str:
    return f"%{keyword.translate(_LIKE_ESCAPE)}%"


class WritingService:
    """针对单个用户的创作作品做增删改查、大纲对账与导出。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(WritingWork.owner_id == self._owner_id)

    # -- 作品查询 -----------------------------------------------------------

    def list_works(
        self, kind: Optional[str] = None, keyword: Optional[str] = None
    ) -> List[WritingWork]:
        stmt = self._scope(select(WritingWork))
        if kind in KINDS:
            stmt = stmt.where(WritingWork.kind == kind)
        kw = (keyword or "").strip()
        if kw:
            term = _like_term(kw)
            # brief 是 LONGTEXT，ilike 在 MySQL 上编译成 LOWER(col) LIKE，照样能走。
            stmt = stmt.where(
                or_(
                    WritingWork.title.ilike(term, escape="/"),
                    WritingWork.brief.ilike(term, escape="/"),
                )
            )
        stmt = stmt.order_by(WritingWork.updated_at.desc(), WritingWork.id.desc())
        return list(self._db.scalars(stmt).all())

    def get_work(self, work_id: int) -> WritingWork:
        work = self._db.scalar(
            self._scope(select(WritingWork).where(WritingWork.id == work_id))
        )
        if work is None:
            raise LookupError("作品不存在")
        return work

    # -- 作品变更 -----------------------------------------------------------

    def create_work(self, payload: WritingWorkCreate) -> WritingWork:
        if payload.kind not in KINDS:
            raise ValueError("未知的作品类型")
        existing = self._db.scalar(
            self._scope(select(func.count(WritingWork.id))).select_from(WritingWork)
        )
        if (existing or 0) >= settings.WRITING_MAX_WORKS_PER_OWNER:
            raise ValueError(
                f"每个账号最多 {settings.WRITING_MAX_WORKS_PER_OWNER} 部作品"
            )

        work = WritingWork(
            owner_id=self._owner_id,
            kind=payload.kind,
            title=payload.title.strip()[:128],
            brief=(payload.brief or "").strip(),
            requirement=(payload.requirement or "").strip()[:512] or None,
            params=sanitize_params(payload.kind, payload.params),
            outline=[],
            meta={},
        )
        self._db.add(work)
        self._db.commit()
        self._db.refresh(work)
        logger.info(
            "writing work created: id=%s owner=%s kind=%s title=%r",
            work.id,
            self._owner_id,
            work.kind,
            work.title,
        )
        return work

    def update_work(self, work_id: int, payload: WritingWorkUpdate) -> WritingWork:
        work = self.get_work(work_id)
        data = payload.model_dump(exclude_unset=True)
        if data.get("title"):
            work.title = data["title"].strip()[:128]
        if "brief" in data:
            work.brief = (data["brief"] or "").strip()
        if "requirement" in data:
            work.requirement = (data["requirement"] or "").strip()[:512] or None
        if data.get("params") is not None:
            # 按作品自己的 kind 洗，不允许借更新把别种参数塞进来。
            work.params = sanitize_params(work.kind, data["params"])
        self._db.commit()
        self._db.refresh(work)
        return work

    def delete_work(self, work_id: int) -> None:
        work = self.get_work(work_id)
        # 正文块是作品的从属行，同事务一起删，不留孤儿。
        self._db.execute(
            delete(WritingPart).where(
                WritingPart.owner_id == self._owner_id, WritingPart.work_id == work.id
            )
        )
        self._db.delete(work)
        self._db.commit()
        logger.info(
            "writing work deleted: id=%s owner=%s title=%r",
            work_id,
            self._owner_id,
            work.title,
        )

    # -- 大纲与正文块 -------------------------------------------------------

    def list_parts(self, work: WritingWork) -> List[WritingPart]:
        return list(
            self._db.scalars(
                select(WritingPart)
                .where(
                    WritingPart.owner_id == self._owner_id,
                    WritingPart.work_id == work.id,
                )
                .order_by(WritingPart.sort_order, WritingPart.id)
            ).all()
        )

    def _get_part(self, work: WritingWork, cid: str) -> WritingPart:
        part = self._db.scalar(
            select(WritingPart).where(
                WritingPart.owner_id == self._owner_id,
                WritingPart.work_id == work.id,
                WritingPart.cid == cid,
            )
        )
        if part is None:
            raise LookupError("章节不存在")
        return part

    def save_outline(self, work_id: int, nodes: List[Any]) -> WritingWork:
        """清洗大纲并对账正文块：新叶子建空块、删掉的叶子连带删块、顺序/标题同步。"""
        work = self.get_work(work_id)
        tree = sanitize_outline(work.kind, nodes)
        leaves = flatten_leaves(tree)
        if len(leaves) > settings.WRITING_MAX_PARTS_PER_WORK:
            raise ValueError(
                f"一部作品最多 {settings.WRITING_MAX_PARTS_PER_WORK} 个章节/段落"
            )

        existing = {part.cid: part for part in self.list_parts(work)}
        keep: set[str] = set()
        for order, leaf in enumerate(leaves):
            cid = str(leaf["cid"])
            keep.add(cid)
            part = existing.get(cid)
            if part is None:
                self._db.add(
                    WritingPart(
                        owner_id=self._owner_id,
                        work_id=work.id,
                        cid=cid,
                        sort_order=order,
                        title=str(leaf["title"]),
                        content="",
                        word_count=0,
                    )
                )
            else:
                part.sort_order = order
                part.title = str(leaf["title"])
        for cid, part in existing.items():
            if cid not in keep:
                self._db.delete(part)

        work.outline = tree
        self._db.commit()
        self._db.refresh(work)
        return work

    def add_part(self, work_id: int, title: str, after_cid: Optional[str]) -> Tuple[WritingWork, str]:
        """手工加章/加段：大纲补一个叶子并建空块，返回 ``(作品, 新 cid)``。"""
        work = self.get_work(work_id)
        tree = sanitize_outline(work.kind, work.outline or [])
        leaf_count = len(flatten_leaves(tree))
        if leaf_count >= settings.WRITING_MAX_PARTS_PER_WORK:
            raise ValueError(
                f"一部作品最多 {settings.WRITING_MAX_PARTS_PER_WORK} 个章节/段落"
            )

        leaf_kind = LEAF_KIND[work.kind]
        clean_title = (title or "").strip()[:128]
        leaf = {
            "cid": new_cid(),
            "kind": leaf_kind,
            "title": clean_title or ("未命名章节" if work.kind == KIND_NOVEL else "未命名段落"),
            "synopsis": "",
            "target_words": None,
        }
        insert_leaf(tree, leaf, after_cid)
        work.outline = tree
        self._db.flush()
        # 走统一对账建块，sort_order 由树的展平顺序决定。
        self._reconcile_existing(work, tree)
        self._db.commit()
        self._db.refresh(work)
        return work, str(leaf["cid"])

    def update_part(
        self, work_id: int, cid: str, *, title: Optional[str], content: Optional[str]
    ) -> WritingPart:
        """正文块自动保存。改名同步大纲叶子，改正文重算字数。"""
        work = self.get_work(work_id)
        part = self._get_part(work, cid)
        if title is not None:
            clean = title.strip()[:128]
            if not clean:
                raise ValueError("标题不能为空")
            part.title = clean
            tree = sanitize_outline(work.kind, work.outline or [])
            if sync_leaf_title(tree, cid, clean):
                work.outline = tree
        if content is not None:
            if len(content) > settings.WRITING_PART_CONTENT_MAX_CHARS:
                raise ValueError(
                    f"单章/单段正文上限 {settings.WRITING_PART_CONTENT_MAX_CHARS} 字符"
                )
            part.content = content
            part.word_count = count_words(content)
        self._db.commit()
        self._db.refresh(part)
        return part

    def delete_part(self, work_id: int, cid: str) -> WritingWork:
        work = self.get_work(work_id)
        part = self._get_part(work, cid)
        tree = sanitize_outline(work.kind, work.outline or [])
        if not remove_leaf(tree, cid):
            # 大纲里找不到但块行还在：照删，让两边重新对齐。
            logger.warning("writing leaf missing in outline: work=%s cid=%s", work_id, cid)
        work.outline = tree
        self._db.delete(part)
        self._db.commit()
        self._db.refresh(work)
        return work

    def _reconcile_existing(self, work: WritingWork, tree: List[Dict[str, Any]]) -> None:
        """add_part 的内部对账：只处理新增叶子（删/改走 save_outline 的完整路径）。"""
        existing = {part.cid: part for part in self.list_parts(work)}
        for order, leaf in enumerate(flatten_leaves(tree)):
            cid = str(leaf["cid"])
            part = existing.get(cid)
            if part is None:
                self._db.add(
                    WritingPart(
                        owner_id=self._owner_id,
                        work_id=work.id,
                        cid=cid,
                        sort_order=order,
                        title=str(leaf["title"]),
                        content="",
                        word_count=0,
                    )
                )
            else:
                part.sort_order = order

    # -- 导出 ---------------------------------------------------------------

    def export_markdown(self, work: WritingWork) -> str:
        """把大纲与各块正文拼成一份 markdown。卷/章/段落标题按层级给 # 数。"""
        tree = sanitize_outline(work.kind, work.outline or [])
        parts = {part.cid: part for part in self.list_parts(work)}
        lines: List[str] = [f"# {work.title}", ""]

        def emit_leaf(leaf: Dict[str, Any], level: int) -> None:
            part = parts.get(str(leaf["cid"]))
            prefix = "#" * level
            lines.append(f"{prefix} {leaf.get('title', '')}")
            lines.append("")
            content = (part.content if part else "").strip()
            if content:
                lines.extend([content, ""])

        for node in tree:
            if node.get("kind") == NODE_VOLUME:
                lines.extend([f"## {node.get('title', '')}", ""])
                for child in node.get("children", []):
                    emit_leaf(child, 3)
            else:
                # 小说顶层章用 ##，文章段落也是 ##（文章没有卷这一层）。
                emit_leaf(node, 2)
        return "\n".join(lines).strip() + "\n"

    def export_text(self, work: WritingWork) -> str:
        """纯文本导出：同一份内容，只去掉 markdown 的 # 标记。"""
        markdown = self.export_markdown(work)
        return re.sub(r"(?m)^#{1,6}\s*", "", markdown)

    # -- 序列化 -------------------------------------------------------------

    def _counts_map(self, work_ids: List[int]) -> Dict[int, Tuple[int, int]]:
        """一次查询取回每部作品的 ``(块数, 总字数)``，列表页不是每行一次查询。"""
        if not work_ids:
            return {}
        rows = self._db.execute(
            select(
                WritingPart.work_id,
                func.count(WritingPart.id),
                func.coalesce(func.sum(WritingPart.word_count), 0),
            )
            .where(
                WritingPart.owner_id == self._owner_id,
                WritingPart.work_id.in_(work_ids),
            )
            .group_by(WritingPart.work_id)
        ).all()
        return {row[0]: (row[1], int(row[2])) for row in rows}

    def to_item(self, work: WritingWork, counts: Optional[Tuple[int, int]] = None) -> WritingWorkItem:
        part_count, word_count = counts or (0, 0)
        return WritingWorkItem(
            id=work.id,
            kind=work.kind,
            title=work.title,
            params=dict(work.params or {}),
            part_count=part_count,
            word_count=word_count,
            created_at=work.created_at,
            updated_at=work.updated_at,
        )

    def to_items(self, works: List[WritingWork]) -> List[WritingWorkItem]:
        counts = self._counts_map([work.id for work in works])
        return [self.to_item(work, counts.get(work.id, (0, 0))) for work in works]

    def to_detail(self, work: WritingWork) -> WritingWorkDetail:
        parts = self.list_parts(work)
        tree = sanitize_outline(work.kind, work.outline or [])
        total_words = sum(part.word_count for part in parts)
        return WritingWorkDetail(
            id=work.id,
            kind=work.kind,
            title=work.title,
            params=dict(work.params or {}),
            part_count=len(parts),
            word_count=total_words,
            created_at=work.created_at,
            updated_at=work.updated_at,
            brief=work.brief or "",
            requirement=work.requirement,
            outline=[OutlineNode(**node) for node in tree],
            meta=dict(work.meta or {}),
            parts=[
                WritingPartOut(
                    cid=part.cid,
                    sort_order=part.sort_order,
                    title=part.title,
                    content=part.content or "",
                    word_count=part.word_count,
                    created_at=part.created_at,
                    updated_at=part.updated_at,
                )
                for part in parts
            ],
        )

    # 供路由复用的小工具 -----------------------------------------------------

    @staticmethod
    def snippet(text: Optional[str], limit: int = 140) -> Optional[str]:
        """折叠空白后的一行摘要（列表副标题预留，M1 暂未挂出）。"""
        if not text:
            return None
        flat = _WS_RE.sub(" ", text).strip()
        if not flat:
            return None
        return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"
