"""素材库存取：列表筛选、上传登记、删除与引用解绑。

数据访问按单个用户隔离（与 :class:`NoteService` 同一约定）：所有查询都经过
:meth:`MaterialService._scope`，别人的素材 id 读作不存在——库是账号级的，越界
的 id 与打错的 id 不该给出两种答案。

删除是这里唯一跨表的一步：``video_shot.material_ids`` 指的是本表的 id，删一条素材
要把引用它的镜头一起去掉，否则镜头指向一个已经不存在的文件，要到渲染那一刻才在
「参考图路径不合法」这种地方炸出来。反查用 Python 过滤而不是数据库的 JSON 函数：
MySQL 的 ``json_contains`` 与 SQLite 的不同名，而一个账号的镜头行本来就只在几百这个
量级上，读一遍比在两种数据库上各维护一条 SQL 便宜。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models.entities import MaterialAsset, VideoShot
from app.services.materials import MATERIAL_KINDS
from app.services.materials.store import StoredMaterial

logger = logging.getLogger("yangvis.materials")

settings: Settings = get_settings()

# 一次最多取回多少条。库的上限是一个账号 200 条，所以这个数只在分页参数被手改时兜底。
MAX_PAGE_SIZE = 200

# LIKE 通配符转义，与 services/note.py 同一张表：用户搜的 % 与 _ 是普通字符。
_LIKE_ESCAPE = str.maketrans({"/": "//", "%": "/%", "_": "/_"})


class MaterialService:
    """针对单个用户的素材库做存取。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(MaterialAsset.owner_id == self._owner_id)

    # -- 读 -------------------------------------------------------------------

    def list_assets(
        self,
        kind: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 60,
        offset: int = 0,
    ) -> Tuple[List[MaterialAsset], int, int]:
        """按类别与名称筛选的一页素材，外加「筛选后多少条」与「全库占用多少字节」。

        两个总数分开算：``total`` 跟着筛选条件走，分页才数得对；``bytes_total`` 是全库
        用量，界面用它说「还能再放多少条」，与当前筛出来的是哪几条无关。
        """
        stmt = self._scope(select(MaterialAsset))
        if kind in MATERIAL_KINDS:
            stmt = stmt.where(MaterialAsset.kind == kind)
        term = (keyword or "").strip()
        if term:
            stmt = stmt.where(MaterialAsset.name.ilike(f"%{term.translate(_LIKE_ESCAPE)}%", escape="/"))

        total = int(self._db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = list(
            self._db.scalars(
                stmt.order_by(MaterialAsset.created_at.desc(), MaterialAsset.id.desc())
                .limit(max(1, min(int(limit or 60), MAX_PAGE_SIZE)))
                .offset(max(0, int(offset or 0)))
            ).all()
        )
        bytes_total = int(
            self._db.scalar(
                select(func.sum(MaterialAsset.size)).where(MaterialAsset.owner_id == self._owner_id)
            )
            or 0
        )
        return rows, total, bytes_total

    def get_asset(self, asset_id: int) -> MaterialAsset:
        asset = self._db.scalar(self._scope(select(MaterialAsset).where(MaterialAsset.id == asset_id)))
        if asset is None:
            raise LookupError("素材不存在")
        return asset

    # -- 写 -------------------------------------------------------------------

    def add(self, stored: StoredMaterial, name: str) -> MaterialAsset:
        """登记一条已落盘的素材；超量时拒绝，文件由调用方删。

        视频素材在这里只是**被存下来**：本期的渲染只到首帧，参考视频要发给上游得靠
        公网可达的媒体地址，那一条链路还没接通，所以界面与提示都要说清它现在的角色。
        """
        count = int(
            self._db.scalar(
                select(func.count(MaterialAsset.id)).where(MaterialAsset.owner_id == self._owner_id)
            )
            or 0
        )
        if count >= settings.MATERIAL_MAX_PER_OWNER:
            raise ValueError(f"素材库最多存 {settings.MATERIAL_MAX_PER_OWNER} 条，先删掉不用的")

        asset = MaterialAsset(
            owner_id=self._owner_id,
            kind=stored.kind,
            name=(name or "").strip()[:128] or stored.rel_path.rsplit("/", 1)[-1],
            mime=stored.mime,
            size=stored.size,
            rel_path=stored.rel_path,
        )
        self._db.add(asset)
        self._db.commit()
        self._db.refresh(asset)
        logger.info(
            "material added: id=%s owner=%s kind=%s bytes=%d",
            asset.id,
            self._owner_id,
            asset.kind,
            asset.size,
        )
        return asset

    def remove(self, asset_id: int) -> Tuple[str, int]:
        """删一条素材并解掉引用它的镜头。

        返回删掉那条的 ``rel_path``（磁盘清理交给调用方，与短视频删参考图同一
        分工：库里的事务先落定，再动文件）与解绑的镜头数。
        """
        asset = self.get_asset(asset_id)
        rel_path = str(asset.rel_path or "")
        wanted = str(asset.id)
        self._db.delete(asset)

        unbound = 0
        rows = list(
            self._db.scalars(select(VideoShot).where(VideoShot.owner_id == self._owner_id)).all()
        )
        for row in rows:
            refs = [str(item) for item in (row.material_ids or [])]
            if wanted in refs:
                row.material_ids = [item for item in refs if item != wanted]
                unbound += 1
        self._db.commit()
        logger.info(
            "material removed: id=%s owner=%s unbound_shots=%s",
            wanted,
            self._owner_id,
            unbound,
        )
        return rel_path, unbound

    # -- 供短视频侧批量读 -------------------------------------------------------

    def materials_for(self, asset_ids: List[str]) -> Dict[str, MaterialAsset]:
        """按 id 批量取本账号的素材，返回 ``{id 字符串: 行}``。

        首帧渲染入队时要知道每条素材是图还是视频：只有图片能进模型，视频留着
        等参考视频那条链路，所以这里把整行交出去而不是只给路径。
        """
        ids = []
        for item in asset_ids:
            try:
                ids.append(int(str(item)))
            except (TypeError, ValueError):
                continue
        if not ids:
            return {}
        rows = self._db.scalars(
            self._scope(select(MaterialAsset).where(MaterialAsset.id.in_(ids)))
        ).all()
        return {str(row.id): row for row in rows}
