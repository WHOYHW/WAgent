"""图书馆借阅 / 续借契约（文档 12 §3.1 只读 + §3.2 白名单写）。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from .meta import STUDENT_ID_DESC, ToolMeta


class LibraryLoansParams(BaseModel):
    student_id: str = Field(description=STUDENT_ID_DESC)
    include_history: bool = Field(default=False, description="是否包含历史借阅")


class LibraryLoanItem(BaseModel):
    """单条在借图书。``renewable`` 与拒绝原因来自 ILS，不由 AI 推断。"""

    title: str
    barcode: str
    due_date: date = Field(description="应还日期")
    overdue_days: int = Field(ge=0, description="超期天数（0 表示未超期）")
    renewable: bool = Field(description="是否可续借（来自 ILS）")
    renew_reject_reason: str | None = Field(default=None, description="不可续借原因")
    renew_times_left: int = Field(ge=0, default=0, description="剩余可续借次数")
    fine_accrued: float = Field(ge=0, default=0.0, description="已产生罚金")


class LibraryLoansResult(BaseModel):
    items: list[LibraryLoanItem] = Field(
        default_factory=list, description="按应还日期升序（超期优先）"
    )
    meta: ToolMeta


class LibraryRenewParams(BaseModel):
    """续借入参。幂等键 ``idempotency_key`` = sha256(session_id + 排序后条码 + 日期)。

    ``confirmed`` 为二次确认态（R2.4 前置条件"已展示新到期日并获用户确认"的执行点），
    编排层必须先展示新到期日、用户确认后方可置 True 调用。
    """

    student_id: str = Field(description=STUDENT_ID_DESC)
    barcodes: list[str] = Field(min_length=1, max_length=10, description="待续借条码列表")
    idempotency_key: str = Field(description="会话ID+条码+日期 的哈希，防重复提交")
    confirmed: bool = Field(default=False, description="已向用户展示新到期日并获确认")


class LibraryRenewItem(BaseModel):
    barcode: str
    ok: bool = Field(description="是否续借成功")
    new_due_date: date | None = Field(default=None, description="续借后新到期日")
    reason: str | None = Field(default=None, description="失败原因")


class LibraryRenewResult(BaseModel):
    results: list[LibraryRenewItem] = Field(default_factory=list)
    meta: ToolMeta
