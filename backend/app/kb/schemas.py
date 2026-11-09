"""知识库检索契约（文档 18 §三/§四/§五，K1 v1.2）。

零 IO 纪律：本模块只含 Pydantic 契约与 make_doc_id 确定性纯函数——切块三策略
在 chunking.py（K1）、抓取/向量化/落库在 ingest（K3）、检索执行在 retriever
（K2）。本模块可全量单测且无网络，对齐 P4"纯函数先行"原则与"禁止裸 dict"
纪律（15 §C：检索元信息一律建模为嵌套模型，不设 ``meta: dict``）。
"""

from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, Field

from ..schemas.meta import ToolMeta


def make_doc_id(source_url: str) -> str:
    """URL → 确定性文档 ID（sha256 前 16 位十六进制）。

    同一 URL 永远得到同一 doc_id：增量刷新按 doc_id 定位整页、
    按 content_hash 判"未变跳过 / 已变删除重插"（文档 18 §三）。
    """
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]


class RetrievalMode(str, Enum):
    """检索执行模式（文档 18 §四 v1.3 四态：D4 降级语义 + trigram 短查询兜底）。

    - ``HYBRID``：FTS5-trigram + 向量双路 RRF 融合；
    - ``VECTOR_ONLY``：查询 <3 字符（trigram 最小 token 长度）FTS 短语必空，
      跳过 FTS 纯向量兜底（v1.3 裁决③）；
    - ``FTS_ONLY``：向量路不可用（无 embedder / embed 抛错 / 维度不一致），
      仅关键词路——<3 字查询照常执行 FTS，如实返回空，不谎报模式。
    """

    HYBRID = "hybrid"
    VECTOR_ONLY = "vector_only"
    FTS_ONLY = "fts_only"


class KbDocMeta(BaseModel):
    """入库文档级元数据（K3 ingest 组装，K2 检索反查溯源）。"""

    doc_id: str = Field(description="URL 确定性 ID（make_doc_id），整页删除粒度")
    title: str = Field(description="页面标题")
    source_url: str = Field(description="来源 URL（域白名单内，附录 A）")
    column: str = Field(description="所属栏目，如 教务处/通知公告、新闻网/学术动态")
    fetched_at: str = Field(description="抓取时间（ISO 8601 含时区）")
    corpus_version: str = Field(description="语料版本锚，如 2026-11-05.1（对齐 prompt_version 惯例）")


class KbHit(BaseModel):
    """单条检索命中。``content`` 为命中块原文——工具结果直接引用，不二次生成。

    ``anchor`` 是切块锚（chunking 三策略产出：条款号 / markdown 标题 / FAQ 问题行；
    滑窗回退块为 None）——LLM 据此把引用粒度从文档级升到条款级（提示词第 9 条）。
    """

    doc_id: str
    title: str
    content: str
    source_url: str
    anchor: str | None = Field(default=None, description="条款/标题/问题行锚，引用粒度的最小定位单位")
    score: float = Field(description="RRF 融合分，仅排序用，不出现在给用户的文本里")


class KbSearchParams(BaseModel):
    """``kb_search`` 工具入参（文档 18 §五 v1.5）。

    ``top_k`` 可选：模型可请求更少结果，工具层二次钳制 ``≤ kb_top_k``（不信任上游）。
    """

    query: str = Field(
        min_length=1,
        max_length=256,
        description="检索词，用学生的自然说法，如“转专业条件”“图书馆开放时间”",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="返回条数上限（可选）；超过系统上限时按系统上限钳制",
    )


class KbSearchResult(BaseModel):
    """``kb_search`` 工具出参：命中列表 + 检索模式 + 溯源 meta（R1.3 三元组）。

    v1.5 裁决①：出参键沿用既有工具 ``items`` 惯例（v1.0 的 ``hits`` 废弃）——
    ``orchestrator._result_digest`` 认 ``items`` 键，SSE 摘要免费获得 ``{count: n}``。
    """

    items: list[KbHit] = Field(default_factory=list, description="RRF 融合序，Top-K")
    mode: RetrievalMode = Field(description="实际执行的检索模式（fts_only = 语义路已降级）")
    meta: ToolMeta = Field(description="溯源三元组；语料陈旧时 is_stale=True（kb_stale_hours 判定）")


class KbRetrievalResult(BaseModel):
    """检索内核出参（v1.3 裁决⑧）：``retriever.search`` → K4 工具层。

    K4 据此补 ``meta``（ToolMeta 溯源三元组）组装 ``KbSearchResult``；
    内核层不造 meta——新鲜度/stale 判定是工具层职责（§四 新鲜度锚）。
    """

    hits: list[KbHit] = Field(default_factory=list, description="RRF 融合序，已过阈值门与 Top-K")
    mode: RetrievalMode = Field(description="实际执行的检索模式")


__all__ = [
    "KbDocMeta",
    "KbHit",
    "KbRetrievalResult",
    "KbSearchParams",
    "KbSearchResult",
    "RetrievalMode",
    "make_doc_id",
]
