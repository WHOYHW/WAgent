"""Alembic 迁移环境（C3）。

连接串来自 ``app.config.settings.db_url_sync``（同步引擎，迁移不跑异步）；
模型注册自 ``app.orm``，保证 autogenerate 与手写迁移均以 ORM 为唯一事实来源。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 将 backend 目录加入 sys.path，使 `import app` 生效（alembic 从 backend/ 运行）。
sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import orm  # noqa: E402,F401  确保模型全部注册
from app.config import settings  # noqa: E402
from app.orm import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 迁移连接串以配置为中心：DB_URL_SYNC 环境变量 > alembic.ini 占位。
config.set_main_option("sqlalchemy.url", settings.db_url_sync)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL，不连库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),  # SQLite 支持批量 DDL 变更
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直连执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        url = str(connection.engine.url)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
