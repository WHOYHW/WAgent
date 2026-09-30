
"""C3 数据模型：4 张 PostgreSQL 表 + 索引/约束（文档 12 §4 + 规范 §3.3）。

JSONB 字段（``trigger_config`` / ``intent_history`` / ``hit_rules`` /
``retrieved_facts`` / ``args_digest``）的结构在契约层（schemas.*）定义，
写入前由服务层经 Pydantic 校验后 ``model_dump()`` 入库，禁止裸 dict（R3.3）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, IdMixin, JSONBType, TimestampTZ


class ReminderSubscription(IdMixin, Base):
    """提醒订阅（文档 12 §4 reminder_subscription）。"""

    __tablename__ = "reminder_subscription"
    __table_args__ = (
        Index("idx_sub_student", "student_id", "enabled"),
        Index("idx_sub_subject", "subject", "trigger_type"),
    )

    student_id: Mapped[str] = mapped_column(String(20), nullable=False)
    subject: Mapped[str] = mapped_column(String(32), nullable=False)  # course / library_due / ...
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)  # time / event / threshold
    trigger_config: Mapped[dict[str, Any]] = mapped_column(JSONBType, nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="wecom")
    priority: Mapped[str] = mapped_column(String(4), nullable=False, default="P2")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReminderDispatch(IdMixin, Base):
    """提醒发送记录（幂等 + 效果回流，文档 12 §4 reminder_dispatch）。"""

    __tablename__ = "reminder_dispatch"
    __table_args__ = (
        # 数据库层去重（R2.3 / AC3.4）
        UniqueConstraint("dedup_key", "dedup_date", name="uq_dispatch_dedup"),
        Index("idx_dispatch_student", "student_id", "pushed_at"),
    )

    subscription_id: Mapped[int] = mapped_column(Integer, nullable=False)
    student_id: Mapped[str] = mapped_column(String(20), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)  # hash(subject+trigger+date)
    dedup_date: Mapped[date] = mapped_column(nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # sent/failed/degraded/skipped_quiet
    pushed_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    action_taken: Mapped[str | None] = mapped_column(String(32), nullable=True)  # viewed/renewed/returned
    is_degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class HandoffTicket(IdMixin, Base):
    """转人工工单（文档 12 §4 handoff_ticket）。"""

    __tablename__ = "handoff_ticket"
    __table_args__ = (
        Index("idx_ticket_sla", "status", "priority", "created_at"),
    )

    student_id: Mapped[str] = mapped_column(String(20), nullable=False)  # 脱敏存储（R3.4）
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_question: Mapped[str] = mapped_column(Text, nullable=False)
    intent_history: Mapped[list[str]] = mapped_column(JSONBType, nullable=False, default=list)
    hit_rules: Mapped[list[str]] = mapped_column(JSONBType, nullable=False)
    retrieved_facts: Mapped[list[dict[str, Any]]] = mapped_column(JSONBType, nullable=False, default=list)
    agent_response_log: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[str] = mapped_column(String(4), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")  # open/handling/closed
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_response_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False, server_default=func.now())


class AuditLog(IdMixin, Base):
    """审计日志（脱敏，文档 12 §4 audit_log）。"""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_action", "action", "ts"),
    )

    ts: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False, server_default=func.now())
    student_id: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 脱敏后
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False)  # tool_call/redline_block/write_exec/handoff
    tool_name: Mapped[str | None] = mapped_column(String(48), nullable=True)
    args_digest: Mapped[dict[str, Any]] = mapped_column(JSONBType, nullable=False, default=dict)
    result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel: Mapped[str | None] = mapped_column(String(24), nullable=True)


class ConversationSession(IdMixin, Base):
    """会话（短期记忆载体；阶段一上限 30 分钟 TTL，文档 12 §4 + 任务设计 W1.1）。

    ``session_id`` 由应用层生成 ``sess_<16hex>``；``student_id`` 与 ``handoff_ticket`` /
    ``audit_log`` 字段一致（VARCHAR(20)）；``status`` 形如 ``active / expired / closed``。
    """

    __tablename__ = "conversation_session"
    __table_args__ = (
        Index("idx_session_student_status", "student_id", "status"),
        Index("idx_session_expires", "expires_at"),
    )

    session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    student_id: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="wecom")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    summary_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False)


class Message(IdMixin, Base):
    """单条消息（user / assistant / tool / system；任务设计 W1.1）。

    - ``seq`` 会话内单调递增，由应用层 ``MAX(seq)+1`` 分配；
    - 唯一约束 ``(session_id, seq)`` 兜底并发冲突；
    - ``tool_calls`` 用 JSONB 直接存 OpenAI Function Calling 的 tool_calls 数组；
    - ``deleted_at`` 软删：压缩时被覆盖的历史消息仅打点，不物理删除（满足 ≥180 天留存）；
    - ``is_summary=True`` 标识该条是压缩产生的 system 摘要，编排层据此区分。
    """

    __tablename__ = "message"
    __table_args__ = (
        Index("idx_msg_session_seq", "session_id", "seq"),
        Index("idx_msg_session_role", "session_id", "role"),
        UniqueConstraint("session_id", "seq", name="uq_msg_session_seq"),
    )

    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONBType, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_summary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(TimestampTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=func.now()
    )
