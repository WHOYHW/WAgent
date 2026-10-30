"""提醒订阅契约（文档 12 §3.3 订阅管理工具 reminder_subscribe）。

触发配置 ``trigger_config`` 为 JSONB 字段，须在此以 ``TriggerConfig`` 定型（§3.3.2 R3.3），
不允许裸 ``dict`` 入库。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel, Field, field_validator

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


# ---------------------------------------------------------------------------
# 时机词表契约（09 §2.3）：入库校验与候选生成同源。
# 词表属契约层——候选生成（reminder/candidates.py）反向引用本模块，
# 禁止反向依赖，避免循环 import。
# ---------------------------------------------------------------------------

#: ``T-<N><unit>``，unit ∈ {d, h, min}
TIMING_RE = re.compile(r"^T-(\d+)(d|h|min)$")
_TIMING_UNITS = {"d": "days", "h": "hours", "min": "minutes"}


class TimingFormatError(ValueError):
    """时机串违反契约词表 ``T-<N><unit>``——脏参数在入库前被拒（R3.3 结构定型）。"""


def parse_timing(timing: str) -> timedelta:
    """``T-<N><unit>`` → 时长。unit ∈ {d, h, min}；非法输入显式报错。"""
    m = TIMING_RE.match(timing.strip())
    if m is None:
        raise TimingFormatError(f"非法时机串：{timing!r}（期望 T-3d / T-2h / T-30min）")
    n, unit = int(m.group(1)), _TIMING_UNITS[m.group(2)]
    return timedelta(**{unit: n})


class TriggerConfig(BaseModel):
    """订阅触发配置的 JSONB 结构（§3.3.2 R3.3，定型后 model_dump 入库）。"""

    timing: list[str] = Field(
        default_factory=list,
        description='提前量，须匹配 T-<N><unit>（unit ∈ d/h/min），如 ["T-3d","T-1d"]',
    )
    conditions: TriggerConditions = Field(default_factory=TriggerConditions)

    @field_validator("timing")
    @classmethod
    def _timing_in_vocabulary(cls, v: list[str]) -> list[str]:
        # timing 是 LLM 生成的工具参数，直接落库——脏值（如 "3天前"）若入库，
        # 每个 tick 的扫描都会毒化（09 风险③入库侧防线）。
        for t in v:
            parse_timing(t)
        return v


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


# ---------------------------------------------------------------------------
# 效果回流契约（15 §2.1/§2.2，R5）：POST /agent/reminders/feedback
# ---------------------------------------------------------------------------


class ReminderFeedbackAction(str, Enum):
    """回流行为枚举（单向状态机 none→viewed→renewed/returned，15 §2.2）。

    值对齐 ``orm/models.py`` ``ReminderDispatch.action_taken`` 列注释
    （viewed/renewed/returned）——契约层与 ORM 词表同源，禁止第二份拼写。
    """

    VIEWED = "viewed"
    RENEWED = "renewed"
    RETURNED = "returned"


class ReminderFeedbackParams(BaseModel):
    """回流上报请求体。

    **不含 ``student_id``**——L4 铁律（12 §7）：身份只来自 JWT 注入，
    行归属校验（dispatch.student_id ≠ 令牌身份 → 404）在服务层
    ``apply_feedback`` 执行（15 §D1 裁决）。body 同名字段即使伪造也会被忽略。
    """

    dispatch_id: int = Field(ge=1, description="目标 reminder_dispatch.id（自增主键）")
    action: ReminderFeedbackAction
