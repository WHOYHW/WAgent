"""ORM 基类与可移植列类型（C3）。

PostgreSQL 为目标库（规范 §4.5），开发/测试默认 SQLite。为保证同一套模型在
两库上可跑，使用 SQLAlchemy 可移植类型：
- ``JSONB``：PostgreSQL 上为 JSONB，其余退化为 JSON（SQLite 存 TEXT）。
- ``TimestampTZ``：PostgreSQL 上为 TIMESTAMPTZ，SQLite 存 DATETIME。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

# 可移植 JSONB：生产用原生 JSONB（含索引/约束），SQLite 测试降级为 JSON。
JSONBType: TypeEngine = JSON().with_variant(JSONB(), "postgresql")

# 可移植带时区时间戳。
TimestampTZ: TypeEngine = DateTime(timezone=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""

    pass


class IdMixin:
    """统一自增主键（PostgreSQL 上为 BIGSERIAL）。"""

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False)
