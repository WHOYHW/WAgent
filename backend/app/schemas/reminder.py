"""提醒订阅契约（文档 12 §3.3 订阅管理工具 reminder_subscribe）。

触发配置 ``trigger_config`` 为 JSONB 字段，须在此以 ``TriggerConfig`` 定型（§3.3.2 R3.3），
不允许裸 ``dict`` 入库。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .meta import STUDENT_ID_DESC, ToolMeta


class ReminderSubject(str, Enum):
    """订阅对象枚举（文档 12 §3.3）。"""

    COURSE = "course"
    LIBRARY_DUE = "library_due"
    GRADE_PUBLISH = "grade_publish"
    EXAM = "exam"
    SIGNUP_DEADLINE = "signup_deadline"
    RESERVATION_ARRIVAL = "reservation_arrival"


class ReminderChannel(str, Enum):
    WECOM = "wecom"
    WECHAT_MP = "wechat_mp"
    MINIAPP = "miniapp"
    SMS_FALLBACK = "sms_fallback"


class ReminderTriggerType(str, Enum):
    TIME = "time"
    EVENT = "event"
    THRESHOLD = "threshold"


class ThresholdCondition(BaseModel):
    """阈值型触发条件（R3.3：JSONB 结构定型，禁止裸 dict）。"""

    metric: str = Field(description="指标名，如 due_days / grade_published")
    op: str = Field(description="比较符：lt / lte / gt / gte / eq")
    value: float = Field(description="阈值")


class EventCondition(BaseModel):
    """事件型触发条件。"""

    event_code: str = Field(description="事件编码，如 grade_publish / reservation_arrival")


class TriggerConditions(BaseModel):
    """按触发器类型选填的条件结构（time 型可全部缺省）。"""

    threshold: ThresholdCondition | None = None
    event: EventCondition | None = None


class TriggerConfig(BaseModel):
    """订阅触发配置的 JSONB 结构（§3.3.2 R3.3，定型后 model_dump 入库）。"""

    timing: list[str] = Field(default_factory=list, description='提前量，如 ["T-3d","T-1d"]')
    conditions: TriggerConditions = Field(default_factory=TriggerConditions)


class ReminderSubscribeParams(BaseModel):
    student_id: str = Field(description=STUDENT_ID_DESC)
    subject: ReminderSubject
    timing: list[str] | None = Field(default=None, description='如 ["T-3d","T-1d"]')
    channel: ReminderChannel = Field(default=ReminderChannel.WECOM)
    enabled: bool = Field(default=True)


class ReminderSubscribeResult(BaseModel):
    """订阅写操作的结构化确认（写内部订阅库；meta.source=internal，AC1.4 全出参带 meta）。"""

    subscription_id: int
    student_id: str
    subject: ReminderSubject
    trigger_type: ReminderTriggerType
    channel: ReminderChannel
    enabled: bool
    updated_at: datetime
    meta: ToolMeta
