"""迁移 roundtrip 单测（文档 18 §三 v1.2，AC-18a 迁移部分，零网络）。

alembic command API 驱动临时 SQLite 文件库全链路：
upgrade head → 插行 → 触发器同步验证（FTS MATCH）→ downgrade 0004 → 再 upgrade。

连接串经 monkeypatch settings.db_url_sync 注入（env.py 每次命令执行时读取），
不触碰真实 campus_agent.db。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import config as config_module
from app.config import Settings
from app.orm import KbChunk

BACKEND_DIR = Path(__file__).resolve().parents[1]
PREV_REVISION = "0004_audit_log_attribution"


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    # cwd 无关：script_location 必须绝对路径
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg


def _table_names(conn) -> set[str]:
    rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    return {r[0] for r in rows}


def test_kb_chunk_migration_roundtrip(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "mig.db").as_posix()
    # 以全新 Settings 实例替换模块级单例（而非改旧对象属性）：test_api_chat 的
    # importlib.reload 会重建 app.config.settings，env.py 命令期读到的必须是当前模块属性
    monkeypatch.setattr(config_module, "settings", Settings(db_url_sync=url))
    cfg = _alembic_config()

    # ---- upgrade head：kb_chunk 表 + FTS5 虚表 + 三触发器 ----
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        tables = _table_names(conn)
    assert "kb_chunk" in tables and "kb_chunk_fts" in tables

    # ---- 插行：触发器同步进 FTS 索引（external content 模式）----
    now = datetime.now(UTC)
    maker = sessionmaker(bind=engine)
    with maker() as session:
        session.add(
            KbChunk(
                doc_id="a" * 16,
                section_idx=0,
                anchor="第一条",
                title="转专业实施办法",
                content="第一条 转专业条件：平均学分绩点排名专业前百分之二十。",
                content_hash="0" * 64,
                source_url="https://jwc.hnist.cn/gzzd/xjgl.htm",
                column="教务处/规章制度",
                corpus_version="2026-11-05.1",
                fetched_at=now,
                synced_at=now,
            )
        )
        session.commit()

    with engine.connect() as conn:
        hits = conn.execute(
            text("SELECT rowid FROM kb_chunk_fts WHERE kb_chunk_fts MATCH '转专业'")
        ).fetchall()
    assert hits, "INSERT 触发器未同步 FTS 索引"

    # ---- downgrade：表 + 虚表 + 触发器全部清除 ----
    command.downgrade(cfg, PREV_REVISION)
    with engine.connect() as conn:
        tables = _table_names(conn)
    assert "kb_chunk" not in tables and "kb_chunk_fts" not in tables

    # ---- 再 upgrade：roundtrip 收尾（重复执行迁移幂等可重放）----
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        tables = _table_names(conn)
    assert "kb_chunk" in tables and "kb_chunk_fts" in tables
    engine.dispose()
