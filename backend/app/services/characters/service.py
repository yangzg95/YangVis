"""人物库存取：列表筛选、建档、改档、换形象图与硬删除。

数据访问按单个用户隔离（与 :class:`MaterialService` 同一约定）：所有查询都经过
:meth:`CharacterService._scope`，别人的角色 id 读作不存在——库是账号级的，越界的
id 与打错的 id 不该给出两种答案。

删除是**硬删除**而不是素材库那套回收期：角色条目少、删除是低频且 deliberate 的
操作，界面会先展示引用影响面再让用户确认。项目里残留的 ``character_ids`` 不在
删除时逐条改写（视频项目可能正被编辑），而由视频侧在保存与渲染时按「id 已不存在」
自然跳过——与素材文件丢失的降级口径一致。反查用 Python 过滤而不是数据库的 JSON
函数：MySQL 的 ``json_contains`` 与 SQLite 的不同名，而一个账号的项目行本来就不多。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models.entities import VideoCharacter, VideoProject
from app.models.schemas import CharacterUsageOut, CharacterUsageProject
from app.services.characters.store import StoredCharacterImage
from app.services.media.store import delete_blob

logger = logging.getLogger("yangvis.characters")

settings: Settings = get_settings()

MAX_PAGE_SIZE = 200

# LIKE 通配符转义，与 services/note.py、materials/service.py 同一张表：
# 用户搜的 % 与 _ 是普通字符。
_LIKE_ESCAPE = str.maketrans({"/": "//", "%": "/%", "_": "/_"})

# 文本字段的落库上限，与 entities.VideoCharacter 的列宽对齐。
_NAME_MAX = 64
_TEXT_MAX = 512


def _clean_text(value: Any, limit: int = _TEXT_MAX) -> str:
    """None/空白归一成空串，再按列宽截断。"""
    return str(value or "").strip()[:limit]


class CharacterService:
    """针对单个用户的人物库做存取。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(VideoCharacter.owner_id == self._owner_id)

    # -- 读 -------------------------------------------------------------------

    def list_characters(
        self,
        keyword: Optional[str] = None,
        limit: int = 60,
        offset: int = 0,
    ) -> Tuple[List[VideoCharacter], int]:
        """按名称筛选的一页角色 + 筛选后的总数（分页按它算，不分全库用量）。"""
        stmt = self._scope(select(VideoCharacter))
        term = (keyword or "").strip().translate(_LIKE_ESCAPE)
        if term:
            stmt = stmt.where(VideoCharacter.name.ilike(f"%{term}%", escape="/"))
        total = int(self._db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = list(
            self._db.scalars(
                stmt.order_by(VideoCharacter.id.desc())
                .limit(max(1, min(int(limit or 60), MAX_PAGE_SIZE)))
                .offset(max(0, int(offset or 0)))
            ).all()
        )
        return rows, total

    def get_character(self, character_id: int) -> VideoCharacter:
        row = self._db.scalar(
            self._scope(
                select(VideoCharacter).where(VideoCharacter.id == character_id)
            )
        )
        if row is None:
            raise LookupError("人物不存在")
        return row

    def characters_for(self, ids: List[str]) -> Dict[str, VideoCharacter]:
        """按 id 批量取本账号的角色，返回 ``{id 字符串: 行}``。

        与 :meth:`MaterialService.materials_for` 同一口径：只返回本账号**仍存在**
        的行，他人的、已删除的、模型编出来的 id 一律不出现在结果里，调用方按缺失
        跳过即可。
        """
        numeric: List[int] = []
        for item in ids or []:
            try:
                numeric.append(int(str(item)))
            except (TypeError, ValueError):
                continue
        if not numeric:
            return {}
        rows = self._db.scalars(
            self._scope(select(VideoCharacter).where(VideoCharacter.id.in_(numeric)))
        ).all()
        return {str(row.id): row for row in rows}

    # -- 写 -------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        persona: str,
        outfit: str,
        features: str,
        stored: StoredCharacterImage,
    ) -> VideoCharacter:
        """登记一个带形象图的角色；超条数闸门拒绝，文件由调用方负责删。"""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("人物名称不能为空")
        count = int(
            self._db.scalar(
                select(func.count(VideoCharacter.id)).where(
                    VideoCharacter.owner_id == self._owner_id
                )
            )
            or 0
        )
        if count >= settings.CHARACTER_MAX_PER_OWNER:
            raise ValueError(
                f"人物库最多建 {settings.CHARACTER_MAX_PER_OWNER} 个角色，先删掉不用的"
            )

        row = VideoCharacter(
            owner_id=self._owner_id,
            name=clean_name[:_NAME_MAX],
            persona=_clean_text(persona),
            outfit=_clean_text(outfit),
            features=_clean_text(features),
            mime=stored.mime,
            size=stored.size,
            rel_path=stored.rel_path,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info(
            "character created: id=%s owner=%s name=%r bytes=%d",
            row.id,
            self._owner_id,
            row.name,
            row.size,
        )
        return row

    def update(self, character_id: int, **fields: Any) -> VideoCharacter:
        """改文字档案；``name`` 给了就不能是空白，没给的字段保持原样。"""
        row = self.get_character(character_id)
        if fields.get("name") is not None:
            clean_name = str(fields["name"]).strip()
            if not clean_name:
                raise ValueError("人物名称不能为空")
            row.name = clean_name[:_NAME_MAX]
        if fields.get("persona") is not None:
            row.persona = _clean_text(fields["persona"])
        if fields.get("outfit") is not None:
            row.outfit = _clean_text(fields["outfit"])
        if fields.get("features") is not None:
            row.features = _clean_text(fields["features"])
        self._db.commit()
        self._db.refresh(row)
        logger.info("character updated: id=%s owner=%s name=%r", row.id, self._owner_id, row.name)
        return row

    def replace_portrait(
        self, character_id: int, stored: StoredCharacterImage
    ) -> VideoCharacter:
        """换形象图：新文件先落盘、库里的事实先提交，旧文件 best-effort 再删。

        与素材删除同一分工：磁盘慢一步或删失败都不该让换图失败，旧文件顶多成孤儿，
        而媒体目录按账号分、容量可控。
        """
        row = self.get_character(character_id)
        old_rel_path = row.rel_path
        row.mime = stored.mime
        row.size = stored.size
        row.rel_path = stored.rel_path
        self._db.commit()
        self._db.refresh(row)
        if old_rel_path and old_rel_path != stored.rel_path:
            delete_blob(old_rel_path)
        logger.info(
            "character portrait replaced: id=%s owner=%s old=%s new=%s",
            row.id,
            self._owner_id,
            old_rel_path,
            stored.rel_path,
        )
        return row

    def remove(self, character_id: int) -> int:
        """硬删除一个角色，返回仍引用它的视频项目数（仅用于界面提示）。

        行先删、事务先落定，磁盘文件 best-effort 跟着清：删文件失败只留日志，不能
        让用户删不掉一个角色。残留引用不改写——读取/渲染侧按 id 缺失自然跳过。
        """
        row = self.get_character(character_id)
        references = self._reference_count({str(character_id)})
        rel_path = row.rel_path
        self._db.delete(row)
        self._db.commit()
        delete_blob(rel_path)
        logger.info(
            "character removed: id=%s owner=%s references=%s file=%s",
            character_id,
            self._owner_id,
            references,
            rel_path,
        )
        return references

    # -- 引用影响 ---------------------------------------------------------------

    def usage(self, character_ids: List[int]) -> List[CharacterUsageOut]:
        """每个角色被哪些短视频项目选进角色组，删除前给用户看影响面。

        反查口径与 :class:`MaterialService` 相同：读一遍本账号的项目行再按 id 过滤，
        结果按输入顺序建桶，项目按新→旧排序。
        """
        wanted = {str(item) for item in character_ids}
        if not wanted:
            return []
        projects = list(
            self._db.scalars(
                select(VideoProject)
                .where(VideoProject.owner_id == self._owner_id)
                .order_by(VideoProject.created_at.desc(), VideoProject.id.desc())
            ).all()
        )
        buckets: Dict[str, List[VideoProject]] = {str(item): [] for item in character_ids}
        for project in projects:
            refs = {str(item) for item in (project.character_ids or [])} & wanted
            for ref in refs:
                buckets[ref].append(project)
        return [
            CharacterUsageOut(
                character_id=int(character_id),
                projects=[
                    CharacterUsageProject(project_id=project.id, title=project.title)
                    for project in buckets[str(character_id)]
                ],
            )
            for character_id in character_ids
        ]

    def _reference_count(self, wanted: set) -> int:
        """仍引用这批角色 id 的项目数（删除提示用，同一项目只算一次）。"""
        if not wanted:
            return 0
        rows = self._db.scalars(
            select(VideoProject).where(VideoProject.owner_id == self._owner_id)
        ).all()
        return sum(
            1
            for project in rows
            if {str(item) for item in (project.character_ids or [])} & wanted
        )
