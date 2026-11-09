"""知识库分块表 kb_chunk + FTS5-trigram 虚表（文档 18 §三 v1.2，K1）。

- kb_chunk：唯一约束 (doc_id, section_idx)，BLOB 向量列（D2），provenance 按块冗余；
- kb_chunk_fts：FTS5 external content 虚表（trigram tokenizer）+ AFTER
  INSERT/DELETE/UPDATE 三触发器同步——原生 SQL 由 app/kb/fts.py 单源提供
  （v1.3 裁决⑦：K2 检索单测夹具与迁移共用同一 DDL，消复制漂移）；
- 方言守卫：FTS5/trigram 为 SQLite 专有，PG（生产目标库）跳过虚表与触发器只建表，
  PG 全文检索方案推迟立项（文档 18 v1.3 裁决⑥），切 PG 前另裁决。

Revision ID: 0005_add_kb_chunk
Revises: 0004_audit_log_attribution
Create Date: 2026-11-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.kb.fts import KB_FTS_TABLE_DDL, KB_FTS_TRIGGER_DDLS

revision = "0005_add_kb_chunk"
down_revision = "0004_audit_log_attribution"
branch_labels = None
depends_on = None

# 与 app/orm/base.py 的 TimestampTZ 保持一致。
TS_T = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "kb_chunk",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("doc_id", sa.String(16), nullable=False),
        sa.Column("section_idx", sa.Integer(), nullable=False),
        sa.Column("anchor", sa.String(256), nullable=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("embedding_model", sa.String(64), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_url", sa.String(512), nullable=False),
        sa.Column("column", sa.String(128), nullable=False),
        sa.Column("corpus_version", sa.String(32), nullable=False),
        sa.Column("fetched_at", TS_T, nullable=False),
        sa.Column("synced_at", TS_T, nullable=False),
        sa.UniqueConstraint("doc_id", "section_idx", name="uq_kb_chunk_doc_section"),
    )
    op.create_index("idx_kb_chunk_doc", "kb_chunk", ["doc_id"])

    # 方言守卫：SQLite 建 FTS5 虚表 + 同步触发器；PG 跳过（v1.3 裁决⑥：PG 方案推迟立项）。
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(KB_FTS_TABLE_DDL)
        for _, ddl in KB_FTS_TRIGGER_DDLS:
            op.execute(ddl)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for name, _ in KB_FTS_TRIGGER_DDLS:
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
        op.execute("DROP TABLE IF EXISTS kb_chunk_fts")
    op.drop_index("idx_kb_chunk_doc", table_name="kb_chunk")
    op.drop_table("kb_chunk")
