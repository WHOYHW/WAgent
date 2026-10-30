"""R4b 四闸全序 + 下发闭环测试（实施设计 14 §三：AC-5a ~ 5g）。

纯函数 + 内存库直调 gate.dispatch；引擎端到端复用 test_reminder_engine 基建。
推送统一用 FakePush（记录收到的 content，可注入回执）。
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.adapters.push import PushReceipt
from app.config import Settings
from app.db import make_engine
from app.orm import Base, ReminderDispatch
from app.reminder.candidates import ReminderCandidate
from app.reminder.engine import ReminderEngine
from app.reminder.gate import QUIET_SUFFIX, dispatch
from tests.test_reminder_engine import seed_subscription

CAMPUS = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 27, 12, 0, 0, tzinfo=CAMPUS)   # 非静默
NIGHT = datetime(2026, 10, 27, 23, 30, 0, tzinfo=CAMPUS)  # 静默期


def make_settings(**kw) -> Settings:
    kw.setdefault("reminder_enabled", True)
    return Settings(**kw)


class FakePush:
    """假通道：记录 (student, channel, content)，可注入回执。"""

    def __init__(self, receipt: str = "sent") -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.receipt = receipt

    async def send(self, student_id: str, channel: str, content: str) -> PushReceipt:
        self.calls.append((student_id, channel, content))
        return PushReceipt(
            status=self.receipt, channel=channel, detail=f"fake-{self.receipt}"
        )

    async def aclose(self) -> None:
        return None


LIB_VARS = {"items": [{"title": "三体", "barcode": "01020", "due_date": date(2026, 10, 30)}]}
COURSE_VARS = {
    "course_name": "数据结构", "location": "敬贤楼201", "teacher": "王老师",
    "start": datetime(2026, 10, 27, 14, 0, tzinfo=CAMPUS),
    "is_changed": False, "change_note": "",
}


def make_candidate(
    *,
    sub_id: int = 1,
    student: str = "20230001",
    subject: str = "library_due",
    priority: str = "P2",
    timing: str = "T-3d",
    dedup_key: str = "k1",
) -> ReminderCandidate:
    return ReminderCandidate(
        subscription_id=sub_id,
        student_id=student,
        subject=subject,
        channel="wecom",
        priority=priority,
        timing=timing,
        trigger_at=NOON + timedelta(days=3),
        dedup_key=dedup_key,
        payload_vars=dict(COURSE_VARS if subject == "course" else LIB_VARS),
    )


async def get_dispatch_rows(factory) -> list[ReminderDispatch]:
    async with factory() as db:
        rows = await db.execute(select(ReminderDispatch))
        return list(rows.scalars().all())


@pytest_asyncio.fixture
async def session_factory():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# AC-5a 静默门
# ---------------------------------------------------------------------------


async def test_ac5a_quiet_skips_non_p0_without_slot(session_factory):
    """非 P0 静默期 → skipped_quiet 行（|quiet 后缀），不发送、不占真实坑。"""
    push = FakePush()
    stats = await dispatch(
        [make_candidate()],
        session_factory=session_factory, push=push,
        settings=make_settings(), now=NIGHT,
    )
    assert stats.skipped_quiet == 1 and not push.calls
    rows = await get_dispatch_rows(session_factory)
    assert len(rows) == 1 and rows[0].status == "skipped_quiet"
    assert rows[0].dedup_key.endswith(QUIET_SUFFIX)


async def test_ac5a_after_quiet_sends_normally(session_factory):
    """同一候选 07:00 后重放：干净 dedup_key 正常发送（跳过未占坑）。"""
    push = FakePush()
    stats = await dispatch(
        [make_candidate()],
        session_factory=session_factory, push=push,
        settings=make_settings(),
        now=datetime(2026, 10, 28, 7, 5, 0, tzinfo=CAMPUS),
    )
    assert stats.sent == 1 and len(push.calls) == 1


async def test_ac5a_p0_passes_quiet(session_factory):
    """P0 静默期照发（凌晨的调停课变更不能等）。"""
    push = FakePush()
    cand = make_candidate(priority="P0", subject="course", dedup_key="k-p0")
    cand = ReminderCandidate(
        **{**cand.__dict__, "payload_vars": {**COURSE_VARS, "is_changed": True, "change_note": "调至305"}}
    )
    stats = await dispatch(
        [cand], session_factory=session_factory, push=push,
        settings=make_settings(), now=NIGHT,
    )
    assert stats.sent == 1 and stats.skipped_quiet == 0
    assert push.calls[0][2].startswith("【调停课】")


# ---------------------------------------------------------------------------
# AC-5b claim 与幂等
# ---------------------------------------------------------------------------


async def test_ac5b_claim_send_and_terminal(session_factory):
    """首扫：claimed → sent，claimed_at / pushed_at 落值，is_degraded=False。"""
    push = FakePush(receipt="sent")
    await dispatch(
        [make_candidate()], session_factory=session_factory, push=push,
        settings=make_settings(), now=NOON,
    )
    rows = await get_dispatch_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].status == "sent" and rows[0].claimed_at is not None
    assert rows[0].pushed_at is not None and rows[0].is_degraded is False


async def test_ac5b_duplicate_claim_yields(session_factory):
    """二扫同 dedup_key：IntegrityError → 让路，不重发（duplicates 计数）。"""
    push = FakePush()
    args = dict(session_factory=session_factory, push=push, settings=make_settings())
    await dispatch([make_candidate()], now=NOON, **args)
    stats = await dispatch([make_candidate()], now=NOON + timedelta(minutes=5), **args)
    assert stats.duplicates == 1 and stats.sent == 0
    assert len(push.calls) == 1  # 只发过一次


# ---------------------------------------------------------------------------
# AC-5c CAS 认领自愈
# ---------------------------------------------------------------------------


async def seed_orphan_claim(factory, *, claimed_at: datetime, dedup_key: str = "k1") -> int:
    async with factory() as db:
        row = ReminderDispatch(
            subscription_id=1, student_id="20230001", dedup_key=dedup_key,
            dedup_date=date(2026, 10, 30), channel="wecom",
            status="claimed", claimed_at=claimed_at,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def test_ac5c_stale_orphan_taken_over(session_factory):
    """孤儿 claimed 行超阈值 → CAS 接管续发，dedup 身份不变。"""
    stale = NOON - timedelta(seconds=400)  # 阈值 300s
    row_id = await seed_orphan_claim(session_factory, claimed_at=stale)
    push = FakePush()
    stats = await dispatch(
        [make_candidate()], session_factory=session_factory, push=push,
        settings=make_settings(), now=NOON,
    )
    assert stats.sent == 1 and len(push.calls) == 1
    rows = await get_dispatch_rows(session_factory)
    assert len(rows) == 1 and rows[0].id == row_id and rows[0].status == "sent"


async def test_ac5c_fresh_orphan_yields(session_factory):
    """孤儿行锚新鲜（<300s）→ 另一实例在处理，让路不重发。"""
    fresh = NOON - timedelta(seconds=10)
    await seed_orphan_claim(session_factory, claimed_at=fresh)
    push = FakePush()
    stats = await dispatch(
        [make_candidate()], session_factory=session_factory, push=push,
        settings=make_settings(), now=NOON,
    )
    assert stats.duplicates == 1 and not push.calls


# ---------------------------------------------------------------------------
# AC-5d 频控
# ---------------------------------------------------------------------------


async def test_ac5d_quota_exceeded_marks_skipped(session_factory):
    """cap=1：批内第 2 条非 P0 → skipped_quota；P0 超额照发。"""
    await seed_subscription(session_factory, student_id="20230001")  # id=1, P2
    cands = [
        make_candidate(sub_id=1, dedup_key="k-a"),
        make_candidate(sub_id=1, dedup_key="k-b"),
    ]
    push = FakePush()
    stats = await dispatch(
        cands, session_factory=session_factory, push=push,
        settings=make_settings(reminder_daily_cap=1), now=NOON,
    )
    assert stats.sent == 1 and stats.skipped_quota == 1
    assert len(push.calls) == 1
    statuses = {r.dedup_key: r.status for r in await get_dispatch_rows(session_factory)}
    assert statuses == {"k-a": "sent", "k-b": "skipped_quota"}


async def test_ac5d_p0_not_counted(session_factory):
    """P0 不计入日频：cap=1 + 3 条 P0 全发。"""
    await seed_subscription(session_factory, student_id="20230001")
    cands = [make_candidate(sub_id=1, priority="P0", dedup_key=f"p0-{i}") for i in range(3)]
    push = FakePush()
    stats = await dispatch(
        cands, session_factory=session_factory, push=push,
        settings=make_settings(reminder_daily_cap=1), now=NOON,
    )
    assert stats.sent == 3 and stats.skipped_quota == 0


# ---------------------------------------------------------------------------
# AC-5e 聚合
# ---------------------------------------------------------------------------


def _course_candidate(dedup_key: str) -> ReminderCandidate:
    return make_candidate(sub_id=1, subject="course", timing="T-30min", dedup_key=dedup_key)


async def test_ac5e_same_student_merged(session_factory):
    """同学生同批 2 条非 P0 → 一条【提醒汇总】含两条文案。"""
    await seed_subscription(session_factory, student_id="20230001")
    push = FakePush()
    await dispatch(
        [make_candidate(sub_id=1, dedup_key="lib"), _course_candidate("course")],
        session_factory=session_factory, push=push,
        settings=make_settings(), now=NOON,
    )
    assert len(push.calls) == 1
    content = push.calls[0][2]
    assert content.startswith("【提醒汇总】共 2 条：")
    assert "【图书到期提醒】" in content and "【上课提醒】" in content


async def test_ac5e_p0_never_merged(session_factory):
    """P0 混批：P0 单发一条，非 P0 合一条，共 2 条消息。"""
    await seed_subscription(session_factory, student_id="20230001")
    push = FakePush()
    p0 = dataclasses.replace(_course_candidate("p0-course"), priority="P0")
    await dispatch(
        [p0, make_candidate(sub_id=1, dedup_key="lib")],
        session_factory=session_factory, push=push,
        settings=make_settings(), now=NOON,
    )
    assert len(push.calls) == 2
    contents = [c for _, _, c in push.calls]
    assert any(c.startswith("【上课提醒】") for c in contents)  # P0 单发
    assert any(c.startswith("【图书到期提醒】") for c in contents)  # 单条不聚合


# ---------------------------------------------------------------------------
# AC-5f 回执映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("receipt,expect_status,expect_degraded", [
    ("failed", "failed", False),
    ("degraded", "degraded", True),
])
async def test_ac5f_receipt_mapping(session_factory, receipt, expect_status, expect_degraded):
    push = FakePush(receipt=receipt)
    await dispatch(
        [make_candidate()], session_factory=session_factory, push=push,
        settings=make_settings(), now=NOON,
    )
    (row,) = await get_dispatch_rows(session_factory)
    assert row.status == expect_status and row.is_degraded is expect_degraded


# ---------------------------------------------------------------------------
# AC-5g 引擎端到端
# ---------------------------------------------------------------------------


class FakeEvaluator:
    """产出固定候选的求值器（绕开真实源适配器）。"""

    subjects = frozenset({"library_due"})

    def __init__(self, candidates: list[ReminderCandidate]) -> None:
        self.candidates = candidates

    async def evaluate(self, subs, now: datetime):
        return self.candidates


async def test_ac5g_engine_end_to_end_dispatch(session_factory):
    """scan_once 全链路：订阅 → 候选 → 四闸 → 桩通道收到真实 content。"""
    await seed_subscription(session_factory, student_id="20230001")  # library_due
    push = FakePush()
    cand = make_candidate(sub_id=1)
    eng = ReminderEngine(
        session_factory,
        evaluators=[FakeEvaluator([cand])],
        settings=make_settings(),
        push_adapter=push,
    )
    stats = await eng.scan_once()
    assert stats.candidates == 1 and stats.sent == 1 and stats.scan_errors == 0
    assert len(push.calls) == 1
    assert push.calls[0][2].startswith("【图书到期提醒】")


async def test_ac5g_no_push_adapter_safe_noop(session_factory):
    """无适配器：只求值不发送（安全空转语义保持）。"""
    await seed_subscription(session_factory, student_id="20230001")
    eng = ReminderEngine(
        session_factory,
        evaluators=[FakeEvaluator([make_candidate(sub_id=1)])],
        settings=make_settings(),
        push_adapter=None,
    )
    stats = await eng.scan_once()
    assert stats.candidates == 1 and stats.sent == 0
    assert (await get_dispatch_rows(session_factory)) == []


# ---------------------------------------------------------------------------
# AC-5h 频控基数跨批生效（2026-10-27 回归：双实体 Row 鸭子访问 AttributeError）
# 前置条件 = 同学生有订阅 + 当日已有 sent 行 —— 旧测试从未同时构造过这两者。
# ---------------------------------------------------------------------------


async def test_ac5h_base_counts_prior_sent_cap2_second_still_sent(session_factory):
    """同日已有 1 条 sent，cap=2 → 第二条（不同 dedup_key）基数被计入后仍照发。"""
    await seed_subscription(session_factory, student_id="20230001")  # id=1, P2
    push = FakePush()
    first = await dispatch(
        [make_candidate(dedup_key="k1")],
        session_factory=session_factory, push=push,
        settings=make_settings(reminder_daily_cap=2), now=NOON,
    )
    assert first.sent == 1

    second = await dispatch(
        [make_candidate(dedup_key="k2")],
        session_factory=session_factory, push=push,
        settings=make_settings(reminder_daily_cap=2), now=NOON,
    )

    assert second.sent == 1  # base=1 < cap=2， allowance=1 → 放行
    assert second.skipped_quota == 0 and second.duplicates == 0


async def test_ac5h_base_counts_prior_sent_cap1_second_skipped(session_factory):
    """同日已有 1 条 sent，cap=1 → 第二条基数被计入，标 skipped_quota。"""
    await seed_subscription(session_factory, student_id="20230001")
    push = FakePush()
    first = await dispatch(
        [make_candidate(dedup_key="k1")],
        session_factory=session_factory, push=push,
        settings=make_settings(reminder_daily_cap=1), now=NOON,
    )
    assert first.sent == 1

    second = await dispatch(
        [make_candidate(dedup_key="k2")],
        session_factory=session_factory, push=push,
        settings=make_settings(reminder_daily_cap=1), now=NOON,
    )

    assert second.sent == 0 and second.skipped_quota == 1
