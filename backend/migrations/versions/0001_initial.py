"""初始建表：reminder_subscription / reminder_dispatch / handoff_ticket / audit_log

对应文档 12 §4 与规范 §3.3（AC3.1 可执行、AC3.2 可回滚）。
JSONB/TIMESTAMPTZ 采用可移植写法：PostgreSQL 用原生类型，SQLite 降级为 JSON/DATETIME。

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# 可移植类型：与 app/orm/base.py 的 JSONBType / TimestampTZ 保持一致。
JSON_T = sa.JSON().with_variant(JSONB(), "postgresql")
TS_T = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "reminder_subscription",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("student_id", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(32), nullable=False),
        sa.Column("trigger_type", sa.String(16), nullable=False),
        sa.Column("trigger_config", JSON_T, nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("priority", sa.String(4), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS_T, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS_T, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_sub_student", "reminder_subscription", ["student_id", "enabled"])
    op.create_index("idx_sub_subject", "reminder_subscription", ["subject", "trigger_type"])

    op.create_table(
        "reminder_dispatch",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.String(20), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column("dedup_date", sa.Date(), nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("pushed_at", TS_T, nullable=True),
        sa.Column("opened_at", TS_T, nullable=True),
        sa.Column("action_taken", sa.String(32), nullable=True),
        sa.Column("is_degraded", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("dedup_key", "dedup_date", name="uq_dispatch_dedup"),
    )
    op.create_index("idx_dispatch_student", "reminder_dispatch", ["student_id", "pushed_at"])

    op.create_table(
        "handoff_ticket",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("student_id", sa.String(20), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("user_question", sa.Text(), nullable=False),
        sa.Column("intent_history", JSON_T, nullable=False),
        sa.Column("hit_rules", JSON_T, nullable=False),
        sa.Column("retrieved_facts", JSON_T, nullable=False),
        sa.Column("agent_response_log", sa.Text(), nullable=False),
        sa.Column("suggested_owner", sa.String(64), nullable=True),
        sa.Column("priority", sa.String(4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(64), nullable=True),
        sa.Column("first_response_at", TS_T, nullable=True),
        sa.Column("closed_at", TS_T, nullable=True),
        sa.Column("created_at", TS_T, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_ticket_sla", "handoff_ticket", ["status", "priority", "created_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("ts", TS_T, nullable=False, server_default=sa.func.now()),
        sa.Column("student_id", sa.String(20), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("tool_name", sa.String(48), nullable=True),
        sa.Column("args_digest", JSON_T, nullable=False),
        sa.Column("result", sa.String(16), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(24), nullable=True),
    )
    op.create_index("idx_audit_action", "audit_log", ["action", "ts"])


def downgrade() -> None:
    op.drop_index("idx_audit_action", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("idx_ticket_sla", table_name="handoff_ticket")
    op.drop_table("handoff_ticket")
    op.drop_index("idx_dispatch_student", table_name="reminder_dispatch")
    op.drop_table("reminder_dispatch")
    op.drop_index("idx_sub_student", table_name="reminder_subscription")
    op.drop_index("idx_sub_subject", table_name="reminder_subscription")
    op.drop_table("reminder_subscription")
