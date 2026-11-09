"""kb_search 知识库检索工具（文档 18 §五 v1.5，K4）。

只读检索（write=False，boundary="AI"，无写门禁）：编排层接入混合检索内核的
唯一入口。职责边界——检索执行/RRF 融合在 ``app.kb.retriever``；本层只做
C4 语义包装（异常 → 工具失败通道 + 模板文案）、ToolMeta 组装（新鲜度锚）、
二次钳制（top_k / 单块长度，不信任上游）与门控注册协作。

student_id 按约定③从根源不适用：kb 语料是公开制度库，无个人数据，
入参契约里没有该字段，导出 Schema 自然干净。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from ..degrade import render_degrade_text
from ..kb import Retriever
from ..kb.schemas import KbSearchParams, KbSearchResult
from ..logging_util import get_logger
from ..orm import KbChunk
from ..schemas.meta import DegradeStatus, ToolMeta, ToolSource
from .base import Tool, ToolContext, ToolError, ToolSpec

logger = get_logger("tools")

SPEC = ToolSpec(
    name="kb_search",
    description=(
        "检索学校规章制度与办事指南公开知识库（转专业、休学复学、证件补办、"
        "开放时间等）。未命中就如实说明未找到，不要编造。"
    ),
    params_model=KbSearchParams,
    result_model=KbSearchResult,
    preconditions=("kb_enabled 已开启（未开启时本工具不注册，LLM 工具列表不出现）",),
    postconditions=("只读检索，零副作用", "空结果如实返回，不编造"),
    write=False,
    boundary="AI",
)


class KnowledgeSearchTool(Tool):
    spec = SPEC

    async def execute(self, ctx: ToolContext, params: KbSearchParams) -> KbSearchResult:
        settings = ctx.services.settings
        retriever: Retriever = ctx.services.kb_retriever  # 门控开启时懒装配必有值
        top_k = min(params.top_k or settings.kb_top_k, settings.kb_top_k)
        try:
            retrieval = await retriever.search(ctx.services.db, params.query)
        except Exception as exc:
            # C4 语义包装（v1.5 裁决⑤）：检索异常走工具失败通道 + 模板文案，
            # 不走降级出参——mode 是检索执行事实，异常时无可如实报告的模式。
            reason = render_degrade_text(DegradeStatus.UNAVAILABLE, SPEC.name)
            logger.warning("kb_search_failed", error=type(exc).__name__)
            raise ToolError(reason) from exc

        # 二次钳制：条数 ≤ kb_top_k、单块 ≤ kb_chunk_max_chars（防会话膨胀，
        # 语料入库后配置收紧时旧块超限的防御性截断）。
        items = [
            hit.model_copy(update={"content": hit.content[: settings.kb_chunk_max_chars]})
            for hit in retrieval.hits
        ][:top_k]

        now = datetime.now(UTC)
        synced_at = await self._corpus_synced_at(ctx, now)
        is_stale = (now - synced_at).total_seconds() > settings.kb_stale_hours * 3600
        meta = ToolMeta(
            source=ToolSource.INTERNAL,
            synced_at=synced_at,
            degrade_status=DegradeStatus.FRESH,
            is_stale=is_stale,
            stale_reason="corpus_aged" if is_stale else None,
        )
        return KbSearchResult(items=items, mode=retrieval.mode, meta=meta)

    async def _corpus_synced_at(self, ctx: ToolContext, now: datetime) -> datetime:
        """新鲜度锚：语料最新同步时间（空库回退当前时间，不产生负龄差）。"""
        value = (await ctx.services.db.execute(select(func.max(KbChunk.synced_at)))).scalar()
        if value is None:
            return now
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
