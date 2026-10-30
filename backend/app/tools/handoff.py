"""handoff_create 工具（转人工工单，AI+H）。"""

from __future__ import annotations

from datetime import UTC, datetime

from ..degrade import build_tool_meta
from ..logging_util import get_logger, write_audit
from ..orm import HandoffTicket
from ..schemas import HandoffCreateParams, HandoffTicketOut
from ..schemas.meta import DegradeStatus, ToolSource
from ..security import mask_student_id
from .base import Tool, ToolContext, ToolSpec

logger = get_logger("tools.handoff")

SPEC = ToolSpec(
    name="handoff_create",
    description="生成转人工工单。必须携带完整上下文，缺失字段视为不合格。",
    params_model=HandoffCreateParams,
    result_model=HandoffTicketOut,
    preconditions=("必填字段齐全（hit_rules / agent_response_log / priority）",),
    postconditions=("工单进入 open 状态", "上下文完整率 100%"),
    boundary="AI+H",
)


class HandoffCreateTool(Tool):
    spec = SPEC

    async def execute(self, ctx: ToolContext, params: HandoffCreateParams) -> HandoffTicketOut:
        if ctx.services.db is None:
            raise RuntimeError("handoff_create 需要数据库会话")

        # R1.2：student_id 以会话身份（ctx）为准，忽略模型/请求体传入值。
        # 脱敏存储（R3.4）；retrieved_facts 仅存摘要（R1.5）。
        row = HandoffTicket(
            student_id=mask_student_id(ctx.student_id),
            session_id=params.session_id,
            user_question=params.user_question,
            intent_history=params.intent_history,
            hit_rules=params.hit_rules,
            retrieved_facts=[f.model_dump(mode="json") for f in params.retrieved_facts],
            agent_response_log=params.agent_response_log,
            suggested_owner=params.suggested_owner,
            priority=params.priority.value,
            status="open",
            created_at=datetime.now(UTC),
        )
        ctx.services.db.add(row)
        await ctx.services.db.commit()
        await ctx.services.db.refresh(row)

        # 工单创建独立审计（action=handoff）。审计失败不阻断主流程（工单已落库）。
        try:
            await write_audit(
                ctx.services.db,
                action="handoff",
                student_id=ctx.student_id,
                session_id=params.session_id,
                args_digest={"hit_rules": params.hit_rules, "priority": params.priority.value},
                result="open",
            )
        except Exception:  # noqa: BLE001 — 审计失败不阻断工单返回
            logger.exception("audit_write_failed", tool="handoff_create")

        now = datetime.now(UTC)
        # meta 是必填字段，须在校验时一并提供（先 validate 再 model_copy 会因缺 meta 失败）。
        # 写内部工单库：source=internal、无外部源系统、永不 stale（AC1.4）。
        row_data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        return HandoffTicketOut.model_validate({
            **row_data,
            "meta": build_tool_meta(
                source=ToolSource.INTERNAL,
                synced_at=now,
                degrade_status=DegradeStatus.LIVE_OK,
                ttl_seconds=0,
                now=now,
            ),
        })
