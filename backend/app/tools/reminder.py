"""reminder_subscribe 工具（订阅管理，写订阅库，非业务系统写）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from ..degrade import build_tool_meta
from ..orm import ReminderSubscription
from ..schemas import (
    ReminderSubject,
    ReminderSubscribeParams,
    ReminderSubscribeResult,
    ReminderTriggerType,
    TriggerConfig,
)
from ..schemas.meta import DegradeStatus, ToolSource
from .base import Tool, ToolContext, ToolSpec

# 订阅对象 → 触发器类型（文档 10 §7.2）
_SUBJECT_TRIGGER = {
    ReminderSubject.COURSE: ReminderTriggerType.TIME,
    ReminderSubject.LIBRARY_DUE: ReminderTriggerType.TIME,
    ReminderSubject.GRADE_PUBLISH: ReminderTriggerType.EVENT,
    ReminderSubject.EXAM: ReminderTriggerType.TIME,
    ReminderSubject.SIGNUP_DEADLINE: ReminderTriggerType.TIME,
    ReminderSubject.RESERVATION_ARRIVAL: ReminderTriggerType.EVENT,
}

SPEC = ToolSpec(
    name="reminder_subscribe",
    description="登记/修改/取消提醒订阅。",
    params_model=ReminderSubscribeParams,
    result_model=ReminderSubscribeResult,
    preconditions=("学号已绑定（会话注入）", "subject 在枚举内"),
    postconditions=("订阅即时生效或即时取消",),
)


class ReminderSubscribeTool(Tool):
    spec = SPEC

    async def execute(
        self, ctx: ToolContext, params: ReminderSubscribeParams
    ) -> ReminderSubscribeResult:
        if ctx.services.db is None:
            raise RuntimeError("reminder_subscribe 需要数据库会话")

        trigger_type = _SUBJECT_TRIGGER[params.subject]
        trigger_config = TriggerConfig(timing=params.timing or [])

        # 同 (student_id, subject, channel) 幂等 upsert。
        stmt = select(ReminderSubscription).where(
            ReminderSubscription.student_id == ctx.student_id,
            ReminderSubscription.subject == params.subject.value,
            ReminderSubscription.channel == params.channel.value,
        )
        existing = (await ctx.services.db.execute(stmt)).scalar_one_or_none()

        if existing is None:
            row = ReminderSubscription(
                student_id=ctx.student_id,
                subject=params.subject.value,
                trigger_type=trigger_type.value,
                trigger_config=trigger_config.model_dump(),
                channel=params.channel.value,
                priority="P2",
                enabled=params.enabled,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            ctx.services.db.add(row)
            await ctx.services.db.flush()
            subscription_id = row.id
        else:
            existing.trigger_config = trigger_config.model_dump()
            existing.trigger_type = trigger_type.value
            existing.enabled = params.enabled
            existing.updated_at = datetime.now(UTC)
            subscription_id = existing.id

        await ctx.services.db.commit()

        now = datetime.now(UTC)
        return ReminderSubscribeResult(
            subscription_id=subscription_id,
            student_id=ctx.student_id,
            subject=params.subject,
            trigger_type=trigger_type,
            channel=params.channel,
            enabled=params.enabled,
            updated_at=now,
            # 写内部订阅库：source=internal、无外部源系统、永不 stale（AC1.4）。
            meta=build_tool_meta(
                source=ToolSource.INTERNAL,
                synced_at=now,
                degrade_status=DegradeStatus.LIVE_OK,
                ttl_seconds=0,
                now=now,
            ),
        )
