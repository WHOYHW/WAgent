"""timetable_query 工具（只读，AI）。"""

from __future__ import annotations

from ..degrade import build_tool_meta
from ..schemas import TimetableQueryParams, TimetableResult
from ..stale import ttl_for_domain
from .base import PreconditionError, Tool, ToolContext, ToolSpec

SPEC = ToolSpec(
    name="timetable_query",
    description="查询学生本人的课表。仅返回调用者本人的数据。",
    params_model=TimetableQueryParams,
    result_model=TimetableResult,
    preconditions=("start_date <= end_date", "学号已绑定（会话注入）"),
    postconditions=("返回项含 is_changed 标注", "meta.is_stale 可信"),
)


class TimetableTool(Tool):
    spec = SPEC

    def validate(self, ctx: ToolContext, params: TimetableQueryParams) -> None:
        if params.start_date > params.end_date:
            raise PreconditionError("start_date 不能晚于 end_date")

    async def execute(self, ctx: ToolContext, params: TimetableQueryParams) -> TimetableResult:
        resp = await ctx.services.edu.fetch_timetable(
            ctx.student_id, params.start_date, params.end_date, params.course_name
        )
        meta = build_tool_meta(
            source=resp.source,
            synced_at=resp.synced_at,
            degrade_status=resp.degrade_status,
            ttl_seconds=ttl_for_domain("timetable", ctx.services.settings),
        )
        return TimetableResult(items=resp.data or [], meta=meta)
