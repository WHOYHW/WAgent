"""library_loans（只读）与 library_renew（白名单写）工具。"""

from __future__ import annotations

from ..degrade import build_tool_meta
from ..schemas import (
    LibraryLoansParams,
    LibraryLoansResult,
    LibraryRenewItem,
    LibraryRenewParams,
    LibraryRenewResult,
)
from ..schemas.meta import DegradeStatus
from ..stale import ttl_for_domain
from .base import PreconditionError, Tool, ToolContext, ToolSpec

LOANS_SPEC = ToolSpec(
    name="library_loans",
    description="查询学生本人的在借图书清单与可续借资格。",
    params_model=LibraryLoansParams,
    result_model=LibraryLoansResult,
    preconditions=("学号已绑定（会话注入）",),
    postconditions=("按 due_date 升序", "renewable 来自 ILS 而非推断"),
)

RENEW_SPEC = ToolSpec(
    name="library_renew",
    description=(
        "续借图书。系统唯一写操作白名单成员：已向用户展示新到期日并获确认、"
        "幂等、失败不重试超过 1 次。"
    ),
    params_model=LibraryRenewParams,
    result_model=LibraryRenewResult,
    preconditions=(
        "已展示新到期日并获用户确认",
        "idempotency_key 有效",
        "本会话调用 ≤ 10 次",
    ),
    postconditions=("返回每条 ok 状态", "失败不重试超 1 次", "审计留痕"),
    write=True,
    boundary="AI",  # 需二次确认（AI+H 的确认态在编排层落地）
)


class LibraryLoansTool(Tool):
    spec = LOANS_SPEC

    async def execute(self, ctx: ToolContext, params: LibraryLoansParams) -> LibraryLoansResult:
        resp = await ctx.services.lib.fetch_loans(ctx.student_id, params.include_history)
        items = sorted(resp.data or [], key=lambda i: i.due_date)  # 后置：按 due_date 升序
        meta = build_tool_meta(
            source=resp.source,
            synced_at=resp.synced_at,
            degrade_status=resp.degrade_status,
            ttl_seconds=ttl_for_domain("library", ctx.services.settings),
        )
        return LibraryLoansResult(items=items, meta=meta)


class LibraryRenewTool(Tool):
    spec = RENEW_SPEC

    def validate(self, ctx: ToolContext, params: LibraryRenewParams) -> None:
        if not params.confirmed:
            raise PreconditionError("需先展示新到期日并获用户确认后方可续借")
        session_id = ctx.session_id or "anonymous"
        used = ctx.services.renew_session_count.get(session_id, 0)
        if used >= ctx.services.settings.renew_per_session_limit:
            raise PreconditionError("本会话续借次数已达上限（≤ 10 次）")

    async def execute(self, ctx: ToolContext, params: LibraryRenewParams) -> LibraryRenewResult:
        # 幂等：同一 idempotency_key 重复提交返回上次结果（AC2.2）。
        if params.idempotency_key in ctx.services.idempotency:
            return ctx.services.idempotency[params.idempotency_key]

        resp, fail_reason = await ctx.services.lib.renew(
            ctx.student_id, params.barcodes, params.idempotency_key
        )

        if resp.degrade_status == DegradeStatus.WRITE_FAILED or resp.data is None:
            # 写失败：逐条返回 ok=False，不重试（R4.3 / AC4.5），原因透传（E_WRITE_REJECTED）。
            items = [
                LibraryRenewItem(barcode=b, ok=False, reason=fail_reason or "续借失败")
                for b in params.barcodes
            ]
        else:
            items = resp.data

        meta = build_tool_meta(
            source=resp.source,
            synced_at=resp.synced_at,
            degrade_status=resp.degrade_status,
            ttl_seconds=ttl_for_domain("library", ctx.services.settings),
        )
        result = LibraryRenewResult(results=items, meta=meta)

        # 记录幂等 + 会话频控。
        ctx.services.idempotency[params.idempotency_key] = result
        session_id = ctx.session_id or "anonymous"
        ctx.services.renew_session_count[session_id] = (
            ctx.services.renew_session_count.get(session_id, 0) + 1
        )
        return result
