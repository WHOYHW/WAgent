"""grade_query 工具（只读，AI，三不原则）。"""

from __future__ import annotations

from ..degrade import build_tool_meta
from ..schemas import GradeAggregate, GradeItem, GradeQueryParams, GradeResult
from .base import Tool, ToolContext, ToolSpec

# 成绩不缓存（红线 R3），ttl=0；口径为「已发布课程」的算术汇总，不含判定。
# 已获学分以「成绩数值 ≥ 60」为预核算口径，最终认定以教务处为准（免责声明在出参顶层）。
_CALIBER = (
    "口径：均分为已发布课程总评成绩的算术平均；已获学分为已发布课程中成绩 ≥60 的学分合计，"
    "预核算结果最终以教务处认定为准"
)

_PASSED_SCORE = 60.0

SPEC = ToolSpec(
    name="grade_query",
    description="查询学生本人的已发布成绩。不得缓存明细，不得写入长期记忆。",
    params_model=GradeQueryParams,
    result_model=GradeResult,
    preconditions=("学号已绑定（会话注入）", "用户已勾选成绩类数据同意"),
    postconditions=("不缓存明细", "返回的 system_status 为源系统原文"),
)


def _aggregate(items: list[GradeItem]) -> GradeAggregate | None:
    if not items:
        return None
    avg = round(sum(i.score for i in items) / len(items), 2)
    earned = round(sum(i.credit for i in items if i.score >= _PASSED_SCORE), 2)
    return GradeAggregate(avg_score=avg, earned_credits=earned, caliber=_CALIBER)


class GradeTool(Tool):
    spec = SPEC

    async def execute(self, ctx: ToolContext, params: GradeQueryParams) -> GradeResult:
        resp = await ctx.services.edu.fetch_grades(
            ctx.student_id, params.term, params.course_name
        )
        items = resp.data or []
        meta = build_tool_meta(
            source=resp.source,
            synced_at=resp.synced_at,
            degrade_status=resp.degrade_status,
            ttl_seconds=0,  # 成绩不缓存 → 永不 stale（AC5.5）
        )
        aggregate = _aggregate(items) if (params.aggregate and items) else None
        return GradeResult(items=items, aggregate=aggregate, meta=meta)
