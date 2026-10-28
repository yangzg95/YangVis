"""智能办公 · 求职笔记的存取。

数据访问按单个用户隔离（与 :class:`InterviewService` 同一约定）：所有查询都经过
:meth:`NoteService._scope`，handler 不直接接触 session，他人的笔记 id 一律读作 404。

列表不返回正文：一行只给标题、摘要与时间，正文走详情端点。摘要在序列化时由正文
折叠空白截取，不落库——正文改了摘要自然跟着改，没有两份事实要同步。
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Note
from app.models.schemas import NoteCreate, NoteDetail, NoteItem, NoteUpdate

logger = logging.getLogger("yangvis.note")

# 列表里每条摘要的最长字符数。
SNIPPET_LEN = 140

# 连续空白（含换行）折叠成一个空格：摘要是给表格副标题用的一行。
_WS_RE = re.compile(r"\s+")

# LIKE 通配符转义：用户搜的 % / _ 是普通字符，配合 ilike 的 escape='/'。
_LIKE_ESCAPE = str.maketrans({"/": "//", "%": "/%", "_": "/_"})


def _like_term(keyword: str) -> str:
    """把搜索词包成安全的 LIKE 模式：转义通配符，两边模糊匹配。"""
    return f"%{keyword.translate(_LIKE_ESCAPE)}%"


class NoteService:
    """针对单个用户的求职笔记做增删改查。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(Note.owner_id == self._owner_id)

    # -- CRUD ---------------------------------------------------------------

    def list_records(self, keyword: Optional[str] = None) -> List[Note]:
        stmt = self._scope(select(Note))
        kw = (keyword or "").strip()
        if kw:
            term = _like_term(kw)
            # ilike 在 MySQL 上由 SQLAlchemy 编译成 LOWER(col) LIKE LOWER(:p)。
            stmt = stmt.where(
                or_(Note.title.ilike(term, escape="/"), Note.content.ilike(term, escape="/"))
            )
        stmt = stmt.order_by(Note.updated_at.desc(), Note.id.desc())
        return list(self._db.scalars(stmt).all())

    def get_record(self, note_id: int) -> Note:
        record = self._db.scalar(self._scope(select(Note).where(Note.id == note_id)))
        if record is None:
            raise LookupError("笔记不存在")
        return record

    def create_record(self, payload: NoteCreate) -> Note:
        record = Note(
            owner_id=self._owner_id,
            title=payload.title.strip(),
            content=(payload.content or "").strip() or None,
        )
        self._db.add(record)
        self._db.commit()
        self._db.refresh(record)
        logger.info("owner %s created note %s (%r)", self._owner_id, record.id, record.title)
        return record

    def update_record(self, note_id: int, payload: NoteUpdate) -> Note:
        record = self.get_record(note_id)
        # exclude_unset：没传的字段保持不动；显式传 null 的正文要真的清空。
        data = payload.model_dump(exclude_unset=True)
        # 标题非空（含纯空格拦截）由 NoteUpdate 的校验器保证，这里落 strip 后的值。
        if data.get("title"):
            record.title = data["title"].strip()
        if "content" in data:
            record.content = (data["content"] or "").strip() or None
        self._db.commit()
        self._db.refresh(record)
        return record

    def delete_record(self, note_id: int) -> None:
        record = self.get_record(note_id)
        self._db.delete(record)
        self._db.commit()
        logger.info("owner %s deleted note %s (%r)", self._owner_id, note_id, record.title)

    # -- 序列化 -------------------------------------------------------------

    @staticmethod
    def _snippet(content: Optional[str]) -> Optional[str]:
        if not content:
            return None
        text = _WS_RE.sub(" ", content).strip()
        if not text:
            return None
        return text if len(text) <= SNIPPET_LEN else text[:SNIPPET_LEN].rstrip() + "…"

    @classmethod
    def to_item(cls, record: Note) -> NoteItem:
        return NoteItem(
            id=record.id,
            title=record.title,
            snippet=cls._snippet(record.content),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def to_detail(self, record: Note) -> NoteDetail:
        return NoteDetail(**self.to_item(record).model_dump(), content=record.content)
