"""启动检查与一次性的初始化工作。"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet
from sqlalchemy import func, inspect, select, text

from app.config import Settings
from app.database import Base, SessionLocal, engine
from app.models.entities import KbProject, KnowledgeType, SysUser
from app.services.agents import seed_builtin_agents
from app.services.knowledge import reset_stale_indexing
from app.services.ops_actions import reset_stale_ops_actions
from app.services.resume import reset_stale_analysis
from app.security import hash_password

logger = logging.getLogger("yangvis.bootstrap")

# HS256 见 RFC 7518 §3.2；低于该长度 PyJWT 会告警，而这个告警是有道理的。
MIN_SECRET_LENGTH = 32

# 与修改密码相关 schema 中强制的下限保持一致。
MIN_PASSWORD_LENGTH = 8


class ConfigurationError(RuntimeError):
    """进程配置有误，不得对外提供服务。"""


def validate_settings(settings: Settings) -> None:
    """认证配置不安全时拒绝启动。

    默认的签名密钥会让任何人都能签发管理员 token，而一个「能跑起来」的默认值，
    恰恰是最容易一路留到生产环境的那类东西。所以宁可大声地失败。
    """
    secret = settings.AUTH_JWT_SECRET
    if not secret:
        raise ConfigurationError(
            "AUTH_JWT_SECRET is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` "
            "and set it in the environment."
        )
    if len(secret.encode("utf-8")) < MIN_SECRET_LENGTH:
        raise ConfigurationError(
            f"AUTH_JWT_SECRET is too short ({len(secret)} chars); "
            f"use at least {MIN_SECRET_LENGTH} bytes."
        )
    if settings.AUTH_TOKEN_TTL <= 0:
        raise ConfigurationError("AUTH_TOKEN_TTL must be a positive number of seconds.")

    # 与 JWT secret 相同的理由，加上一点它自己的：轮换这个密钥会让所有已存储的
    # api_key 无法解密，因此必须由用户主动设置，而不是靠默认值凭空产生。
    if not settings.ENCRYPTION_KEY:
        raise ConfigurationError(
            "ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and set it in the environment."
        )
    try:
        Fernet(settings.ENCRYPTION_KEY.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            "ENCRYPTION_KEY is not a valid Fernet key (expected 32 url-safe base64 bytes)."
        ) from exc

    # 放在这里检查而不是放在 bootstrap_admin() 里，是为了让有问题的取值在任何表
    # 被创建之前就中止进程。中途失败会留下一个半初始化的数据库，而它看上去就像
    # 应用已经成功跑起来一样。
    password = settings.BOOTSTRAP_ADMIN_PASSWORD
    if password and len(password) < MIN_PASSWORD_LENGTH:
        raise ConfigurationError(
            f"BOOTSTRAP_ADMIN_PASSWORD is too short "
            f"({len(password)} chars); use at least {MIN_PASSWORD_LENGTH}."
        )


def create_tables() -> None:
    """创建所有缺失的表。

    在 schema 只增不删的阶段够用了。任何涉及改写已有列的操作都需要真正的迁移；
    ``docs/schema.sql`` 是这份 DDL 的可审阅版本。
    """
    Base.metadata.create_all(bind=engine)
    logger.info("database schema verified")


# 在「项目」这个概念出现之前创建的知识类型最终会落到这里。
DEFAULT_PROJECT_NAME = "默认项目"


def _column_names(table: str) -> set[str]:
    """返回 ``table`` 的列名集合；表不存在时返回空集合。"""
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def migrate_schema() -> None:
    """补上 ``create_all`` 无法添加的列，然后回填数据。

    ``create_all`` 只会创建缺失的*表*；已存在的表上新增一列，它是看不见的。
    这些 ALTER 语句被写成可以在每次启动时安全重复执行：每一条都先查询当前的列做
    判断，回填也只会动那些仍然保留占位值的行。
    """
    type_columns = _column_names("knowledge_type")
    if type_columns and "project_id" not in type_columns:
        # 用 DEFAULT 0 是为了让 ALTER 在已有数据的表上也能成功；下面的回填会把
        # 每一个 0 替换成真实的项目。
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE knowledge_type "
                    "ADD COLUMN project_id BIGINT NOT NULL DEFAULT 0"
                )
            )
        logger.info("added knowledge_type.project_id")

    conversation_columns = _column_names("chat_conversation")
    if conversation_columns:
        for column, ddl in (
            ("project_id", "ADD COLUMN project_id BIGINT NULL"),
            ("type_ids", "ADD COLUMN type_ids JSON NULL"),
            ("model_config_id", "ADD COLUMN model_config_id BIGINT NULL"),
            ("ops_target_type", "ADD COLUMN ops_target_type VARCHAR(16) NULL"),
            ("ops_target_id", "ADD COLUMN ops_target_id BIGINT NULL"),
        ):
            if column in conversation_columns:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE chat_conversation {ddl}"))
            logger.info("added chat_conversation.%s", column)

    database_columns = _column_names("ops_database")
    if database_columns:
        for column, ddl in (
            ("writable", "ADD COLUMN writable TINYINT(1) NOT NULL DEFAULT 0"),
            ("color", "ADD COLUMN color VARCHAR(16) NULL"),
        ):
            if column in database_columns:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE ops_database {ddl}"))
            logger.info("added ops_database.%s", column)

    user_columns = _column_names("sys_user")
    if user_columns and "ops_write" not in user_columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE sys_user "
                    "ADD COLUMN ops_write TINYINT(1) NOT NULL DEFAULT 0"
                )
            )
            # grandfather：存量用户在旧模型下本来就什么都能做，全部置 1 保持
            # 现状；新建账号由 users.py 显式赋值。回填与 ALTER 同分支，只在
            # 列刚新增的这一次启动里执行，天然幂等。
            conn.execute(text("UPDATE sys_user SET ops_write = 1"))
        logger.info("added sys_user.ops_write and backfilled existing users")

    audit_columns = _column_names("ops_audit_log")
    if audit_columns and "pending_action_id" not in audit_columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE ops_audit_log ADD COLUMN pending_action_id BIGINT NULL")
            )
        logger.info("added ops_audit_log.pending_action_id")

    agent_columns = _column_names("agent")
    if agent_columns:
        for column, ddl in (
            ("use_ops", "ADD COLUMN use_ops TINYINT(1) NOT NULL DEFAULT 0"),
            ("chat_visible", "ADD COLUMN chat_visible TINYINT(1) NOT NULL DEFAULT 1"),
        ):
            if column in agent_columns:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE agent {ddl}"))
            logger.info("added agent.%s", column)

    resume_columns = _column_names("resume")
    if resume_columns:
        for column, ddl in (
            ("netdisk_fs_id", "ADD COLUMN netdisk_fs_id BIGINT NULL"),
            ("netdisk_path", "ADD COLUMN netdisk_path VARCHAR(512) NULL"),
        ):
            if column in resume_columns:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE resume {ddl}"))
            logger.info("added resume.%s", column)

    _backfill_default_projects()


def _backfill_default_projects() -> None:
    """给每一个没有归属的知识类型找一个项目安放。

    按 owner 各建一个项目，而不是共用一个：项目就是检索边界，而一个跨用户的边界
    正好会造成其他地方处处按 owner 收敛所要防止的那种泄漏。
    """
    with SessionLocal() as db:
        owner_ids = db.scalars(
            select(KnowledgeType.owner_id)
            .where(KnowledgeType.project_id == 0)
            .distinct()
        ).all()
        if not owner_ids:
            return

        for owner_id in owner_ids:
            project = db.scalar(
                select(KbProject).where(
                    KbProject.owner_id == owner_id,
                    KbProject.name == DEFAULT_PROJECT_NAME,
                )
            )
            if project is None:
                project = KbProject(
                    owner_id=owner_id,
                    name=DEFAULT_PROJECT_NAME,
                    description="迁移时自动创建，用于存放已有的知识类型",
                )
                db.add(project)
                db.flush()

            db.query(KnowledgeType).filter(
                KnowledgeType.owner_id == owner_id,
                KnowledgeType.project_id == 0,
            ).update({KnowledgeType.project_id: project.id})

        db.commit()
        logger.info("backfilled knowledge types into default projects for %d owner(s)", len(owner_ids))


def seed_agents() -> None:
    """安装内置智能体（幂等）。"""
    with SessionLocal() as db:
        seed_builtin_agents(db)


def reset_stale_jobs() -> None:
    """把上一个进程中途遗留下来的任务收尾。

    后台任务与待确认项都活在 Gunicorn worker 内部，因此一次重启会悄悄把它们
    丢掉：文档永远停在 ``indexing`` 状态，待确认命令永远等不到回答。在启动时
    把这些情况转成明确的终态，至少能让它们暴露出来并且可以重试。
    """
    with SessionLocal() as db:
        reset_stale_indexing(db)
        reset_stale_analysis(db)
        # 进程重启同样会丢下「等用户确认」的运维写命令——发起它们的 SSE 流
        # 已经断了，统一物化为 expired。
        reset_stale_ops_actions(db)


def bootstrap_admin(settings: Settings) -> None:
    """在 ``sys_user`` 为空时创建第一个管理员。

    除非两项凭据都已配置，否则跳过；这里故意不留任何兜底账号，因为一个内置的
    默认密码和没有密码是一回事。
    """
    username = settings.BOOTSTRAP_ADMIN_USERNAME.strip()
    password = settings.BOOTSTRAP_ADMIN_PASSWORD

    with SessionLocal() as db:
        user_count = db.scalar(select(func.count()).select_from(SysUser)) or 0
        if user_count:
            return

        if not username or not password:
            logger.warning(
                "sys_user is empty and BOOTSTRAP_ADMIN_USERNAME / "
                "BOOTSTRAP_ADMIN_PASSWORD are not set - nobody can log in. "
                "Set both and restart to create the first administrator."
            )
            return

        db.add(
            SysUser(
                username=username,
                password_hash=hash_password(password),
                nickname=username,
                status=True,
                is_admin=True,
            )
        )
        db.commit()
        logger.info("bootstrapped initial administrator %r", username)
