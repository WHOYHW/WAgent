"""reminder_dispatch 增加 claimed_at 认领时间锚（任务设计 09 §D3，v1.4）。

CAS 认领自愈的比较锚：实例在 claim 后崩溃留下 status='claimed' 孤儿行，
任一实例对 claimed 超阈值者执行 ``SET status='claimed', claimed_at=now
WHERE status='claimed' AND claimed_at < :threshold``。锚必须**可刷新**——
续发认领时前移比较基准，否则另一实例下一 tick 能立刻重复认领同一行
（双发窗口）；故不采用不可变的 created_at 当锚。nullable：历史行（迁移前）
与终态行缺省，新 claim 行在占坑时即写入。

Revision ID: 0003_add_dispatch_claimed_at
Revises: 0002_add_conversation_tables
Create Date: 2026-10-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_add_dispatch_claimed_at"
down_revision = "0002_add_conversation_tables"
branch_labels = None
depends_on = None

# 与 app/orm/base.py 的 TimestampTZ 保持一致。
TS_T = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("reminder_dispatch", sa.Column("claimed_at", TS_T, nullable=True))


def downgrade() -> None:
    op.drop_column("reminder_dispatch", "claimed_at")
