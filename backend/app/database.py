"""SQLAlchemy 数据库引擎与 session 辅助工具。"""
from __future__ import annotations

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    future=True,
)


class Base(DeclarativeBase):
    """ORM 模型的基类。"""


def get_db() -> Generator[Session, None, None]:
    """产出一个数据库 session 的 FastAPI 依赖。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
