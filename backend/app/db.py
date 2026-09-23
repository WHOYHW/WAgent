"""异步数据库引擎与会话（C3）。

- 生产：PostgreSQL（asyncpg）。
- 开发/测试：SQLite（aiosqlite），内存库测试用 ``StaticPool``。
- ``make_engine`` 暴露给测试按需构建隔离引擎。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from . import orm  # noqa: F401  确保模型在 create_all 前完成注册
from .config import settings
from .orm import Base


def make_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    """构建异步引擎。测试可传 ``sqlite+aiosqlite:///:memory:`` + StaticPool。"""
    target = url or settings.db_url
    if target.startswith("sqlite") and ":memory:" in target:
        from sqlalchemy.pool import StaticPool

        kwargs.setdefault("poolclass", StaticPool)
        kwargs.setdefault("connect_args", {"check_same_thread": False})
    return create_async_engine(target, **kwargs)


engine: AsyncEngine = make_engine()
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """建表（开发/测试用；生产走 Alembic 迁移）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：按请求提供会话。"""
    async with async_session_factory() as session:
        yield session
