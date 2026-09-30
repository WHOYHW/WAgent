"""新增会话与消息表（任务设计 W1.4 + 文档 12 §4 短期记忆）。

- 对话会话表 conversation_session：唯一 session_id，按 (student_id, status) 建索引便于查询；
- 消息表 message：会话内 seq 唯一约束，is_summary 标记压缩产物，deleted_at 软删；
- JSONB / TIMESTAMPTZ 采用可移植写法：PG 用原生类型，SQLite 降级为 JSON/DATETIME。

Revision ID: 0002_add_conversation_tables
Revises: 0001_initial
Create Date: 2026-09-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_add_conversation_tables"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

# 与 app/orm/base.py 的 JSONBType / TimestampTZ 保持一致。
JSON_T = sa.JSON().with_variant(JSONB(), "postgresql")
TS_T = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "conversation_session",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False, unique=True),
        sa.Column("student_id", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(24), nullable=False, server_default="wecom"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("summary_message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", TS_T, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS_T, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TS_T, nullable=False),
    )
    op.create_index(
        "idx_session_student_status", "conversation_session", ["student_id", "status"]
    )
    op.create_index("idx_session_expires", "conversation_session", ["expires_at"])

    op.create_table(
        "message",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls", JSON_T, nullable=True),
        sa.Column("tool_call_id", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(32), nullable=True),
        sa.Column("is_summary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", TS_T, nullable=True),
        sa.Column("created_at", TS_T, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("session_id", "seq", name="uq_msg_session_seq"),
    )
    op.create_index("idx_msg_session_seq", "message", ["session_id", "seq"])
    op.create_index("idx_msg_session_role", "message", ["session_id", "role"])


def downgrade() -> None:
    op.drop_index("idx_msg_session_role", table_name="message")
    op.drop_index("idx_msg_session_seq", table_name="message")
    op.drop_table("message")
    op.drop_index("idx_session_expires", table_name="conversation_session")
    op.drop_index("idx_session_student_status", table_name="conversation_session")
    op.drop_table("conversation_session")
