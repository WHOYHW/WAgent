"""FTS5 虚表 DDL 单一事实源（文档 18 §三 v1.3 裁决⑦）。

迁移 0005 与 K2 检索单测夹具共用同一常量——K2 单测的内存库不走 alembic
（``Base.metadata.create_all`` 建不出虚表），若夹具内联 DDL 会与迁移复制漂移。

纯常量零 IO：本模块被 migrations 导入，禁止引入第三方重依赖。
"""

from __future__ import annotations

#: external content 模式：rowid 对齐 kb_chunk.id，trigram 对 CJK 友好（D3）
KB_FTS_TABLE_DDL = (
    "CREATE VIRTUAL TABLE kb_chunk_fts USING fts5("
    "title, content, content='kb_chunk', content_rowid='id', tokenize='trigram')"
)

#: external content 模式的标准三触发器（FTS5 官方文档 §8.2.1）：
#: INSERT 直写、UPDATE/DELETE 先以 'delete' 命令删除旧索引行再写新值。
KB_FTS_TRIGGER_DDLS: tuple[tuple[str, str], ...] = (
    ("kb_chunk_fts_ai", """
        CREATE TRIGGER kb_chunk_fts_ai AFTER INSERT ON kb_chunk BEGIN
            INSERT INTO kb_chunk_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END
    """),
    ("kb_chunk_fts_ad", """
        CREATE TRIGGER kb_chunk_fts_ad AFTER DELETE ON kb_chunk BEGIN
            INSERT INTO kb_chunk_fts(kb_chunk_fts, rowid, title, content)
            VALUES ('delete', old.id, old.title, old.content);
        END
    """),
    ("kb_chunk_fts_au", """
        CREATE TRIGGER kb_chunk_fts_au AFTER UPDATE ON kb_chunk BEGIN
            INSERT INTO kb_chunk_fts(kb_chunk_fts, rowid, title, content)
            VALUES ('delete', old.id, old.title, old.content);
            INSERT INTO kb_chunk_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END
    """),
)

__all__ = ["KB_FTS_TABLE_DDL", "KB_FTS_TRIGGER_DDLS"]
