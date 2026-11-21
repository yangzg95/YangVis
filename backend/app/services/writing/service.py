"""创作模块的存取：作品、大纲与正文块的 CRUD、对账和导出。

数据访问按单个用户隔离（与 :class:`NoteService` 同一约定）：所有作品查询都经过
:meth:`WritingService._scope`，正文块先过作品归属这道门，他人的 id 一律读作 404。

大纲（``writing_work.outline`` 的 JSON 树）是目录的唯一事实源，正文块行与大纲叶子
按 cid 1:1 对账：保存大纲时新叶子建空块、被删的叶子连带删块、顺序变化改
``sort_order``。标题在两边同步——大纲改名与正文块改名走的是同一棵树。

M2 起大纲生成走内置工作流 + 后台任务：本表仍只存产物，进度与失败原因只有
``workflow_run`` 一份事实源，序列化时由 :func:`subject_state` 翻译（同 slide_deck）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import (
    ModelConfig,
    WorkflowRun,
    WorkflowRunStep,
    WritingPart,
    WritingWork,
)
from app.models.schemas import (
    OutlineNode,
    WritingPartOut,
    WritingWorkCreate,
    WritingWorkDetail,
    WritingWorkItem,
    WritingWorkUpdate,
)
from app.services.agent_run import model_error_text, require_chat_model
from app.services.workflows.builtin import builtin_output, start_builtin
from app.services.workflows.runs import (
    fail_run,
    latest_run,
    latest_runs,
    running_subjects,
    subject_state,
)
from app.services.workflows import (
    RUN_SUCCEEDED,
    SUBJECT_UPLOADED,
    SUBJECT_WRITING_WORK,
)
from app.services.writing import generate
from app.services.writing.outline import (
    KIND_ARTICLE,
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


def _like_term(keyword: str) -> str:
    return f"%{keyword.translate(_LIKE_ESCAPE)}%"


class WritingService:
    """针对单个用户的创作作品做增删改查、大纲对账与导出。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        # 路由把 owner 传给后台任务用（同 SlideDeckService 的暴露方式）。
        return self._owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(WritingWork.owner_id == self._owner_id)

    def _assert_not_generating(self, work: WritingWork) -> None:
        """大纲运行期间冻结所有会改大纲/正文的手工操作。

        生成落库是整棵树覆盖 + part 对账（模型产物不带 cid，旧叶子会被当成孤儿
        删掉），运行中允许手改等于让用户白写：请求直接挡在门外，比静默覆盖友好。
        后台任务自己的 save_outline 在运行 succeeded 之后才执行，不在此列。
        """
        if running_subjects(self._db, self._owner_id, SUBJECT_WRITING_WORK, [work.id]):
            raise ValueError("大纲正在生成中，请稍后再编辑")

    # -- 模型门槛（与幻灯片同一份共用实现）-------------------------------------

    def require_chat_config(self) -> ModelConfig:
        """生成门槛：用户得先有一份验证过的默认对话模型。"""
        return require_chat_model(self._db, self._owner_id)

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
        # 大纲还在后台生成时连作品一起删，任务回来就找不到认领对象了——同 deck 闸门。
        if running_subjects(self._db, self._owner_id, SUBJECT_WRITING_WORK, [work.id]):
            raise ValueError("作品正在生成大纲，请稍后再删")
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

    # -- AI 大纲生成（M2）----------------------------------------------------

    def start_outline_generation(
        self,
        work_id: int,
        requirement: Optional[str] = None,
        model_config_id: Optional[int] = None,
    ) -> WritingWork:
        """建一条认领该作品的大纲运行。

        ``model_config_id`` 给了就把这次运行的步骤行盖成指定模型（必须是本人的、
        对话用途且测通的配置）；不传则步骤行为空，执行时回落默认对话模型。
        """
        work = self.get_work(work_id)
        if running_subjects(self._db, self._owner_id, SUBJECT_WRITING_WORK, [work.id]):
            raise ValueError("大纲正在生成中，请勿重复触发")
        # 门槛在请求里先过：没测通的配置直接 BusinessError（4004），不留下排队中的运行。
        config = require_chat_model(self._db, self._owner_id, model_config_id)
        if requirement is not None:
            work.requirement = requirement.strip()[:512] or None
            self._db.commit()
            self._db.refresh(work)
        run = start_builtin(
            self._db,
            self._owner_id,
            generate.workflow_slug_for(work.kind),
            generate.outline_inputs(work),
            subject=(SUBJECT_WRITING_WORK, work.id),
            subject_label=work.title,
        )
        if model_config_id is not None:
            # 运行步骤在建运行时一次性建齐（pending）：把选择的模型盖到每一步上，
            # 引擎执行时读行上的 model_config_id，空着才回落默认。
            for step in self._db.scalars(
                select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
            ).all():
                step.model_config_id = config.id
            self._db.commit()
        logger.info(
            "writing outline run started: id=%s owner=%s kind=%s model=%s",
            work.id,
            self._owner_id,
            work.kind,
            config.id,
        )
        return work

    async def run_outline_generation(self, work_id: int) -> None:
        """后台任务体：跑完发起时建好的运行，解析产物并按手工保存同一套闸门落库。

        解析不出结构、一个合法叶子都没有、节点超上限这类**后处理**失败，统一用
        :func:`fail_run` 把运行降级——不允许「succeeded 但没有大纲」。
        """
        work = self.get_work(work_id)
        run = latest_run(self._db, self._owner_id, SUBJECT_WRITING_WORK, work.id)
        if run is None:
            logger.error("writing work has no run to execute: id=%s", work_id)
            return
        try:
            text = await builtin_output(self._db, self._owner_id, run.id)
            title_candidates: List[str] = []
            if work.kind == KIND_ARTICLE:
                nodes, title_candidates = generate.parse_article_outline(text)
            else:
                nodes = generate.parse_novel_outline(text)
            # save_outline 内部发 cid、收层级、卡节点上限并对账 part 行（覆盖旧大纲）。
            saved = self.save_outline(work_id, nodes)
            if not flatten_leaves(saved.outline or []):
                raise ValueError("模型返回的大纲里没有任何合法章节/段落")
            if title_candidates:
                meta = dict(work.meta or {})
                meta["title_candidates"] = title_candidates
                work.meta = meta
                self._db.commit()
        except Exception as exc:  # noqa: BLE001 - 失败写在运行上，不抛给后台调度器
            logger.warning(
                "writing outline generation failed: id=%s error=%s", work_id, exc, exc_info=True
            )
            fail_run(self._db, self._owner_id, run.id, model_error_text(exc))

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
        self._assert_not_generating(work)
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
        self._assert_not_generating(work)
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
        self._assert_not_generating(work)
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
        self._assert_not_generating(work)
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

    def _item_of(
        self,
        work: WritingWork,
        run: Optional[WorkflowRun],
        counts: Optional[Tuple[int, int]] = None,
    ) -> WritingWorkItem:
        part_count, word_count = counts or (0, 0)
        has_outline = bool(flatten_leaves(work.outline or []))
        # 产物 = 大纲里有叶子；没有运行又没有大纲的新作就是「待生成」（uploaded）。
        status, error_msg = subject_state(
            run, has_product=has_outline, idle=SUBJECT_UPLOADED
        )
        # 通用契约里「succeeded 但无产物」判失败，是给步骤重跑那种后处理缺失兜底的；
        # 创作侧的空产物在任务里已被 fail_run 降级成 failed 运行，能走到这里的
        # succeeded + 空大纲只可能是生成成功后被手工清空——按「待生成」呈现，
        # 不该给用户亮一条「结果没有写回，请重新发起」的红色失败。
        if run is not None and run.status == RUN_SUCCEEDED and not has_outline:
            status, error_msg = SUBJECT_UPLOADED, None
        return WritingWorkItem(
            id=work.id,
            kind=work.kind,
            title=work.title,
            params=dict(work.params or {}),
            part_count=part_count,
            word_count=word_count,
            status=status,
            error_msg=error_msg,
            created_at=work.created_at,
            updated_at=work.updated_at,
        )

    def to_item(self, work: WritingWork, counts: Optional[Tuple[int, int]] = None) -> WritingWorkItem:
        run = latest_run(self._db, self._owner_id, SUBJECT_WRITING_WORK, work.id)
        return self._item_of(work, run, counts)

    def to_items(self, works: List[WritingWork]) -> List[WritingWorkItem]:
        counts = self._counts_map([work.id for work in works])
        runs = latest_runs(
            self._db, self._owner_id, SUBJECT_WRITING_WORK, [work.id for work in works]
        )
        return [
            self._item_of(work, runs.get(str(work.id)), counts.get(work.id, (0, 0)))
            for work in works
        ]

    def to_detail(self, work: WritingWork) -> WritingWorkDetail:
        parts = self.list_parts(work)
        tree = sanitize_outline(work.kind, work.outline or [])
        return WritingWorkDetail(
            **self.to_item(work, counts=(len(parts), sum(p.word_count for p in parts))).model_dump(),
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
