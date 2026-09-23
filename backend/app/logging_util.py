"""观测与审计（M8 / C3）：结构化日志 + 脱敏审计。

- structlog 结构化日志（全链路 trace_id 用 contextvar 贯穿）。
- ``write_audit`` 将脱敏审计写入 ``audit_log``（student_id 脱敏，args 摘要化）。
- 审计留存 ≥ 180 天由部署层保证；此处只负责「不写敏感明文」。
"""

from __future__ import annotations

import contextvars
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from .orm import AuditLog
from .security import mask_student_id

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def get_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(trace_id: str | None = None) -> str:
    value = trace_id or uuid.uuid4().hex[:16]
    _trace_id.set(value)
    return value


def get_logger(name: str = "wagent") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


async def write_audit(
    db: AsyncSession,
    *,
    action: str,
    student_id: str | None = None,
    session_id: str | None = None,
    tool_name: str | None = None,
    args_digest: dict[str, Any] | None = None,
    result: str | None = None,
    latency_ms: int | None = None,
    channel: str | None = None,
) -> None:
    """写脱敏审计记录（student_id 脱敏，参数摘要化）并提交。

    审计与业务写入相互独立：本函数自带 commit，调用方（dispatch/handoff）负责
    以 try/except 隔离审计失败，不阻断主流程。
    """
    row = AuditLog(
        student_id=mask_student_id(student_id) if student_id else None,
        session_id=session_id,
        action=action,
        tool_name=tool_name,
        args_digest=args_digest or {},
        result=result,
        latency_ms=latency_ms,
        channel=channel,
        ts=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
