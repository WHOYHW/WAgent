"""K2 检索内核单测（文档 18 §四 v1.3，AC-18b，零网络）。

覆盖四态模式 + 内核性质：
1. hybrid 双路融合序、AC-18b fixture 语料逐条断言（FakeEmbedder 注入）；
2. vector_only：<3 字短查询纯向量兜底；fts_only：embedder 抛错 / 未配置 / 维度不一致降级；
3. 内核性质：确定性（同查询必同结果）、RRF 数学性质、阈值门截断、空库不抛、
   MATCH 语法注入转义、矩阵缓存键变自动失效（ingest 后零通知重载）。

夹具：内存 SQLite + Base.metadata.create_all + app/kb/fts.py 单源 DDL
（FTS 虚表不进 ORM 元数据，须手动建；与迁移 0005 共用同一常量）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import make_engine
from app.kb import (
    FAKE_EMBED_DIM,
    KB_FTS_TABLE_DDL,
    KB_FTS_TRIGGER_DDLS,
    FakeEmbedder,
    HybridRetriever,
    RetrievalMode,
)
from app.orm import Base, KbChunk

JWC_URL = "https://jwc.hnist.cn/gzzd/xjgl.htm"
MODEL = "doubao-embedding"  # 与 Settings 默认 kb_embedding_model 对齐


async def _embed_bytes(text_content: str) -> bytes:
    """FakeEmbedder → float32 BLOB（落库格式与 K3 ingest 一致）。"""
    (vec,) = await FakeEmbedder().embed_texts([text_content])
    assert len(vec) == FAKE_EMBED_DIM
    return np.asarray(vec, dtype=np.float32).tobytes()


async def _add_chunk(
    session: AsyncSession,
    *,
    content: str,
    doc_id: str = "a" * 16,
    section_idx: int = 0,
    title: str = "转专业实施办法",
    embedding: bytes | None = None,
    embedding_model: str | None = MODEL,
    anchor: str | None = None,
) -> None:
    now = datetime.now(UTC)
    session.add(
        KbChunk(
            doc_id=doc_id,
            section_idx=section_idx,
            anchor=anchor,
            title=title,
            content=content,
            embedding=embedding,
            embedding_model=embedding_model,
            content_hash="0" * 64,
            source_url=JWC_URL,
            column="教务处/规章制度",
            corpus_version="2026-11-05.1",
            fetched_at=now,
            synced_at=now,
        )
    )
    await session.commit()


@pytest_asyncio.fixture
async def kb_session():
    """内存库 + FTS 虚表（单源 DDL）：FTS5 虚表不进 ORM 元数据须手动建。"""
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


# ---- AC-18b：hybrid 双路 + fixture 逐条断言 ----


async def test_hybrid_mode_top_hit_content_asserted(kb_session):
    await _add_chunk(
        kb_session,
        content="第一条 转专业条件：平均学分绩点排名专业前百分之二十，转专业申请于每学期第二周提交。",
        anchor="第一条",
        embedding=await _embed_bytes(
            "第一条 转专业条件：平均学分绩点排名专业前百分之二十，转专业申请于每学期第二周提交。"
        ),
    )
    await _add_chunk(
        kb_session,
        doc_id="b" * 16,
        title="图书馆开放时间",
        content="图书馆开放时间：周一至周五八点至二十二点。",
        embedding=await _embed_bytes("图书馆开放时间：周一至周五八点至二十二点。"),
    )
    retriever = HybridRetriever(Settings(), FakeEmbedder())
    result = await retriever.search(kb_session, "转专业条件")
    assert result.mode is RetrievalMode.HYBRID
    assert result.hits, "混合检索应命中转专业条款块"
    top = result.hits[0]
    assert "转专业条件" in top.content
    assert top.source_url == JWC_URL
    assert top.title == "转专业实施办法"
    assert top.anchor == "第一条"  # 条款锚透传到出参（引用粒度到条款级）
    # 双路 rank-1 融合分 = 2/(k+1)；阈值门保留（默认 0.015）
    assert top.score == pytest.approx(2 / (60 + 1))


async def test_deterministic_same_query_same_result(kb_session):
    await _add_chunk(
        kb_session,
        content="第一条 转专业条件：平均学分绩点排名专业前百分之二十。",
        embedding=await _embed_bytes("第一条 转专业条件：平均学分绩点排名专业前百分之二十。"),
    )
    r1 = HybridRetriever(Settings(), FakeEmbedder())
    r2 = HybridRetriever(Settings(), FakeEmbedder())
    a = await r1.search(kb_session, "转专业条件")
    b = await r2.search(kb_session, "转专业条件")
    assert [(h.doc_id, h.score) for h in a.hits] == [(h.doc_id, h.score) for h in b.hits]
    assert a.mode is b.mode


# ---- RRF 数学性质（纯融合逻辑，不依赖库态）----


def test_rrf_fuse_math_properties():
    k = Settings().kb_rrf_k
    retriever = HybridRetriever(Settings(), None)
    fused = retriever._rrf_fuse([1, 2, 3], [2, 4, 5])
    by_id = dict(fused)
    assert by_id[2] == pytest.approx(1 / (k + 2) + 1 / (k + 1))  # FTS rank-2 + 向量 rank-1
    assert by_id[1] == pytest.approx(1 / (k + 1))  # 仅 FTS rank-1
    assert by_id[4] == pytest.approx(1 / (k + 2))  # 仅向量 rank-2
    assert by_id[2] > by_id[1] > by_id[4]  # 双路命中 > 任一单路
    assert [cid for cid, _ in fused][0] == 2  # 双路参与居首


# ---- vector_only：短查询纯向量兜底 ----


async def test_short_query_vector_only(kb_session):
    await _add_chunk(
        kb_session, content="转专业条件说明", embedding=await _embed_bytes("转专业条件说明")
    )
    await _add_chunk(
        kb_session,
        doc_id="c" * 16,
        title="奖助学金评定",
        content="奖助学金每学年评定一次",
        embedding=await _embed_bytes("奖助学金每学年评定一次"),
    )
    retriever = HybridRetriever(Settings(), FakeEmbedder())
    result = await retriever.search(kb_session, "奖")  # 1 字 < trigram 最小 token
    assert result.mode is RetrievalMode.VECTOR_ONLY
    assert any("奖助学金" in h.content for h in result.hits)


async def test_short_query_without_embedder_honest_empty(kb_session):
    await _add_chunk(kb_session, content="转专业条件说明")
    retriever = HybridRetriever(Settings(), embedder=None)
    result = await retriever.search(kb_session, "奖")  # FTS 路 + 短语必空 → 如实空
    assert result.mode is RetrievalMode.FTS_ONLY
    assert result.hits == []


# ---- fts_only：embedder 未配置 / 抛错 / 维度不一致 ----


async def test_no_embedder_fts_only(kb_session):
    await _add_chunk(kb_session, content="转专业条件：绩点排名前百分之二十。")
    retriever = HybridRetriever(Settings(), embedder=None)
    result = await retriever.search(kb_session, "转专业条件")
    assert result.mode is RetrievalMode.FTS_ONLY
    assert result.hits and "转专业条件" in result.hits[0].content


async def test_embedder_failure_falls_back_to_fts(kb_session):
    class BoomEmbedder:
        async def embed_texts(self, texts):
            raise RuntimeError("embedding 服务不可用")

    await _add_chunk(kb_session, content="转专业条件：绩点排名前百分之二十。")
    retriever = HybridRetriever(Settings(), BoomEmbedder())  # type: ignore[arg-type]
    result = await retriever.search(kb_session, "转专业条件")
    assert result.mode is RetrievalMode.FTS_ONLY  # 宁可单路，不出错路（D4）
    assert result.hits and "转专业条件" in result.hits[0].content


async def test_dim_mismatch_degrades_to_fts(kb_session):
    await _add_chunk(
        kb_session, content="转专业条件说明", embedding=await _embed_bytes("转专业条件说明")
    )
    await _add_chunk(
        kb_session, doc_id="d" * 16, content="残缺向量块", embedding=np.zeros(8, dtype=np.float32).tobytes()
    )
    retriever = HybridRetriever(Settings(), FakeEmbedder())
    result = await retriever.search(kb_session, "转专业条件")
    assert result.mode is RetrievalMode.FTS_ONLY  # 维度不一致 → 向量路标记不可用


async def test_embedding_model_mismatch_excluded_from_vector_path(kb_session):
    await _add_chunk(
        kb_session, content="转专业条件说明", embedding=await _embed_bytes("转专业条件说明")
    )
    await _add_chunk(
        kb_session,
        doc_id="e" * 16,
        title="四六级报名",
        content="英语四六级报名安排",
        embedding=await _embed_bytes("英语四六级报名安排"),
        embedding_model="old-embed",
    )
    retriever = HybridRetriever(Settings(), FakeEmbedder())
    result = await retriever.search(kb_session, "四")  # vector_only：旧模型向量不入矩阵
    assert result.mode is RetrievalMode.VECTOR_ONLY
    assert not any("四六级" in h.content for h in result.hits)


# ---- 阈值门 / 空库 / 注入转义 ----


async def test_threshold_gate_truncates(kb_session):
    await _add_chunk(
        kb_session, content="转专业条件说明", embedding=await _embed_bytes("转专业条件说明")
    )
    # 0.05 > 双路 rank-1（2/61≈0.0328）→ 全滤空
    retriever = HybridRetriever(Settings(kb_score_threshold=0.05), FakeEmbedder())
    result = await retriever.search(kb_session, "转专业条件")
    assert result.hits == []
    # 0.02：滤掉单路 rank-1（1/61≈0.0164），保留双路头部
    retriever2 = HybridRetriever(Settings(kb_score_threshold=0.02), FakeEmbedder())
    result2 = await retriever2.search(kb_session, "转专业条件")
    assert result2.hits


async def test_empty_db_returns_empty_no_raise(kb_session):
    retriever = HybridRetriever(Settings(), FakeEmbedder())
    result = await retriever.search(kb_session, "转专业条件")
    assert result.hits == []  # 空库如实返回空，不抛（boundary-empty 前提）
    assert result.mode is RetrievalMode.HYBRID  # 向量路合法空（语料零向量）


async def test_stale_cache_id_skipped_not_crash(kb_session):
    """矩阵缓存 id 已不在库中（删非最大 id 时缓存键不变）→ 跳过该块，不抛 KeyError。"""
    await _add_chunk(
        kb_session, content="转专业条件说明", embedding=await _embed_bytes("转专业条件说明")
    )  # id=1
    await _add_chunk(
        kb_session,
        doc_id="c" * 16,
        title="奖助学金评定",
        content="奖助学金每学年评定一次",
        embedding=await _embed_bytes("奖助学金每学年评定一次"),
    )  # id=2（MAX(id)，删除 id=1 后缓存键不变）
    retriever = HybridRetriever(Settings(), FakeEmbedder())
    await retriever.search(kb_session, "转专业条件")  # 缓存预热：ids=[1, 2]

    await kb_session.execute(delete(KbChunk).where(KbChunk.id == 1))
    await kb_session.commit()

    assert retriever._cache_key == await retriever._corpus_key(kb_session)  # 键未变，缓存仍含陈旧 id
    result = await retriever.search(kb_session, "转专业条件")  # 修复前此处 KeyError → 整体降级
    assert isinstance(result.hits, list)
    assert all(h.doc_id != "a" * 16 for h in result.hits)  # 陈旧 id 被跳过
    assert result.hits  # 在库的 id=2 正常返回


@pytest.mark.parametrize(
    "query",
    ['转"专业" NEAR(a b)', '转专业" OR 1=1 --', "转专业 * AND col:title", '转专业"）NOT "'],
)
async def test_match_syntax_injection_escaped(kb_session, query):
    await _add_chunk(kb_session, content="转专业条件说明", embedding=await _embed_bytes("转专业条件说明"))
    retriever = HybridRetriever(Settings(), FakeEmbedder())
    result = await retriever.search(kb_session, query)  # 不抛即通过：短语转义封死语法面
    assert isinstance(result.hits, list)


# ---- 矩阵缓存：键变自动失效（ingest 后零通知重载）----


async def test_matrix_cache_invalidated_by_new_data(kb_session):
    await _add_chunk(
        kb_session, content="转专业条件说明", embedding=await _embed_bytes("转专业条件说明")
    )
    retriever = HybridRetriever(Settings(), FakeEmbedder())
    await retriever.search(kb_session, "转专业条件")
    ids_before = list(retriever._cache_ids)
    key_before = retriever._cache_key

    await _add_chunk(
        kb_session,
        doc_id="c" * 16,
        title="奖助学金评定",
        content="奖助学金每学年评定一次",
        embedding=await _embed_bytes("奖助学金每学年评定一次"),
    )
    await retriever.search(kb_session, "转专业条件")
    assert retriever._cache_key != key_before  # 键变触发重建
    assert len(retriever._cache_ids) == len(ids_before) + 1  # 新向量入矩阵

    after = await retriever.search(kb_session, "奖")  # vector_only：纯向量路可见新块
    assert after.mode is RetrievalMode.VECTOR_ONLY
    assert any("奖助学金" in h.content for h in after.hits)
