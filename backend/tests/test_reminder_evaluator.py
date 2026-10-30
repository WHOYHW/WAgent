"""R3b 求值器接线测试（实施设计 11 §五：AC-2 / AC-10 / 快照日规则 / 隔离）。

覆盖：
- AC-2 窗口命中：借阅 T-3d/T-1d 各恰一候选，dedup_date = trigger_at.date()；
- AC-10 降级不出错数据：stale_cache / unavailable → 零候选，FRESH/LIVE_OK 放行；
- 课表快照日规则：{T-30min}→今天、{T-1d}→明天、{T-3d}→今天+3；
- 隔离：按学生（源故障/降级）与按订阅（脏 trigger_config）两层；
- 已开课抑制；引擎端到端（DB 订阅 → 求值器 → ScanStats）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from freezegun import freeze_time
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import make_engine
from app.orm import Base, ReminderSubscription
from app.reminder.engine import ReminderEngine
from app.reminder.evaluator import CAMPUS_TZ, CourseEvaluator, LibraryDueEvaluator
from app.schemas.library import LibraryLoanItem
from app.schemas.meta import AdapterResponse, DegradeStatus, ToolSource
from app.schemas.timetable import TimetableItem

# ---------------------------------------------------------------------------
# 桩与夹具
# ---------------------------------------------------------------------------


class FakeLibAdapter:
    """借阅快照桩：按学号返回可配置的 AdapterResponse（含降级注入）。"""

    source = ToolSource.LIBRARY_ILS

    def __init__(self) -> None:
        self.responses: dict[str, AdapterResponse | Exception] = {}
        self.calls: list[str] = []

    def set(self, student_id: str, status: DegradeStatus, loans=None, data=None):
        self.responses[student_id] = AdapterResponse(
            data=loans if loans is not None else data,
            degrade_status=status,
            synced_at=datetime.now(UTC),
            source=self.source,
        )

    async def fetch_loans(self, student_id, include_history=False):
        self.calls.append(student_id)
        resp = self.responses.get(student_id)
        if isinstance(resp, Exception):
            raise resp
        if resp is None:  # 未配置的学生 → 默认空 LIVE_OK
            return AdapterResponse(
                data=[], degrade_status=DegradeStatus.LIVE_OK,
                synced_at=datetime.now(UTC), source=self.source,
            )
        return resp


class FakeEduAdapter:
    """课表快照桩：记录 (student_id, start, end) 调用并按日期返回响应。"""

    source = ToolSource.EDU_SYSTEM

    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date]] = []
        self.responses: dict[tuple[str, date], AdapterResponse | Exception] = {}
        self.default = AdapterResponse(
            data=[], degrade_status=DegradeStatus.LIVE_OK,
            synced_at=datetime.now(UTC), source=self.source,
        )

    def set(self, student_id: str, snapshot: date, status: DegradeStatus, items=None):
        self.responses[(student_id, snapshot)] = AdapterResponse(
            data=items,
            degrade_status=status,
            synced_at=datetime.now(UTC),
            source=self.source,
        )

    async def fetch_timetable(self, student_id, start_date, end_date, course_name=None):
        self.calls.append((student_id, start_date, end_date))
        resp = self.responses.get((student_id, start_date), self.default)
        if isinstance(resp, Exception):
            raise resp
        return resp


def make_settings() -> Settings:
    return Settings(
        reminder_enabled=True,
        reminder_scan_interval_seconds=300.0,  # × horizon_ticks=2 → 窗口 10min
        reminder_horizon_ticks=2,
    )


def make_loan(due: date, barcode: str = "B001", overdue_days: int = 0) -> LibraryLoanItem:
    return LibraryLoanItem(
        title="高等数学（上册）", barcode=barcode, due_date=due,
        overdue_days=overdue_days, renewable=True,
    )


def make_item(period_start: int = 3) -> TimetableItem:
    return TimetableItem(
        course_name="数据结构", course_code="CS201", teacher="李老师",
        location="二教-203", weekday=1, period_start=period_start,
        period_end=period_start, weeks="1-16 周",
    )


def make_sub(student_id: str, subject: str, timing: list[str], sub_id: int = 1):
    """鸭子类型订阅（免 DB 的轻量单元测试用）。"""

    class Sub:
        pass

    s = Sub()
    s.id = sub_id
    s.student_id = student_id
    s.subject = subject
    s.enabled = True
    s.channel = "wecom"
    s.priority = "P2"
    s.trigger_config = {"timing": timing}
    return s


@pytest_asyncio.fixture
async def session_factory():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# LibraryDueEvaluator（AC-2 / AC-10 / 隔离）
# ---------------------------------------------------------------------------

# 校园时区下 2026-10-26 08:00:30（借阅 T-3d 锚点：due-3 日的 08:00 落窗）
NOW = datetime(2026, 10, 26, 8, 0, 30, tzinfo=CAMPUS_TZ)


async def test_ac2_library_due_window_hit():
    """T-3d 命中：恰一候选，dedup_date = trigger_at.date()（窗口归属日）。"""
    lib = FakeLibAdapter()
    lib.set("20230001", DegradeStatus.LIVE_OK, loans=[make_loan(date(2026, 10, 29))])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())
    subs = [make_sub("20230001", "library_due", ["T-3d"])]

    cands = await ev.evaluate(subs, NOW)

    assert len(cands) == 1
    c = cands[0]
    assert c.timing == "T-3d"
    assert c.trigger_at == datetime(2026, 10, 26, 8, 0, tzinfo=CAMPUS_TZ)
    assert c.dedup_date == date(2026, 10, 26)  # = trigger_at.date()，非截止日
    assert c.payload_vars["items"][0]["barcode"] == "B001"


async def test_ac2_library_two_timings_two_candidates():
    """T-3d 与 T-1d 各自命中：两个独立候选（不同 dedup 身份）。"""
    lib = FakeLibAdapter()
    lib.set("20230001", DegradeStatus.LIVE_OK, loans=[make_loan(date(2026, 10, 27))])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())
    subs = [make_sub("20230001", "library_due", ["T-3d", "T-1d"])]

    cands = await ev.evaluate(subs, NOW)  # T-1d 锚点 = 今天 08:00，T-3d 锚点已过窗

    assert len(cands) == 1
    assert cands[0].timing == "T-1d"
    assert cands[0].dedup_date == date(2026, 10, 26)


async def test_library_overdue_loan_skipped():
    """超期借阅不产出（阈值型目录 #3，非本求值器范围）。"""
    lib = FakeLibAdapter()
    lib.set("20230001", DegradeStatus.LIVE_OK, loans=[make_loan(date(2026, 10, 20), overdue_days=6)])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())

    assert await ev.evaluate([make_sub("20230001", "library_due", ["T-3d"])], NOW) == []


@pytest.mark.parametrize("status", [DegradeStatus.STALE_CACHE, DegradeStatus.UNAVAILABLE])
async def test_ac10_library_degraded_zero_candidates(status):
    """AC-10：stale_cache / unavailable → 零候选（宁可不发，不发错误数据）。"""
    lib = FakeLibAdapter()
    lib.set("20230001", status, loans=[make_loan(date(2026, 10, 29))])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())

    assert await ev.evaluate([make_sub("20230001", "library_due", ["T-3d"])], NOW) == []


async def test_ac10_library_fresh_cache_allowed():
    """FRESH（未过期缓存命中）放行，照常产出。"""
    lib = FakeLibAdapter()
    lib.set("20230001", DegradeStatus.FRESH, loans=[make_loan(date(2026, 10, 29))])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())

    assert len(await ev.evaluate([make_sub("20230001", "library_due", ["T-3d"])], NOW)) == 1


async def test_library_isolation_by_student():
    """按学生隔离：A 生快照降级，B 生照常产出。"""
    lib = FakeLibAdapter()
    lib.set("A", DegradeStatus.UNAVAILABLE)
    lib.set("B", DegradeStatus.LIVE_OK, loans=[make_loan(date(2026, 10, 29))])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())
    subs = [
        make_sub("A", "library_due", ["T-3d"], sub_id=1),
        make_sub("B", "library_due", ["T-3d"], sub_id=2),
    ]

    cands = await ev.evaluate(subs, NOW)

    assert [c.student_id for c in cands] == ["B"]


async def test_library_isolation_by_subscription():
    """按订阅隔离：同学生一脏订阅被跳过，其余订阅产出。"""
    lib = FakeLibAdapter()
    lib.set("20230001", DegradeStatus.LIVE_OK, loans=[make_loan(date(2026, 10, 29))])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())
    subs = [
        make_sub("20230001", "library_due", ["3天前"], sub_id=1),  # 脏 timing（历史库数据）
        make_sub("20230001", "library_due", ["T-3d"], sub_id=2),
    ]

    cands = await ev.evaluate(subs, NOW)  # 不抛出

    assert len(cands) == 1
    assert cands[0].subscription_id == 2


async def test_library_isolation_snapshot_exception():
    """快照适配器抛意外异常：该学生跳过，不向上抛出。"""
    lib = FakeLibAdapter()
    lib.responses["A"] = RuntimeError("boom")
    lib.set("B", DegradeStatus.LIVE_OK, loans=[make_loan(date(2026, 10, 29))])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())

    cands = await ev.evaluate(
        [make_sub("A", "library_due", ["T-3d"]), make_sub("B", "library_due", ["T-3d"])], NOW
    )
    assert [c.student_id for c in cands] == ["B"]


async def test_evaluate_rejects_naive_now():
    """naive now 显式拒绝（时区归一契约）。"""
    lib = FakeLibAdapter()
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())
    with pytest.raises(ValueError):
        await ev.evaluate([], datetime(2026, 10, 26, 8, 0))  # noqa: DTZ001


# ---------------------------------------------------------------------------
# CourseEvaluator（快照日规则 / 已开课抑制 / AC-10）
# ---------------------------------------------------------------------------


async def test_course_snapshot_rule_by_timing_granularity():
    """快照日规则：{T-30min}→今天、{T-1d}→明天、{T-3d}→今天+3。"""
    edu = FakeEduAdapter()
    ev = CourseEvaluator(edu=edu, settings=make_settings())
    today = NOW.date()
    subs = [
        make_sub("20230001", "course", ["T-30min"], sub_id=1),
        make_sub("20230002", "course", ["T-1d"], sub_id=2),
        make_sub("20230003", "course", ["T-3d"], sub_id=3),
    ]

    await ev.evaluate(subs, NOW)

    got = {(sid, s, e) for sid, s, e in edu.calls}
    assert ("20230001", today, today) in got
    assert ("20230002", today + timedelta(days=1), today + timedelta(days=1)) in got
    assert ("20230003", today + timedelta(days=3), today + timedelta(days=3)) in got


async def test_course_window_hit_today_class():
    """T-30min 命中：今天 10:00 的课，now=09:25 落窗，恰一候选。"""
    edu = FakeEduAdapter()
    edu.set("20230001", NOW.date(), DegradeStatus.LIVE_OK, items=[make_item(period_start=3)])
    ev = CourseEvaluator(edu=edu, settings=make_settings())
    now = datetime(2026, 10, 26, 9, 25, tzinfo=CAMPUS_TZ)

    cands = await ev.evaluate([make_sub("20230001", "course", ["T-30min"])], now)

    assert len(cands) == 1
    c = cands[0]
    assert c.trigger_at == datetime(2026, 10, 26, 9, 30, tzinfo=CAMPUS_TZ)
    assert c.payload_vars["course_name"] == "数据结构"


async def test_course_started_class_suppressed():
    """已开课（start ≤ now）不再产出——迟到噪音抑制。"""
    edu = FakeEduAdapter()
    edu.set("20230001", NOW.date(), DegradeStatus.LIVE_OK, items=[make_item(period_start=3)])
    ev = CourseEvaluator(edu=edu, settings=make_settings())
    now = datetime(2026, 10, 26, 10, 5, tzinfo=CAMPUS_TZ)  # 课 10:00 已开

    assert await ev.evaluate([make_sub("20230001", "course", ["T-30min"])], now) == []


@pytest.mark.parametrize("status", [DegradeStatus.STALE_CACHE, DegradeStatus.UNAVAILABLE])
async def test_ac10_course_degraded_zero_candidates(status):
    """AC-10（课表侧）：降级快照 → 零候选 + degraded 日志。"""
    edu = FakeEduAdapter()
    edu.set("20230001", NOW.date(), status, items=[make_item()])
    ev = CourseEvaluator(edu=edu, settings=make_settings())

    assert await ev.evaluate([make_sub("20230001", "course", ["T-30min"])], NOW) == []


async def test_course_dirty_timing_skips_sub_only():
    """按订阅隔离（课表侧）：脏 timing 跳过该订阅，不毒化同批其他订阅。"""
    edu = FakeEduAdapter()
    edu.set("20230001", NOW.date(), DegradeStatus.LIVE_OK, items=[make_item()])
    ev = CourseEvaluator(edu=edu, settings=make_settings())
    subs = [
        make_sub("20230001", "course", ["半小时前"], sub_id=1),  # 脏
        make_sub("20230001", "course", ["T-30min"], sub_id=2),
    ]
    now = datetime(2026, 10, 26, 9, 25, tzinfo=CAMPUS_TZ)  # 10:00 的课 T-30min 落窗

    cands = await ev.evaluate(subs, now)  # 不抛出

    assert len(cands) == 1 and cands[0].subscription_id == 2


async def test_course_tomorrow_snapshot_via_t1d_produces_candidate():
    """{T-1d} 拉明天快照：明天 8:00 的课 trigger = 今天 08:00，落窗产出。"""
    edu = FakeEduAdapter()
    tomorrow = NOW.date() + timedelta(days=1)
    edu.set("20230001", tomorrow, DegradeStatus.LIVE_OK, items=[make_item(period_start=1)])
    ev = CourseEvaluator(edu=edu, settings=make_settings())

    cands = await ev.evaluate([make_sub("20230001", "course", ["T-1d"])], NOW)

    assert len(cands) == 1
    assert cands[0].trigger_at == datetime(2026, 10, 26, 8, 0, tzinfo=CAMPUS_TZ)


# ---------------------------------------------------------------------------
# 引擎端到端（R3a 骨架 × R3b 求值器）
# ---------------------------------------------------------------------------


async def seed_subscription(factory, student_id: str, subject: str, timing: list[str]):
    async with factory() as db:
        db.add(
            ReminderSubscription(
                student_id=student_id, subject=subject, trigger_type="time",
                trigger_config={"timing": timing}, channel="wecom",
                priority="P2", enabled=True,
            )
        )
        await db.commit()


@freeze_time("2026-10-26 00:00:30")  # = 校园时区 08:00:30，借阅 T-3d 锚点落窗
async def test_engine_scan_once_end_to_end(session_factory):
    """端到端：DB 订阅 → 引擎分发 → 求值器 → 候选计数进 ScanStats。

    freezegun 冻结引擎的 ``datetime.now``（UTC）——换算校园时区恰为
    08:00:30，due=10-29 的 T-3d 锚点（10-26 08:00）落在 ±10min 窗口内。
    """
    await seed_subscription(session_factory, "20230001", "library_due", ["T-3d"])
    lib = FakeLibAdapter()
    lib.set("20230001", DegradeStatus.LIVE_OK, loans=[make_loan(date(2026, 10, 29))])
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())
    eng = ReminderEngine(session_factory, evaluators=[ev], settings=make_settings())

    stats = await eng.scan_once()

    assert stats.subscriptions == 1
    assert stats.candidates == 1
    assert stats.evaluator_errors == 0 and stats.scan_errors == 0


async def test_engine_scan_once_degraded_source_no_candidates(session_factory):
    """端到端（AC-10）：源降级 → 引擎仍正常扫描，candidates=0。"""
    await seed_subscription(session_factory, "20230001", "library_due", ["T-3d"])
    lib = FakeLibAdapter()
    lib.set("20230001", DegradeStatus.UNAVAILABLE)
    ev = LibraryDueEvaluator(lib=lib, settings=make_settings())
    eng = ReminderEngine(session_factory, evaluators=[ev], settings=make_settings())

    stats = await eng.scan_once()

    assert stats.subscriptions == 1
    assert stats.candidates == 0
    assert stats.evaluator_errors == 0  # 降级不是异常：正常路径内的"零候选"
