"""SQLAlchemy 数据库引擎与 session 辅助工具。

引擎是同步的，所以 handler 的写法有讲究：只碰数据库的接口写成 ``def`` 而不是
``async def``——FastAPI 会把同步 handler 丢进线程池，一条慢查询从此只占一个线程；
写成 ``async def`` 的话每次 pymysql 往返都冻结事件循环，而生产只有 1 个 worker，
正在推的 SSE（问答、工作流进度、运维终端）会跟着一起停。

``async def`` 的 handler（要 await 流式响应的那种）里不能做本地 CPU/磁盘活：
PDF 解析、写文件、密码哈希这类要走 ``asyncio.to_thread``。
"""
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
