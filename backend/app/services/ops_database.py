"""运维数据库的台账与 SQL 收藏。

这里只管台账表 ``ops_database`` 和收藏表 ``ops_sql_favorite`` 的增删改查：
连接信息里的密码以 Fernet 加密落库，出参一律走 ``mask()``，密文与明文都不出场。
执行通道、结构浏览、数据浏览、设计表、Redis 键操作都在
:mod:`app.services.ops_db_exec` 等兄弟模块里，本模块不碰任何网络连接。

两个 ``OpsDb*`` 异常是全运维数据库这一层的共同语言，所以放这里而不是执行模块：
台账层报「连接不存在 / 没有写权限」，与各执行模块报的失败，路由侧用同一套映射处理。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto import (
    DecryptionError,
    decrypt,
    encrypt,
    is_masked,
    mask,
)
from app.models.entities import OpsDatabase, OpsSqlFavorite
from app.models.schemas import (
    DatabaseType,
    OpsDatabaseCreate,
    OpsDatabaseItem,
    OpsDatabaseUpdate,
    SqlFavoriteCreate,
    SqlFavoriteUpdate,
)
from app.timeutil import db_now

logger = logging.getLogger("yangvis.ops_database")


class OpsDbError(RuntimeError):
    """连接或执行数据库命令失败。"""


class OpsDbForbidden(RuntimeError):
    """命令被安全策略挡下。与执行失败区分开，前端要给不同的提示。"""


class OpsDatabaseService:
    """数据库台账。台账全员共用：``owner_id`` 只记录登记人/审计归属，
    不再作为可见性过滤；增删改由路由层限定管理员。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    def _scope(self, stmt: Select) -> Select:
        # 台账全局化后不再按 owner 过滤；保留这个方法只是为了让各查询点的
        # 结构不变。
        return stmt

    def _require_unique_name(self, name: str, *, exclude_id: Optional[int] = None) -> None:
        """台账共用后名称全局唯一。DB 层仍只有 (owner_id, name) 复合索引，
        全局唯一靠这层预查强制（存量历史重名不强刷，只挡新增/改名）。"""
        stmt = select(OpsDatabase.id).where(OpsDatabase.name == name)
        if exclude_id is not None:
            stmt = stmt.where(OpsDatabase.id != exclude_id)
        if self._db.scalar(stmt) is not None:
            raise ValueError(f"数据库名称「{name}」已存在")

    # -- 查询 ---------------------------------------------------------------

    def list(
        self, keyword: Optional[str] = None, db_type: Optional[DatabaseType] = None
    ) -> List[OpsDatabase]:
        stmt = self._scope(select(OpsDatabase))
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(OpsDatabase.name.like(like) | OpsDatabase.host.like(like))
        if db_type is not None:
            stmt = stmt.where(OpsDatabase.db_type == db_type.value)
        return list(self._db.scalars(stmt.order_by(OpsDatabase.id.desc())).all())

    def get(self, database_id: int) -> OpsDatabase:
        item = self._db.scalar(self._scope(select(OpsDatabase).where(OpsDatabase.id == database_id)))
        if item is None:
            raise LookupError("数据库不存在")
        return item

    # -- 变更操作 -----------------------------------------------------------

    def create(self, payload: OpsDatabaseCreate) -> OpsDatabase:
        self._require_unique_name(payload.name.strip())

        item = OpsDatabase(
            owner_id=self._owner_id,
            name=payload.name.strip(),
            db_type=payload.db_type.value,
            host=payload.host.strip(),
            port=payload.port,
            username=(payload.username or "").strip() or None,
            password_enc=encrypt(payload.password or ""),
            db_name=(payload.db_name or "").strip() or None,
            writable=payload.writable,
            color=payload.color,
            remark=payload.remark,
        )
        self._db.add(item)
        self._commit_unique(f"数据库名称「{item.name}」已存在")
        self._db.refresh(item)
        logger.info(
            "owner %s created database %s (%s %s:%s, writable=%s)",
            self._owner_id,
            item.id,
            item.db_type,
            item.host,
            item.port,
            item.writable,
        )
        return item

    def update(self, database_id: int, payload: OpsDatabaseUpdate) -> OpsDatabase:
        item = self.get(database_id)
        data = payload.model_dump(exclude_unset=True)

        for field in ("name", "host", "username", "db_name", "remark"):
            value = data.get(field)
            if value is not None:
                setattr(item, field, value.strip() if isinstance(value, str) else value)

        if data.get("name") is not None:
            self._require_unique_name(item.name, exclude_id=item.id)

        if data.get("port") is not None:
            item.port = data["port"]
        if data.get("db_type") is not None:
            item.db_type = data["db_type"].value
        if data.get("writable") is not None:
            item.writable = data["writable"]
        # 颜色要能改回「无」，None 是合法值，不走上面 None 即跳过的通用循环。
        if "color" in data:
            item.color = data["color"] or None

        password = data.get("password")
        password_changed = bool(password and not is_masked(password))
        if password_changed:
            item.password_enc = encrypt(password)

        # 只有连接信息（地址/凭据/库名/类型）变化才让上次探测结果作废——
        # 改备注、颜色、写开关不影响连通性，与 ops_server.update 保持一致。
        endpoint_changed = any(
            data.get(f) is not None for f in ("host", "port", "username", "db_name", "db_type")
        )
        if endpoint_changed or password_changed:
            item.last_check_ok = False
            item.last_check_error = None

        self._commit_unique(f"数据库名称「{item.name}」已存在")
        self._db.refresh(item)
        logger.info(
            "owner %s updated database %s (writable_toggled=%s, password_changed=%s)",
            self._owner_id,
            item.id,
            data.get("writable") is not None,
            password_changed,
        )
        return item

    def delete(self, database_id: int) -> None:
        item = self.get(database_id)
        self._db.delete(item)
        self._db.commit()
        logger.info("owner %s deleted database %s (%s)", self._owner_id, item.id, item.name)

    def record_check(self, item: OpsDatabase, *, ok: bool, message: str = "") -> None:
        item.last_checked_at = db_now()
        item.last_check_ok = ok
        item.last_check_error = None if ok else message[:512]
        self._db.commit()

    # -- 序列化 -------------------------------------------------------------

    def to_item(self, item: OpsDatabase) -> OpsDatabaseItem:
        try:
            secret = decrypt(item.password_enc)
            password = mask(secret) if secret else ""
            error = None
        except DecryptionError as exc:
            # 同 ops_server：解密失败意味着密钥轮换或数据损坏，必须留痕。
            logger.warning("failed to decrypt the password of database %s", item.id)
            password = ""
            error = str(exc)

        return OpsDatabaseItem(
            id=item.id,
            name=item.name,
            db_type=DatabaseType(item.db_type),
            host=item.host,
            port=item.port,
            username=item.username,
            password=password,
            password_error=error,
            db_name=item.db_name,
            writable=item.writable,
            color=item.color,
            remark=item.remark,
            last_checked_at=item.last_checked_at,
            last_check_ok=item.last_check_ok,
            last_check_error=item.last_check_error,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def _commit_unique(self, message: str) -> None:
        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            raise ValueError(message) from exc


class OpsSqlFavoriteService:
    """单个用户的 SQL 收藏夹。

    「通用收藏」（``database_id`` 为空）和绑定某个连接的收藏存在同一张表里；
    查询页的列表 = 当前连接的收藏 + 通用收藏。
    """

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(OpsSqlFavorite.owner_id == self._owner_id)

    def list(self, database_id: Optional[int] = None) -> List[OpsSqlFavorite]:
        stmt = self._scope(select(OpsSqlFavorite))
        if database_id is not None:
            stmt = stmt.where(
                (OpsSqlFavorite.database_id == database_id)
                | (OpsSqlFavorite.database_id.is_(None))
            )
        # 绑定连接的收藏排在通用收藏前面；MySQL 不认 NULLS LAST，用 IS NULL 代替。
        stmt = stmt.order_by(OpsSqlFavorite.database_id.is_(None), OpsSqlFavorite.id.desc())
        return list(self._db.scalars(stmt).all())

    def get(self, favorite_id: int) -> OpsSqlFavorite:
        item = self._db.scalar(
            self._scope(select(OpsSqlFavorite).where(OpsSqlFavorite.id == favorite_id))
        )
        if item is None:
            raise LookupError("收藏不存在")
        return item

    def create(self, payload: SqlFavoriteCreate) -> OpsSqlFavorite:
        item = OpsSqlFavorite(
            owner_id=self._owner_id,
            database_id=self._check_database(payload.database_id),
            title=payload.title.strip(),
            content=payload.content.strip(),
            remark=payload.remark,
        )
        self._db.add(item)
        self._db.commit()
        self._db.refresh(item)
        return item

    def update(self, favorite_id: int, payload: SqlFavoriteUpdate) -> OpsSqlFavorite:
        item = self.get(favorite_id)
        data = payload.model_dump(exclude_unset=True)

        if data.get("title") is not None:
            item.title = data["title"].strip()
        if data.get("content") is not None:
            item.content = data["content"].strip()
        if "remark" in data:
            item.remark = data["remark"]
        if "database_id" in data:
            item.database_id = self._check_database(data["database_id"])

        self._db.commit()
        self._db.refresh(item)
        return item

    def delete(self, favorite_id: int) -> None:
        item = self.get(favorite_id)
        self._db.delete(item)
        self._db.commit()

    def _check_database(self, database_id: Optional[int]) -> Optional[int]:
        """绑定连接时校验它属于当前用户；None（通用收藏）直接放行。"""
        if database_id is None:
            return None
        OpsDatabaseService(self._db, self._owner_id).get(database_id)
        return database_id
