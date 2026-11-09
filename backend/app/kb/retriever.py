"""混合检索内核（文档 18 §四 v1.3，K2）。

双路候选 → RRF 融合 → 阈值门 → Top-K：
- FTS 路：FTS5-trigram 短语转义查询 + bm25 排序（SQLite 方言，D3；PG 推迟立项）；
- 向量路：numpy 全量余弦（D2），矩阵按语料键 ``(MAX(corpus_version), MAX(id))``
  懒加载缓存、键变自动重建（ingest 失效零通知）；embedder 未配置/抛错/维度
  不一致时降级单路（D4"宁可单路，不出错路"）；
- 模式三态：hybrid / vector_only（<3 字 FTS 必空，纯向量兜底）/ fts_only。

职责边界：本层不重试、不造 meta、不产出降级文案——DB 异常向上抛，C4 语义
包装与 ToolMeta 组装是 K4 工具层职责；测试零网络（FakeEmbedder 桩）。
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging_util import get_logger
from app.orm import KbChunk

from .embedder import EmbeddingClient
from .schemas import KbHit, KbRetrievalResult, RetrievalMode

logger = get_logger("kb")

#: FTS trigram tokenizer 最小 token 长度：查询 <3 字符时短语必空，跳过 FTS 路
TRIGRAM_MIN_CHARS = 3

#: 单路候选池深度（§四：两路各取 Top-20 进 RRF）
CANDIDATE_POOL = 20


class Retriever(Protocol):
    """检索内核冻结协议（K4 工具层与黄金集桩依赖此协议，duck typing）。"""

    async def search(self, session: AsyncSession, query: str) -> KbRetrievalResult:
        """双路检索 + RRF 融合 + 阈值门。"""
        ...


class HybridRetriever:
    """SQLite FTS5 + numpy 向量双路检索（文档 18 §四 v1.3）。"""

    def __init__(self, settings: Settings, embedder: EmbeddingClient | None = None) -> None:
        self._settings = settings
        self._embedder = embedder
        # 向量矩阵缓存：语料键 → (ids, 归一化矩阵)；矩阵 None = 该键下维度
        # 不一致已标记不可用（键不变则不重建，避免每查重扫全表 BLOB）
        self._cache_key: tuple[str, int] | None = None
        self._cache_ids: list[int] = []
        self._cache_matrix: np.ndarray | None = None

    async def search(self, session: AsyncSession, query: str) -> KbRetrievalResult:
        fts_usable = len(query.strip()) >= TRIGRAM_MIN_CHARS
        fts_ids = await self._fts_search(session, query) if fts_usable else []

        qvec = await self._query_vector(query)
        vec_ids: list[int] | None = None
        if qvec is not None:
            vec_ids = await self._vector_search(session, qvec)

        if vec_ids is not None:
            mode = RetrievalMode.HYBRID if fts_usable else RetrievalMode.VECTOR_ONLY
        else:
            mode = RetrievalMode.FTS_ONLY
            vec_ids = []

        fused = self._rrf_fuse(fts_ids, vec_ids)
        gated = [(cid, score) for cid, score in fused if score >= self._settings.kb_score_threshold]
        top = gated[: self._settings.kb_top_k]
        rows = await self._fetch_rows(session, [cid for cid, _ in top])
        hits = []
        for cid, score in top:
            row = rows.get(cid)
            if row is None:
                # 防御：矩阵缓存的 id 已不在库中（手工清库 / 缓存键在"删非最大 id
                # 且无新增"时不变）——跳过该块，不让整个检索降级成不可访问
                continue
            hits.append(
                KbHit(
                    doc_id=row.doc_id,
                    title=row.title,
                    content=row.content,
                    source_url=row.source_url,
                    anchor=row.anchor,
                    score=score,
                )
            )
        return KbRetrievalResult(hits=hits, mode=mode)

    # ---- FTS 路（SQLite）----

    async def _fts_search(self, session: AsyncSession, query: str) -> list[int]:
        """短语转义 → MATCH → bm25 序候选 id 列表（返回序即排名，1 基）。

        转义规则（§四 v1.3 裁决④）：整体包双引号成短语查询、内部引号双写，
        任何用户输入不能逃出短语字面量（``NEAR(``/``AND``/列过滤语法全失效）。
        """
        escaped = '"' + query.replace('"', '""') + '"'
        sql = text(
            "SELECT c.id, bm25(kb_chunk_fts) AS score "
            "FROM kb_chunk_fts JOIN kb_chunk c ON c.id = kb_chunk_fts.rowid "
            "WHERE kb_chunk_fts MATCH :q "
            "ORDER BY score, c.id LIMIT :n"
        )
        result = await session.execute(sql, {"q": escaped, "n": CANDIDATE_POOL})
        return [int(row.id) for row in result]

    # ---- 向量路（numpy 全量余弦）----

    async def _query_vector(self, query: str) -> np.ndarray | None:
        """查询向量化；embedder 未配置或抛错 → None（D4 降级，记日志不重试）。"""
        if self._embedder is None:
            return None
        try:
            vectors = await self._embedder.embed_texts([query])
        except Exception as exc:  # noqa: BLE001 —— 设计裁决：embed 失败一律降级单路
            logger.warning("kb 向量路 embed 失败，降级 FTS 单路", error=type(exc).__name__)
            return None
        return np.asarray(vectors[0], dtype=np.float32)

    async def _vector_search(self, session: AsyncSession, qvec: np.ndarray) -> list[int] | None:
        """全量余弦 Top-20 id 序；None = 向量路降级（矩阵不可用/维度不一致）。"""
        await self._ensure_matrix(session)
        if self._cache_matrix is None:
            return None
        if self._cache_matrix.shape[0] == 0:
            return []  # 语料尚无向量，向量路合法空（ingest 后键变自动重载）
        if self._cache_matrix.shape[1] != qvec.shape[0]:
            logger.warning(
                "kb 查询向量维度与矩阵不一致，降级 FTS",
                query_dim=int(qvec.shape[0]), matrix_dim=int(self._cache_matrix.shape[1]),
            )
            return None
        norm = float(np.linalg.norm(qvec))
        q = qvec / norm if norm > 0 else qvec  # 零向量守卫：保持零向量，不产生 NaN
        scores = self._cache_matrix @ q
        order = np.argsort(-scores, kind="stable")[:CANDIDATE_POOL]
        return [self._cache_ids[int(i)] for i in order]

    async def _ensure_matrix(self, session: AsyncSession) -> None:
        """语料键懒加载：键同复用缓存，键变重建（ingest 删重插后失效零通知）。"""
        key = await self._corpus_key(session)
        if key == self._cache_key:
            return
        rows = (
            await session.execute(
                select(KbChunk.id, KbChunk.embedding)
                .where(
                    KbChunk.embedding_model == self._settings.kb_embedding_model,
                    KbChunk.embedding.is_not(None),
                )
                .order_by(KbChunk.id)
            )
        ).all()
        ids: list[int] = []
        vecs: list[np.ndarray] = []
        dim: int | None = None
        consistent = True
        for cid, blob in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            if dim is None:
                dim = int(v.shape[0])
            elif int(v.shape[0]) != dim:
                consistent = False
                break
            ids.append(int(cid))
            vecs.append(v.copy())
        if not consistent:
            logger.warning("kb 向量维度不一致，本语料键标记向量路不可用", corpus_key=str(key))
            self._cache_key, self._cache_ids, self._cache_matrix = key, [], None
            return
        if vecs:
            matrix = np.stack(vecs).astype(np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrix = matrix / np.where(norms > 0, norms, np.float32(1.0))  # 零向量守卫
        else:
            matrix = np.zeros((0, dim or 0), dtype=np.float32)
        self._cache_key, self._cache_ids, self._cache_matrix = key, ids, matrix

    async def _corpus_key(self, session: AsyncSession) -> tuple[str, int]:
        """缓存键 (MAX(corpus_version), MAX(id))，仅统计模型匹配且已向量化的行。"""
        row = (
            await session.execute(
                select(func.max(KbChunk.corpus_version), func.max(KbChunk.id)).where(
                    KbChunk.embedding_model == self._settings.kb_embedding_model,
                    KbChunk.embedding.is_not(None),
                )
            )
        ).one()
        return (row[0] or "", int(row[1] or 0))

    # ---- 融合与收口 ----

    def _rrf_fuse(self, fts_ids: list[int], vec_ids: list[int]) -> list[tuple[int, float]]:
        """RRF：score(d) = Σ 1/(k + rank_i(d))；同分平局按 id 升序（确定性）。"""
        scores: dict[int, float] = {}
        for ids in (fts_ids, vec_ids):
            for rank, cid in enumerate(ids, start=1):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (self._settings.kb_rrf_k + rank)
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    async def _fetch_rows(self, session: AsyncSession, ids: list[int]) -> dict[int, KbChunk]:
        """按 Top-K id 回查命中块行（矩阵缓存只存 ids + 矩阵，内存轻量）。"""
        if not ids:
            return {}
        result = await session.execute(select(KbChunk).where(KbChunk.id.in_(ids)))
        return {row.id: row for row in result.scalars()}


__all__ = ["CANDIDATE_POOL", "TRIGRAM_MIN_CHARS", "HybridRetriever", "Retriever"]
