"""知识库检索包（M-KB，K3 v1.4：契约 + 切块 + 表模型 + 检索内核 + 真机档 embedder）。

模块地图（文档 18）：
- schemas.py   契约（KbDocMeta/KbHit/KbSearch*/RetrievalMode/KbRetrievalResult/make_doc_id）
- chunking.py  切块三策略纯函数（by_clause/by_heading/by_qa + fixed 滑窗回退）
- fts.py       FTS5 虚表 DDL 单一事实源（迁移 0005 与测试夹具共用）
- embedder.py  EmbeddingClient 协议 + FakeEmbedder 桩 + ArkEmbeddingClient 真机档（v1.4）
- retriever.py 混合检索内核：FTS5-trigram + numpy 向量路 → RRF → 阈值门
- ingest.py    抓取 → 清洗 → 切块 → 向量化 → 落库 CLI（K3 ingest 步）
- faq_export.py handoff → FAQ 候选只读导出 CLI（K6 回流线，文档 11 §4.1 M/N）
"""

from .chunking import (
    CHUNK_DEFAULT_MAX_CHARS,
    CHUNK_DEFAULT_OVERLAP,
    KbChunkDraft,
    by_clause,
    by_heading,
    by_qa,
)
from .embedder import (
    FAKE_EMBED_DIM,
    ArkEmbeddingClient,
    EmbeddingCallError,
    EmbeddingClient,
    EmbeddingNotConfiguredError,
    FakeEmbedder,
)
from .fts import KB_FTS_TABLE_DDL, KB_FTS_TRIGGER_DDLS
from .retriever import CANDIDATE_POOL, TRIGRAM_MIN_CHARS, HybridRetriever, Retriever
from .schemas import (
    KbDocMeta,
    KbHit,
    KbRetrievalResult,
    KbSearchParams,
    KbSearchResult,
    RetrievalMode,
    make_doc_id,
)

__all__ = [
    "CANDIDATE_POOL",
    "CHUNK_DEFAULT_MAX_CHARS",
    "CHUNK_DEFAULT_OVERLAP",
    "FAKE_EMBED_DIM",
    "KB_FTS_TABLE_DDL",
    "KB_FTS_TRIGGER_DDLS",
    "ArkEmbeddingClient",
    "EmbeddingCallError",
    "EmbeddingClient",
    "EmbeddingNotConfiguredError",
    "FakeEmbedder",
    "HybridRetriever",
    "KbChunkDraft",
    "KbDocMeta",
    "KbHit",
    "KbRetrievalResult",
    "KbSearchParams",
    "KbSearchResult",
    "RetrievalMode",
    "Retriever",
    "TRIGRAM_MIN_CHARS",
    "by_clause",
    "by_heading",
    "by_qa",
    "make_doc_id",
]
