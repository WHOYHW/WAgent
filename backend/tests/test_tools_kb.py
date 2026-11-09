"""K4 kb_search 工具单测（文档 18 §五 v1.5，AC-18c，零网络）。

覆盖：门控注册（关=不注册 / 开=注册且 Services 懒装配检索内核）、
default_registry 全量词表、工具执行回路（预灌语料命中非空 / 空库诚实空）、
二次钳制（top_k / 单块长度）、检索异常走 C4 模板失败通道。
编排层完整回路（HTTP→编排→工具→落库）由黄金集 kb 类用例覆盖（18 附录 B）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import pytest_asyncio
from conftest import Services, StubEduAdapter, StubLibAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cache import TTLCache
from app.config import Settings
from app.db import make_engine
from app.kb import (
    KB_FTS_TABLE_DDL,
    KB_FTS_TRIGGER_DDLS,
    FakeEmbedder,
    HybridRetriever,
    KbSearchParams,
    make_doc_id,
)
from app.orm import Base, KbChunk
from app.tools.base import ToolContext, ToolError
from app.tools.kb import KnowledgeSearchTool
from app.tools.registry import build_registry, default_registry

MODEL = "doubao-embedding"  # 与 Settings 默认 kb_embedding_model 对齐
CLAUSE = (
    "第一条 转专业条件：平均学分绩点排名专业前百分之二十，"
    "转专业申请于每学期第二周提交。"
)
URL = "https://jwc.hnist.cn/gzzd/xjgl.htm"


def make_kb_services(db, *, enabled: bool, retriever=None, **overrides) -> Services:
    """kb 场景隔离 Services：门控开关注入 + 可选检索内核桩。"""
    return Services(
        cache=TTLCache(),
        settings=Settings(kb_enabled=enabled, **overrides),
        edu=StubEduAdapter(),
        lib=StubLibAdapter(),
        kb_retriever=retriever,
        db=db,
        idempotency={},
        renew_session_count={},
    )


@pytest_asyncio.fixture
async def kb_db():
    """内存库 + FTS 虚表（单源 DDL）。"""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(KB_FTS_TABLE_DDL))
        for _, ddl in KB_FTS_TRIGGER_DDLS:
            await conn.execute(text(ddl))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _add_clause(session, *, content: str = CLAUSE, synced_at=None, anchor: str | None = None) -> None:
    (vec,) = await FakeEmbedder().embed_texts([content])
    now = synced_at or datetime.now(UTC)
    session.add(
        KbChunk(
            doc_id=make_doc_id(URL),
            section_idx=0,
            anchor=anchor,
            title="转专业实施办法",
            content=content,
            embedding=np.asarray(vec, dtype=np.float32).tobytes(),
            embedding_model=MODEL,
            content_hash="0" * 64,
            source_url=URL,
            column="教务处/规章制度",
            corpus_version="2026-11-05.1",
            fetched_at=now,
            synced_at=now,
        )
    )
    await session.commit()


# ---- 门控注册（D5 / AC-18c）----


def test_registry_gate_off_excludes_kb_search(db):
    """kb_enabled=False：kb_search 不进注册表（LLM 工具列表不出现）。"""
    registry = build_registry(make_kb_services(db, enabled=False))
    assert "kb_search" not in registry.spec_names()


def test_registry_gate_on_registers_kb_search(db):
    """kb_enabled=True：kb_search 注册，且 Services 懒装配检索内核（零网络）。"""
    services = make_kb_services(db, enabled=True)
    registry = build_registry(services)
    assert "kb_search" in registry.spec_names()
    assert services.kb_retriever is not None  # 懒装配（构造零副作用，未触网）


def test_default_registry_is_full_wordlist():
    """default_registry = 全量词表（黄金集受控词表用途），恒含 kb_search。"""
    assert "kb_search" in default_registry.spec_names()
    # 导出 Schema 不含 student_id（kb 语料公开无个人数据，约定③从根源不适用）
    spec = next(s for s in default_registry.specs() if s.name == "kb_search")
    assert "student_id" not in spec.parameters.get("properties", {})
    assert not spec.write and spec.boundary == "AI"


# ---- 工具执行回路 ----


async def test_kb_search_hits_preloaded_corpus(kb_db):
    retriever = HybridRetriever(Settings(kb_enabled=True), embedder=FakeEmbedder())
    services = make_kb_services(kb_db, enabled=True, retriever=retriever)
    await _add_clause(kb_db, anchor="第一条")
    ctx = ToolContext(student_id="20230001", services=services)
    result = await KnowledgeSearchTool().call(
        ctx, KbSearchParams(query="转专业条件")
    )
    assert result.items, "预灌语料应命中转专业条款"
    assert "转专业条件" in result.items[0].content
    assert result.items[0].anchor == "第一条"  # 条款锚透传到工具出参（LLM 可引用到条款级）
    assert result.meta.source.value == "internal"
    assert result.meta.degrade_status.value == "fresh"
    assert result.meta.is_stale is False
    assert result.mode.value == "hybrid"  # FakeEmbedder 双路


async def test_kb_search_empty_corpus_returns_empty(kb_db):
    """空库诚实：合法空返回（boundary-empty 语义），不编造。"""
    retriever = HybridRetriever(Settings(kb_enabled=True), embedder=FakeEmbedder())
    services = make_kb_services(kb_db, enabled=True, retriever=retriever)
    ctx = ToolContext(student_id="20230001", services=services)
    result = await KnowledgeSearchTool().call(
        ctx, KbSearchParams(query="学生证补办")
    )
    assert result.items == []
    assert result.meta.is_stale is False  # 空库回退当前时间，不产生负龄差


async def test_kb_search_clamps_top_k_and_content(kb_db):
    """二次钳制：top_k ≤ kb_top_k、单块 ≤ kb_chunk_max_chars（不信任上游）。"""
    long_clause = CLAUSE + "补充说明。" * 200  # 超 500 字
    retriever = HybridRetriever(Settings(kb_enabled=True), embedder=FakeEmbedder())
    services = make_kb_services(kb_db, enabled=True, retriever=retriever, kb_top_k=1)
    await _add_clause(kb_db, content=long_clause)
    ctx = ToolContext(student_id="20230001", services=services)
    result = await KnowledgeSearchTool().call(
        ctx, KbSearchParams(query="转专业条件", top_k=99)  # 超限 → 钳到 kb_top_k
    )
    assert len(result.items) <= 1
    assert all(len(i.content) <= 500 for i in result.items)


async def test_kb_search_failure_uses_template_channel(kb_db):
    """检索异常 → ToolError + degrade.yaml 模板文案（C4 语义包装，不伪造 mode）。"""

    class ExplodingRetriever:
        async def search(self, session, query):
            raise RuntimeError("db boom")

    services = make_kb_services(kb_db, enabled=True, retriever=ExplodingRetriever())
    ctx = ToolContext(student_id="20230001", services=services)
    with pytest.raises(ToolError) as exc_info:
        await KnowledgeSearchTool().call(
            ctx, KbSearchParams(query="转专业条件")
        )
    assert "知识库暂时无法访问" in str(exc_info.value)


async def test_kb_search_stale_corpus_flagged(kb_db):
    """新鲜度锚：synced_at 超 kb_stale_hours → is_stale=True + corpus_aged。"""
    aged = datetime.now(UTC) - timedelta(hours=24 * 30 + 1)
    retriever = HybridRetriever(Settings(kb_enabled=True), embedder=FakeEmbedder())
    services = make_kb_services(kb_db, enabled=True, retriever=retriever)
    await _add_clause(kb_db, synced_at=aged)
    ctx = ToolContext(student_id="20230001", services=services)
    result = await KnowledgeSearchTool().call(
        ctx, KbSearchParams(query="转专业条件")
    )
    assert result.meta.is_stale is True
    assert result.meta.stale_reason == "corpus_aged"
