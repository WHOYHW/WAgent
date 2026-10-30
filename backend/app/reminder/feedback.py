"""提醒效果回流（R5.2，实施设计 15 §2.2/§D4/§D5）。

写路径与扫描门（gate.py）职责分离：gate 管扫描侧下发，本模块管查询侧回流。
两段式：SELECT 判「404 vs no-op」边界，CAS 单语句 UPDATE 承载转移合法性
（竞态下 WHERE 兜底，不依赖 SELECT 结果新鲜度，15 §D5）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..orm import ReminderDispatch
from ..schemas.reminder import ReminderFeedbackAction

__all__ = ["FeedbackResult", "apply_feedback"]


@dataclass
class FeedbackResult:
    """单次回流结果（端点据此映射 404 / 200 no-op / 200 applied）。

    - ``exists=False``：行不存在或不归属（行归属校验在查询条件内，D1）→ 404；
    - ``eligible=False``：行存在但 ``status != 'sent'``（failed/skipped_*/claimed
      无可观测对象，15 §D4）→ 200 no-op；
    - ``applied``：CAS UPDATE rowcount==1（状态机推进成功）。
    """

    exists: bool
    eligible: bool = False
    applied: bool = False
    action_taken: str | None = None  # 响应回显：applied 时为新值，否则为当前值


async def apply_feedback(
    db: AsyncSession,
    *,
    dispatch_id: int,
    student_id: str,
    action: ReminderFeedbackAction | str,
    now: datetime,
) -> FeedbackResult:
    """回流落库：opened_at 首次写 + action_taken 单向状态机（15 §2.2）。

    转移合法性在 UPDATE 的 WHERE 内表达：``action_taken IS NULL``（none → 任意）
    或 ``action_taken='viewed' 且 action ∈ {renewed, returned}``；
    ``renewed/returned`` 为终态，后续回流 rowcount=0 → 静默 no-op（D2 裁决，
    拒绝 409/422——回流是埋点类调用，错误噪音比宽容更伤）。
    """
    action_val = (
        action.value if isinstance(action, ReminderFeedbackAction) else str(action)
    )

    # 第一段：归属 + 存在性（404 边界）；SELECT 条件带 student_id（D1）。
    found = await db.execute(
        select(ReminderDispatch).where(
            ReminderDispatch.id == dispatch_id,
            ReminderDispatch.student_id == student_id,
        )
    )
    row = found.scalar_one_or_none()
    if row is None:
        return FeedbackResult(exists=False)
    if row.status != "sent":
        # D4：不可回流行（failed/skipped_*/claimed）→ 200 no-op，不报错。
        return FeedbackResult(
            exists=True, eligible=False, applied=False,
            action_taken=row.action_taken,
        )

    # 第二段：CAS UPDATE（rowcount 判 applied；synchronize_session=False
    # ——SQLite 存 aware datetime 为 naive 字符串，evaluate 策略会炸，R4b 同款坑）。
    result = await db.execute(
        update(ReminderDispatch)
        .where(
            ReminderDispatch.id == dispatch_id,
            ReminderDispatch.student_id == student_id,
            ReminderDispatch.status == "sent",
            or_(
                ReminderDispatch.action_taken.is_(None),           # none → 任意
                and_(                                              # viewed → 终态
                    ReminderDispatch.action_taken == "viewed",
                    action_val in ("renewed", "returned"),
                ),
            ),
        )
        .values(
            opened_at=func.coalesce(ReminderDispatch.opened_at, now),  # 只写首次
            action_taken=action_val,
        )
        .execution_options(synchronize_session=False)
    )
    applied = bool(result.rowcount == 1)
    if applied:
        # store 层同款惯例（conversation/store.py）：模块函数内提交，请求级
        # 会话关闭即回滚，不提交则 CAS 结果仅存于未提交事务（R5.4 实测踩坑）。
        await db.commit()

    # 回显当前状态：更新成功取新值；no-op 取库中现值（重新读取，避免 ORM 不同步）。
    refreshed = await db.execute(
        select(ReminderDispatch.action_taken).where(ReminderDispatch.id == dispatch_id)
    )
    current = refreshed.scalar_one()
    return FeedbackResult(
        exists=True, eligible=True, applied=applied, action_taken=current
    )
