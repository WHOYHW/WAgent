"""转人工工单契约（文档 12 §3.3 handoff_create + §3.3.2 R3.4 脱敏）。

§3.2.2 R2.5：缺 ``hit_rules`` / ``agent_response_log`` / ``priority`` 任一字段视为不合格工单。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .meta import ToolMeta


class HandoffPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class FactSummary(BaseModel):
    """已查得事实的摘要（§3.1.2 R1.5：只存摘要，不含成绩数值）。"""

    source: str
    synced_at: datetime
    summary: str


class HandoffCreateParams(BaseModel):
    """入参。student_id 以会话身份注入为准（服务端忽略外部传入值，R1.2）；脱敏后入库。"""

    student_id: str = Field(description="学号，会话身份注入；服务端忽略外部传入值（R1.2）")
    session_id: str
    user_question: str
    intent_history: list[str] = Field(default_factory=list)
    hit_rules: list[str] = Field(min_length=1, description="命中红线规则（必填）")
    retrieved_facts: list[FactSummary] = Field(default_factory=list)
    agent_response_log: str = Field(min_length=1, description="Agent 已输出内容（必填，避免人工重复解释）")
    suggested_owner: str | None = Field(default=None)
    priority: HandoffPriority


class HandoffTicketOut(BaseModel):
    """工单出参（ORM 映射，from_attributes=True；meta 由工具层构造后附加，AC1.4）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: str
    session_id: str
    user_question: str
    intent_history: list[str] = Field(default_factory=list)
    hit_rules: list[str] = Field(default_factory=list)
    retrieved_facts: list[FactSummary] = Field(default_factory=list)
    agent_response_log: str
    suggested_owner: str | None = None
    priority: HandoffPriority
    status: str
    created_at: datetime
    meta: ToolMeta
