"""模型配置的存取，按单个用户隔离。

所有查询都要经过 :meth:`ModelConfigService._scope`，这样 handler 就不会
因为漏写一个过滤条件而误读到别人的配置。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.crypto import DecryptionError, decrypt, encrypt, is_masked, mask
from app.models.entities import ModelConfig
from app.models.schemas import (
    ModelConfigCreate,
    ModelConfigItem,
    ModelConfigUpdate,
    ModelPurpose,
)

logger = logging.getLogger("yangvis.model_config")


class ModelConfigService:
    """针对单个用户的模型配置做增删改查。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    # -- 内部方法 -----------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        """把查询限定在当前 owner 上。所有读操作都必须经过这里。"""
        return stmt.where(ModelConfig.owner_id == self._owner_id)

    def _get_or_none(self, config_id: int) -> Optional[ModelConfig]:
        return self._db.scalar(self._scope(select(ModelConfig).where(ModelConfig.id == config_id)))

    # -- 查询 ---------------------------------------------------------------

    def list(self, purpose: Optional[ModelPurpose] = None) -> List[ModelConfig]:
        stmt = self._scope(select(ModelConfig))
        if purpose is not None:
            stmt = stmt.where(ModelConfig.purpose == purpose.value)
        stmt = stmt.order_by(ModelConfig.is_default.desc(), ModelConfig.id.desc())
        return list(self._db.scalars(stmt).all())

    def get(self, config_id: int) -> ModelConfig:
        config = self._get_or_none(config_id)
        if config is None:
            # 返回 404 而不是 403：告诉调用方「这行记录存在，只是属于别人」，
            # 本身就是一点信息泄露。
            raise LookupError("模型配置不存在")
        return config

    def get_default(self, purpose: ModelPurpose) -> Optional[ModelConfig]:
        stmt = self._scope(select(ModelConfig)).where(
            ModelConfig.purpose == purpose.value,
            ModelConfig.is_default.is_(True),
        )
        return self._db.scalar(stmt)

    # -- 变更操作 -----------------------------------------------------------

    def create(self, payload: ModelConfigCreate) -> ModelConfig:
        config = ModelConfig(
            owner_id=self._owner_id,
            purpose=payload.purpose.value,
            title=payload.title,
            model_name=payload.model_name,
            base_url=payload.base_url.rstrip("/"),
            api_key_enc=encrypt(payload.api_key),
            remark=payload.remark,
            is_default=False,
        )
        self._db.add(config)
        self._db.flush()

        # 某一类下的第一份配置自动成为默认，这样新账号不用多点一次就能用。
        if self.get_default(payload.purpose) is None:
            config.is_default = True

        self._db.commit()
        self._db.refresh(config)
        return config

    def update(self, config_id: int, payload: ModelConfigUpdate) -> ModelConfig:
        config = self.get(config_id)
        data = payload.model_dump(exclude_unset=True)

        for field in ("title", "model_name", "remark"):
            if field in data and data[field] is not None:
                setattr(config, field, data[field])

        if data.get("purpose") is not None:
            config.purpose = data["purpose"].value

        credentials_changed = False
        if data.get("base_url"):
            new_url = data["base_url"].rstrip("/")
            credentials_changed = credentials_changed or new_url != config.base_url
            config.base_url = new_url

        # api_key 为空或者是打码后的值，都表示「别动它」。要是把掩码原样写
        # 回去，那么有人只改了个标题，一把能用的 key 就当场被抹掉了，而且
        # 直到下一次调用才会暴露出来。
        api_key = data.get("api_key")
        if api_key is not None and api_key != "" and not is_masked(api_key):
            config.api_key_enc = encrypt(api_key)
            credentials_changed = True

        if credentials_changed:
            # 之前那次测试通过，已经不能说明任何问题了。
            config.last_test_ok = False
            config.last_test_error = None
            config.vector_size = None
            if config.is_default:
                config.is_default = False
                logger.info("config %s lost default status after a credential change", config.id)

        self._db.commit()
        self._db.refresh(config)
        return config

    def delete(self, config_id: int) -> None:
        config = self.get(config_id)
        was_default = config.is_default
        purpose = ModelPurpose(config.purpose)
        self._db.delete(config)
        self._db.flush()

        # 顶上另一份验证过的配置，免得删掉之后这个功能悄无声息地不可用了。
        if was_default:
            replacement = self._db.scalar(
                self._scope(select(ModelConfig))
                .where(
                    ModelConfig.purpose == purpose.value,
                    ModelConfig.last_test_ok.is_(True),
                )
                .order_by(ModelConfig.id.desc())
            )
            if replacement is not None:
                replacement.is_default = True

        self._db.commit()

    def set_default(self, config_id: int) -> ModelConfig:
        config = self.get(config_id)

        # 要求先通过测试，就把「我的助手坏了」这种事，提前变成配置阶段
        # 一条明确的报错。
        if not config.last_test_ok:
            raise ValueError("请先通过连通性测试，再设为默认")

        for other in self.list(ModelPurpose(config.purpose)):
            other.is_default = other.id == config.id

        self._db.commit()
        self._db.refresh(config)
        return config

    def record_test_result(
        self,
        config: ModelConfig,
        *,
        ok: bool,
        message: str,
        vector_size: Optional[int] = None,
    ) -> None:
        config.last_tested_at = datetime.now(timezone.utc)
        config.last_test_ok = ok
        config.last_test_error = None if ok else message[:512]
        if vector_size is not None:
            config.vector_size = vector_size
        self._db.commit()

    # -- 序列化 -------------------------------------------------------------

    def to_item(self, config: ModelConfig) -> ModelConfigItem:
        """转成 API 返回的结构，同时把凭证打码。"""
        try:
            api_key = mask(decrypt(config.api_key_enc))
            key_error = None
        except DecryptionError as exc:
            api_key = ""
            key_error = str(exc)

        return ModelConfigItem(
            id=config.id,
            purpose=ModelPurpose(config.purpose),
            title=config.title,
            model_name=config.model_name,
            base_url=config.base_url,
            api_key=api_key,
            api_key_error=key_error,
            remark=config.remark,
            is_default=config.is_default,
            vector_size=config.vector_size,
            last_tested_at=config.last_tested_at,
            last_test_ok=config.last_test_ok,
            last_test_error=config.last_test_error,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )
