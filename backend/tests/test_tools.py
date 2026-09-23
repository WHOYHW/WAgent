"""C2/C3 验收测试：幂等（AC2.2）、确认态与频控（R2.4）、handoff 会话身份（R1.2）、
审计落库（AC3.7）、订阅 upsert、degrade_status 端到端透传（R4.4）。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.orm import AuditLog, HandoffTicket, ReminderSubscription
from app.schemas import (
    FactSummary,
    HandoffCreateParams,
    HandoffPriority,
    LibraryRenewParams,
    ReminderChannel,
    ReminderSubscribeParams,
    ReminderSubject,
    TimetableQueryParams,
)
from app.schemas.meta import DegradeStatus
from app.security import mask_student_id
from app.tools.base import PreconditionError, ToolContext
from app.tools.registry import build_registry
from conftest import StubEduAdapter, StubLibAdapter, make_services

import pytest


def _ctx(db=None, *, student_id="20230001", session_id="sess-1", services=None):
    return ToolContext(
        student_id=student_id,
        session_id=session_id,
        services=services or make_services(db=db),
    )


# ---- 续借：确认态 / 幂等 / 频控 ----


async def test_renew_requires_confirmation():
    reg = build_registry(make_services())
    params = LibraryRenewParams(
        student_id="20230001", barcodes=["B001"],
        idempotency_key="k1", confirmed=False,
    )
    with pytest.raises(PreconditionError):
        await reg.dispatch("library_renew", _ctx(), params)


async def test_renew_idempotency_replay_single_call():
    lib = StubLibAdapter()
    services = make_services(lib=lib)
    reg = build_registry(services)
    ctx = _ctx(services=services)
    params = LibraryRenewParams(
        student_id="20230001", barcodes=["B001"],
        idempotency_key="k-1", confirmed=True,
    )
    first = await reg.dispatch("library_renew", ctx, params)
    second = await reg.dispatch("library_renew", ctx, params)
    assert lib.renew_calls == 1  # AC2.2：重放只执行一次
    assert first == second
    assert first.meta.degrade_status == DegradeStatus.LIVE_OK


async def test_renew_session_limit():
    reg = build_registry(make_services())
    ctx = _ctx()
    for i in range(10):
        params = LibraryRenewParams(
            student_id="20230001", barcodes=[f"B{i:03d}"],
            idempotency_key=f"k-{i}", confirmed=True,
        )
        await reg.dispatch("library_renew", ctx, params)
    with pytest.raises(PreconditionError):
        await reg.dispatch("library_renew", ctx, LibraryRenewParams(
            student_id="20230001", barcodes=["B999"],
            idempotency_key="k-11", confirmed=True,
        ))


async def test_renew_write_failed_returns_structured_result():
    """写失败：逐条 ok=False 且原因透传，不抛 500（R4.4 / E_WRITE_REJECTED）。"""
    lib = StubLibAdapter()
    lib.renew_ok = False
    services = make_services(lib=lib)
    reg = build_registry(services)
    result = await reg.dispatch("library_renew", _ctx(services=services), LibraryRenewParams(
        student_id="20230001", barcodes=["B001"],
        idempotency_key="k-fail", confirmed=True,
    ))
    assert result.meta.degrade_status == DegradeStatus.WRITE_FAILED
    assert result.results[0].ok is False
    assert result.results[0].reason == "图书馆系统不可用"


# ---- handoff：会话身份注入（R1.2）与审计落库（AC3.7）----


async def test_handoff_uses_ctx_student_id_not_params(db):
    reg = build_registry(make_services(db=db))
    params = HandoffCreateParams(
        student_id="9999999",  # 模型/请求体伪造的身份，应被忽略
        session_id="sess-1",
        user_question="成绩复核怎么走流程？",
        hit_rules=["R1_终局性结论"],
        retrieved_facts=[FactSummary(source="edu_system", synced_at="2026-09-20T10:00:00Z", summary="已查得 2 门成绩")],
        agent_response_log="已说明复核流程",
        priority=HandoffPriority.P2,
    )
    out = await reg.dispatch("handoff_create", _ctx(db, student_id="20230001"), params)
    assert out.meta.source.value == "internal"
    row = (await db.execute(select(HandoffTicket))).scalar_one()
    assert row.student_id == mask_student_id("20230001")  # 用 ctx 身份且脱敏


async def test_dispatch_writes_audit_row(db):
    reg = build_registry(make_services(db=db))
    await reg.dispatch("timetable_query", _ctx(db), TimetableQueryParams(
        student_id="20230001",
        start_date=date(2026, 9, 20),
        end_date=date(2026, 9, 21),
    ))
    rows = (await db.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].student_id == mask_student_id("20230001")  # 脱敏（R3.4）
    assert rows[0].result == "ok"


# ---- reminder：upsert 幂等 ----


async def test_reminder_subscribe_upsert(db):
    reg = build_registry(make_services(db=db))
    ctx = _ctx(db)
    base = dict(student_id="20230001", subject=ReminderSubject.LIBRARY_DUE,
                channel=ReminderChannel.WECOM)
    first = await reg.dispatch("reminder_subscribe", ctx, ReminderSubscribeParams(**base))
    second = await reg.dispatch("reminder_subscribe", ctx, ReminderSubscribeParams(
        **base, enabled=False))
    assert second.subscription_id == first.subscription_id
    assert second.enabled is False
    assert second.meta.degrade_status == DegradeStatus.LIVE_OK
    rows = (await db.execute(select(ReminderSubscription))).scalars().all()
    assert len(rows) == 1
