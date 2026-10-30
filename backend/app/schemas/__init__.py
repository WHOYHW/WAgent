"""C1 数据契约：Pydantic v2 模型为唯一事实来源。

规范 §3.1 要求：所有工具的入参/出参、适配器返回值必须用 Pydantic v2 模型定义，
JSON Schema 通过 ``model_json_schema()`` 导出，禁止手写 JSON Schema 字符串。

本包按「契约类型 × 业务域」组织，并统一导出顶层符号，供工具层 / 适配层 / 编排层引用。
"""

from .conversation import (
    CompressedSummary,
    Conversation,
    Message,
    MessageIn,
    Role,
)
from .grade import GradeAggregate, GradeItem, GradeQueryParams, GradeResult
from .handoff import (
    FactSummary,
    HandoffCreateParams,
    HandoffPriority,
    HandoffTicketOut,
)
from .library import (
    LibraryLoanItem,
    LibraryLoansParams,
    LibraryLoansResult,
    LibraryRenewItem,
    LibraryRenewParams,
    LibraryRenewResult,
)
from .meta import STUDENT_ID_DESC, AdapterResponse, DegradeStatus, ToolMeta, ToolSource
from .reminder import (
    EventCondition,
    ReminderChannel,
    ReminderFeedbackAction,
    ReminderFeedbackParams,
    ReminderSubject,
    ReminderSubscribeParams,
    ReminderSubscribeResult,
    ReminderTriggerType,
    ThresholdCondition,
    TimingFormatError,
    TriggerConditions,
    TriggerConfig,
    parse_timing,
)
from .timetable import TimetableItem, TimetableQueryParams, TimetableResult

__all__ = [
    # meta / degrade
    "AdapterResponse",
    "DegradeStatus",
    "STUDENT_ID_DESC",
    "ToolMeta",
    "ToolSource",
    # timetable
    "TimetableItem",
    "TimetableQueryParams",
    "TimetableResult",
    # grade
    "GradeAggregate",
    "GradeItem",
    "GradeQueryParams",
    "GradeResult",
    # library
    "LibraryLoanItem",
    "LibraryLoansParams",
    "LibraryLoansResult",
    "LibraryRenewItem",
    "LibraryRenewParams",
    "LibraryRenewResult",
    # reminder
    "ReminderChannel",
    "ReminderFeedbackAction",
    "ReminderFeedbackParams",
    "ReminderSubject",
    "ReminderSubscribeParams",
    "ReminderSubscribeResult",
    "ReminderTriggerType",
    "ThresholdCondition",
    "EventCondition",
    "TriggerConditions",
    "TriggerConfig",
    "TimingFormatError",
    "parse_timing",
    # handoff
    "FactSummary",
    "HandoffCreateParams",
    "HandoffPriority",
    "HandoffTicketOut",
    # conversation（任务设计 W1.3）
    "CompressedSummary",
    "Conversation",
    "Message",
    "MessageIn",
    "Role",
]
