"""audit_log 增加归因维度 prompt_version / llm_model（任务 17 前置）。

"任意对话可归因"闭环的最后一块：trace_id 定位消息轨迹，prompt_version ×
llm_model 定位"哪版提示词 × 哪个模型"产生了这轮行为，工具明细由
tools/base.py 各自留痕。评测（任务 17 黄金集）按这两列切分归因，分数
不再混版本。nullable：历史行无对应维度，缺省即可；SQLite 加列为轻操作。

Revision ID: 0004_audit_log_attribution
Revises: 0003_add_dispatch_claimed_at
Create Date: 2026-10-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_audit_log_attribution"
down_revision = "0003_add_dispatch_claimed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("prompt_version", sa.String(32), nullable=True))
    op.add_column("audit_log", sa.Column("llm_model", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "llm_model")
    op.drop_column("audit_log", "prompt_version")
