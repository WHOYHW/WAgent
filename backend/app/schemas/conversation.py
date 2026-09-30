"""会话与消息契约（任务设计 W1.3 + 文档 10 §3.4 短期记忆）。

- :class:`Role` 形如 ``user / assistant / tool / system``，与 OpenAI Chat Completions 对齐；
- :class:`MessageIn` 是写入入参；:class:`Message` 是读出 ORM 映射；
- :class:`Conversation` 是会话对外视图，``status`` 三态与 ORM 字段一致；
- :class:`CompressedSummary` 是压缩操作的返回契约，编排层据此把 system 摘要放进 LLM 上下文。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "assistant", "tool", "system"]


class MessageIn(BaseModel):
    """单条消息入参。``tool_calls`` 仅 assistant 角色携带，``tool_call_id`` 仅 tool 角色携带。"""

    role: Role
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None, description="assistant 角色的 Function Calling 工具调用列表"
    )
    tool_call_id: str | None = Field(
        default=None, description="tool 角色对应的 assistant tool_call.id"
    )
    trace_id: str | None = Field(
        default=None, description="全链路 trace_id（与 logging_util._trace_id 对齐）"
    )


class Message(MessageIn):
    """单条消息读出（ORM 映射）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    seq: int
    is_summary: bool = False
    created_at: datetime


class Conversation(BaseModel):
    """会话对外视图（ORM 映射）。"""

    model_config = ConfigDict(from_attributes=True)

    session_id: str
    student_id: str
    channel: str = "wecom"
    status: Literal["active", "expired", "closed"] = "active"
    summary_message_id: int | None = None
    created_at: datetime
    expires_at: datetime


class CompressedSummary(BaseModel):
    """压缩操作返回契约。"""

    session_id: str
    summarized_count: int = Field(ge=0, description="被摘要覆盖并软删的消息数")
    kept_tail: int = Field(ge=0, description="保留的最近若干条")
    summary_message_id: int = Field(description="新写入的 system 摘要消息 id")
