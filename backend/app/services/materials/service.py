"""素材库存取：列表筛选、上传登记、删除与引用解绑。

数据访问按单个用户隔离（与 :class:`NoteService` 同一约定）：所有查询都经过
:meth:`MaterialService._scope`，别人的素材 id 读作不存在——库是账号级的，越界
的 id 与打错的 id 不该给出两种答案。

删除分两步：先只写 ``deleted_at`` 进回收期（列表与计数立刻不再看它，但行、文件与
镜头引用都原样留着，撤销就是把这一列清回去）；过了 ``MATERIAL_TRASH_TTL_SECONDS``
才由 :meth:`MaterialService.purge_expired` 真删——解掉 ``video_shot.material_ids``
里指向它的引用、删掉文件。引用必须在真删时解，否则镜头会指向一个已经不存在的文件，
要到渲染那一刻才在「参考图路径不合法」这种地方炸出来。反查用 Python 过滤而不是
数据库的 JSON 函数：MySQL 的 ``json_contains`` 与 SQLite 的不同名，而一个账号的
镜头行本来就只在几百这个量级上，读一遍比在两种数据库上各维护一条 SQL 便宜。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models.entities import MaterialAsset, VideoProject, VideoShot
from app.models.schemas import (
    MaterialUsageOut,
    MaterialUsageProject,
    MaterialUsageShot,
)
from app.services.materials import MATERIAL_KINDS
from app.services.materials.store import StoredMaterial
from app.services.media.store import delete_blob
from app.timeutil import db_now

logger = logging.getLogger("yangvis.materials")

settings: Settings = get_settings()

# 一次最多取回多少条。库的上限是一个账号 200 条，所以这个数只在分页参数被手改时兜底。
MAX_PAGE_SIZE = 200

# LIKE 通配符转义，与 services/note.py 同一张表：用户搜的 % 与 _ 是普通字符。
_LIKE_ESCAPE = str.maketrans({"/": "//", "%": "/%", "_": "/_"})

# 打标分组与名字同一长度上限：分组标题由它原样充当，再长只会把界面撑坏。
_MAX_TAG_LEN = 128


def _normalize_tag(value: str | None) -> str | None:
    """空串归一成 ``None``：打了标与没打标是两种分组，不留给「空标签」第三态。"""
    text = (value or "").strip()
    return text[:_MAX_TAG_LEN] or None


def _int_ids(values: List[int] | None) -> List[int]:
    """批量 id 的归一：去重、丢非法值，保留调用方给的顺序。"""
    ids: List[int] = []
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in ids:
            ids.append(number)
    return ids


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

    def _live(self, stmt: Select) -> Select:
        """只取正常可见的行：回收期里的素材对读路径等于已经删了。"""
        return stmt.where(MaterialAsset.deleted_at.is_(None))

    @property
    def trash_ttl_seconds(self) -> int:
        return max(0, int(settings.MATERIAL_TRASH_TTL_SECONDS))

    # -- 读 -------------------------------------------------------------------

    def list_assets(
        self,
        kind: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 60,
        offset: int = 0,
    ) -> Tuple[List[MaterialAsset], int, int]:
        """按类别与名称/分组筛选的一页素材，外加「筛选后多少条」与「全库占用多少字节」。

        两个总数分开算：``total`` 跟着筛选条件走，分页才数得对；``bytes_total`` 是全库
        用量，界面用它说「还能再放多少条」，与当前筛出来的是哪几条无关。
        """
        # 懒清理搭在列表上：不引入后台任务，过期条目最迟在用户下次打开素材库时消失。
        self.purge_expired()
        stmt = self._live(self._scope(select(MaterialAsset)))
        if kind in MATERIAL_KINDS:
            stmt = stmt.where(MaterialAsset.kind == kind)
        term = (keyword or "").strip().translate(_LIKE_ESCAPE)
        if term:
            # 名字与打标分组一起搜：从短视频存进来的首帧名字是「项目名｜第 N 镜首帧」，
            # 但打标分组才是它的检索键——搜项目名两条路都得能命中。
            like = f"%{term}%"
            stmt = stmt.where(or_(MaterialAsset.name.ilike(like, escape="/"), MaterialAsset.tag.ilike(like, escape="/")))

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
                select(func.sum(MaterialAsset.size)).where(
                    MaterialAsset.owner_id == self._owner_id,
                    MaterialAsset.deleted_at.is_(None),
                )
            )
            or 0
        )
        return rows, total, bytes_total

    def get_asset(self, asset_id: int) -> MaterialAsset:
        asset = self._db.scalar(
            self._live(self._scope(select(MaterialAsset).where(MaterialAsset.id == asset_id)))
        )
        if asset is None:
            raise LookupError("素材不存在")
        return asset

    def list_trash(
        self,
        kind: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 60,
        offset: int = 0,
    ) -> Tuple[List[MaterialAsset], int]:
        """回收期里的素材一页 + 筛选后总数。

        与 :meth:`list_assets` 反着来：只看 ``deleted_at`` 非空的行；先懒清理过期项，
        让回收站里永远不会出现「倒计时已经是 0 却还能恢复」的行。排序按删除时刻倒序——
        晚删的剩余窗口最长，恢复价值最高，放最前。
        """
        self.purge_expired()
        stmt = self._scope(select(MaterialAsset)).where(MaterialAsset.deleted_at.is_not(None))
        if kind in MATERIAL_KINDS:
            stmt = stmt.where(MaterialAsset.kind == kind)
        term = (keyword or "").strip().translate(_LIKE_ESCAPE)
        if term:
            like = f"%{term}%"
            stmt = stmt.where(
                or_(
                    MaterialAsset.name.ilike(like, escape="/"),
                    MaterialAsset.tag.ilike(like, escape="/"),
                )
            )
        total = int(self._db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = list(
            self._db.scalars(
                stmt.order_by(MaterialAsset.deleted_at.desc(), MaterialAsset.id.desc())
                .limit(max(1, min(int(limit or 60), MAX_PAGE_SIZE)))
                .offset(max(0, int(offset or 0)))
            ).all()
        )
        return rows, total

    def trash_count(self) -> int:
        """回收站内还没过期的条数，给列表页的入口角标用；同样先懒清理。"""
        self.purge_expired()
        return int(
            self._db.scalar(
                select(func.count(MaterialAsset.id)).where(
                    MaterialAsset.owner_id == self._owner_id,
                    MaterialAsset.deleted_at.is_not(None),
                )
            )
            or 0
        )

    # -- 写 -------------------------------------------------------------------

    def add(self, stored: StoredMaterial, name: str, tag: str | None = None) -> MaterialAsset:
        """登记一条已落盘的素材；超量时拒绝，文件由调用方删。

        ``tag`` 是打标分组（可空）：短视频侧存首帧时自动打上项目名，直接上传的
        由用户在素材库里补。视频素材在这里只是**被存下来**：本期的渲染只到首帧，
        参考视频要发给上游得靠公网可达的媒体地址，那一条链路还没接通，所以界面
        与提示都要说清它现在的角色。
        """
        count = int(
            self._db.scalar(
                select(func.count(MaterialAsset.id)).where(
                    MaterialAsset.owner_id == self._owner_id,
                    MaterialAsset.deleted_at.is_(None),
                )
            )
            or 0
        )
        if count >= settings.MATERIAL_MAX_PER_OWNER:
            raise ValueError(f"素材库最多存 {settings.MATERIAL_MAX_PER_OWNER} 条，先删掉不用的")

        asset = MaterialAsset(
            owner_id=self._owner_id,
            kind=stored.kind,
            name=(name or "").strip()[:128] or stored.rel_path.rsplit("/", 1)[-1],
            tag=_normalize_tag(tag),
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

    def rename(self, asset_id: int, name: str) -> MaterialAsset:
        """给素材改名；空名拒绝——名字是列表里唯一认得出这条素材的东西。"""
        asset = self.get_asset(asset_id)
        text = (name or "").strip()
        if not text:
            raise ValueError("名字不能为空")
        asset.name = text[:_MAX_TAG_LEN]
        self._db.commit()
        self._db.refresh(asset)
        logger.info("material renamed: id=%s owner=%s name=%r", asset.id, self._owner_id, asset.name)
        return asset

    def set_tag(self, asset_id: int, tag: str | None) -> MaterialAsset:
        """改一条素材的打标分组；空串按「摘掉标签」处理，归一回按时间分组。"""
        asset = self.get_asset(asset_id)
        asset.tag = _normalize_tag(tag)
        self._db.commit()
        self._db.refresh(asset)
        logger.info(
            "material tagged: id=%s owner=%s tag=%r", asset.id, self._owner_id, asset.tag
        )
        return asset

    def set_tag_many(self, asset_ids: List[int], tag: str | None) -> List[MaterialAsset]:
        """整组改名用：一批素材一次落库，不再逐条发请求。

        找不到的 id 直接跳过（可能刚被另一处删掉）；返回真正改到的那几条，
        调用方按它算「更新了 N 条」。
        """
        ids = _int_ids(asset_ids)
        if not ids:
            raise ValueError("没有选中素材")
        rows = list(
            self._db.scalars(
                self._live(self._scope(select(MaterialAsset).where(MaterialAsset.id.in_(ids))))
            ).all()
        )
        normalized = _normalize_tag(tag)
        for row in rows:
            row.tag = normalized
        self._db.commit()
        for row in rows:
            self._db.refresh(row)
        logger.info(
            "materials tagged: owner=%s ids=%s tag=%r", self._owner_id, ids, normalized
        )
        return rows

    def remove(self, asset_id: int) -> int:
        """把一条素材移进回收期，返回引用它的镜头数。

        回收期内行、文件与镜头引用都原样留着——撤销就是把 ``deleted_at`` 清回去，
        所以这里一个字都不能改 ``video_shot.material_ids``。真正解引用在
        :meth:`purge_expired` 里随删行一起做。
        """
        asset = self.get_asset(asset_id)
        return self._mark_deleted([asset])

    def remove_many(self, asset_ids: List[int]) -> Tuple[int, int]:
        """批量删除；返回 ``(实际移进回收期的条数, 引用它们的镜头数)``。

        一次查询、一次提交：整组删除不该是 N 个请求，也不该删到一半停下。
        """
        ids = _int_ids(asset_ids)
        if not ids:
            return 0, 0
        rows = list(
            self._db.scalars(
                self._live(self._scope(select(MaterialAsset).where(MaterialAsset.id.in_(ids))))
            ).all()
        )
        return len(rows), self._mark_deleted(rows)

    def restore_many(self, asset_ids: List[int]) -> int:
        """撤销删除：把回收期里的素材恢复成可见，返回真正恢复的条数。

        超额时先放回更早删的那批，剩下的留在回收期等过期清理——恢复不能让账号
        超过条数上限。过了窗口的已经被 :meth:`purge_expired` 清掉，不在这里。
        """
        self.purge_expired()
        ids = _int_ids(asset_ids)
        if not ids:
            return 0
        rows = list(
            self._db.scalars(
                self._scope(select(MaterialAsset))
                .where(MaterialAsset.id.in_(ids), MaterialAsset.deleted_at.is_not(None))
                .order_by(MaterialAsset.deleted_at.asc(), MaterialAsset.id.asc())
            ).all()
        )
        if not rows:
            return 0
        live = int(
            self._db.scalar(
                select(func.count(MaterialAsset.id)).where(
                    MaterialAsset.owner_id == self._owner_id,
                    MaterialAsset.deleted_at.is_(None),
                )
            )
            or 0
        )
        budget = max(0, settings.MATERIAL_MAX_PER_OWNER - live)
        restored = rows[:budget]
        for row in restored:
            row.deleted_at = None
        self._db.commit()
        logger.info(
            "materials restored: owner=%s ids=%s restored=%s",
            self._owner_id,
            ids,
            len(restored),
        )
        return len(restored)

    def _mark_deleted(self, assets: List[MaterialAsset]) -> int:
        """统一盖上删除时间；返回受影响的镜头数（同一镜头引用多条只算一次）。"""
        if not assets:
            return 0
        now = db_now()
        wanted = {str(asset.id) for asset in assets}
        for asset in assets:
            asset.deleted_at = now
        references = 0
        rows = list(
            self._db.scalars(select(VideoShot).where(VideoShot.owner_id == self._owner_id)).all()
        )
        for row in rows:
            if {str(item) for item in (row.material_ids or [])} & wanted:
                references += 1
        self._db.commit()
        logger.info(
            "materials trashed: owner=%s ids=%s referencing_shots=%s",
            self._owner_id,
            sorted(wanted),
            references,
        )
        return references

    def purge_expired(self) -> int:
        """清掉过了撤销窗口的素材：删行、解引用、删文件，返回清掉的条数。

        懒清理，失败只记日志：一次清理不该把列表读挂掉。文件在事务提交之后再删——
        与其它删除同一分工，库里的事实先落定，磁盘慢一步没关系。
        """
        cutoff = db_now() - timedelta(seconds=self.trash_ttl_seconds)
        rel_paths: List[str] = []
        count = 0
        try:
            rows = list(
                self._db.scalars(
                    self._scope(select(MaterialAsset)).where(
                        MaterialAsset.deleted_at.is_not(None),
                        MaterialAsset.deleted_at < cutoff,
                    )
                ).all()
            )
            if not rows:
                return 0
            rel_paths = [
                str(asset.rel_path or "") for asset in rows if str(asset.rel_path or "")
            ]
            wanted = {str(asset.id) for asset in rows}
            for asset in rows:
                self._db.delete(asset)
            self._unbind_shots(wanted)
            self._db.commit()
            count = len(rows)
        except Exception:  # noqa: BLE001 - 清理失败不能影响读路径
            self._db.rollback()
            logger.exception("material purge failed: owner=%s", self._owner_id)
            return 0
        for rel_path in rel_paths:
            delete_blob(rel_path)
        logger.info("materials purged: owner=%s ids=%s", self._owner_id, count)
        return count

    def _unbind_shots(self, wanted: set) -> int:
        """把镜头引用里的这些 id 去掉（只在真删时调用）；返回改动过的镜头数。"""
        if not wanted:
            return 0
        unbound = 0
        rows = list(
            self._db.scalars(select(VideoShot).where(VideoShot.owner_id == self._owner_id)).all()
        )
        for row in rows:
            refs = [str(item) for item in (row.material_ids or [])]
            kept = [item for item in refs if item not in wanted]
            if len(kept) != len(refs):
                row.material_ids = kept
                unbound += 1
        return unbound

    # -- 引用影响 ---------------------------------------------------------------

    def usage(self, asset_ids: List[int]) -> List[MaterialUsageOut]:
        """每条素材被哪些短视频的哪些镜头引用，删除前给用户看影响面。

        与 :meth:`remove` 同一套反查口径（读一遍本账号的镜头行再按 id 过滤），
        结果按「项目新→旧、镜号小→大」排好，界面按项目聚合展示。
        """
        ids = _int_ids(asset_ids)
        if not ids:
            return []
        wanted = {str(asset_id) for asset_id in ids}
        rows = self._db.execute(
            select(VideoShot, VideoProject.title)
            .join(VideoProject, VideoProject.id == VideoShot.project_id)
            .where(
                VideoShot.owner_id == self._owner_id,
                VideoProject.owner_id == self._owner_id,
            )
            .order_by(
                VideoProject.created_at.desc(),
                VideoProject.id.desc(),
                VideoShot.sort_order.asc(),
            )
        ).all()

        # 按输入顺序建桶：同一镜头引用多条待删素材时只登记一次。
        buckets: Dict[str, Dict[int, dict]] = {str(asset_id): {} for asset_id in ids}
        for shot, title in rows:
            refs = {str(item) for item in (shot.material_ids or [])} & wanted
            for ref in refs:
                projects = buckets[ref]
                entry = projects.get(shot.project_id)
                if entry is None:
                    entry = {"project_id": shot.project_id, "title": title, "shots": []}
                    projects[shot.project_id] = entry
                entry["shots"].append(
                    MaterialUsageShot(id=shot.id, sort_order=shot.sort_order)
                )

        return [
            MaterialUsageOut(
                asset_id=int(asset_id),
                projects=[MaterialUsageProject(**entry) for entry in projects.values()],
            )
            for asset_id, projects in buckets.items()
        ]

    def ref_usage_map(self, asset_ids: List[int]) -> Dict[int, int]:
        """一批素材各自被多少个镜头引用，列表卡片的引用角标用。

        一次读全本账号镜头再 Python 聚合，与 :meth:`usage` / :meth:`_mark_deleted`
        同一反查口径；同一镜头引用多条只在各条上各算一次。
        """
        wanted = {str(item) for item in asset_ids}
        if not wanted:
            return {}
        counts: Dict[int, int] = {int(item): 0 for item in asset_ids}
        rows = list(
            self._db.scalars(select(VideoShot).where(VideoShot.owner_id == self._owner_id)).all()
        )
        for row in rows:
            refs = {str(item) for item in (row.material_ids or [])} & wanted
            for ref in refs:
                counts[int(ref)] += 1
        return counts

    # -- 供短视频侧批量读 -------------------------------------------------------

    def materials_for(self, asset_ids: List[str]) -> Dict[str, MaterialAsset]:
        """按 id 批量取本账号的素材，返回 ``{id 字符串: 行}``。

        首帧渲染入队时要知道每条素材是图还是视频：只有图片能进模型，视频留着
        等参考视频那条链路，所以这里把整行交出去而不是只给路径。

        这里**不过滤**回收期里的行：撤销窗口内文件还在，镜头引用也原样保留，
        短视频侧保存时把这些 id 当成「已知」才不会把引用提前抹掉；过期清理之后
        行才真的消失，那时引用本来就已经解开了。
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
