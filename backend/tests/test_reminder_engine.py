"""R3a 心跳骨架测试（实施设计 10 §五：AC-1 + scan_once 管线 + 抖动边界）。

覆盖：
- AC-1a enabled=false 不启动；AC-1b start 幂等；AC-1c stop 幂等 + 可重启；
- AC-1d shutdown 后 asyncio.all_tasks() 无悬挂；
- AC-1e 求值器/会话异常不杀心跳；
- scan_once 管线：独立会话拉 enabled 订阅、按 subject 分发、ScanStats 计数；
- _jittered_interval 边界（±jitter、零抖动、下限 clamp）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import make_engine
from app.orm import Base, ReminderSubscription
from app.reminder.candidates import ReminderCandidate
from app.reminder.engine import (
    HEARTBEAT_TASK_NAME,
    ReminderEngine,
    ReminderEvaluator,
    ScanStats,
)

FAST = 0.05  # 测试用扫描间隔（秒）


def make_settings(**kw) -> Settings:
    kw.setdefault("reminder_enabled", True)
    kw.setdefault("reminder_scan_interval_seconds", FAST)
    return Settings(**kw)


class ExplodingSessionFactory:
    """每次调用都炸的会话工厂（会话级故障注入），带调用计数。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise RuntimeError("db down")


class FakeEvaluator:
    """假求值器：记录收到的订阅，可注入异常与候选数（AC-11 通道抽象同源思路）。"""

    subjects = frozenset({"library_due"})

    def __init__(self, error: Exception | None = None, n_candidates: int = 1) -> None:
        self.calls: list[list] = []
        self.error = error
        self.n_candidates = n_candidates

    async def evaluate(self, subs, now: datetime):
        self.calls.append(list(subs))
        if self.error is not None:
            raise self.error
        out = []
        for s in subs[: self.n_candidates]:
            out.append(
                ReminderCandidate(
                    subscription_id=s.id,
                    student_id=s.student_id,
                    subject=s.subject,
                    channel=s.channel,
                    priority=s.priority,
                    timing="T-3d",
                    trigger_at=now + timedelta(days=3),
                    dedup_key=f"k-{s.id}",
                )
            )
        return out


async def seed_subscription(
    factory,
    *,
    student_id: str = "20230001",
    subject: str = "library_due",
    enabled: bool = True,
) -> int:
    """插入一条订阅，返回 id。"""
    async with factory() as db:
        row = ReminderSubscription(
            student_id=student_id,
            subject=subject,
            trigger_type="time",
            trigger_config={"timing": ["T-3d"]},
            channel="wecom",
            priority="P2",
            enabled=enabled,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


@pytest_asyncio.fixture
async def session_factory():
    """函数级内存库会话工厂（独立引擎，与 conftest 的 db 夹具隔离）。"""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _heartbeat_tasks() -> set:
    return {t for t in asyncio.all_tasks() if t.get_name() == HEARTBEAT_TASK_NAME}


# ---------------------------------------------------------------------------
# AC-1 生命周期
# ---------------------------------------------------------------------------


async def test_ac1a_disabled_engine_never_starts(session_factory):
    """enabled=false：start() 不建任务，all_tasks() 无心跳残留。"""
    eng = ReminderEngine(
        session_factory, settings=make_settings(reminder_enabled=False)
    )
    await eng.start()
    assert eng._task is None
    assert not _heartbeat_tasks()


async def test_ac1b_start_is_idempotent(session_factory):
    """二次 start() 复用同一任务，不新建。"""
    eng = ReminderEngine(session_factory, settings=make_settings())
    await eng.start()
    first = eng._task
    assert first is not None
    await eng.start()
    assert eng._task is first
    assert len(_heartbeat_tasks()) == 1
    await eng.stop()


async def test_ac1c_stop_idempotent_and_restartable(session_factory):
    """未启动直接 stop 不抛；二次 stop 不抛；stop→start 可重建。"""
    eng = ReminderEngine(session_factory, settings=make_settings())
    await eng.stop()  # 未启动：no-op
    await eng.start()
    first = eng._task
    await eng.stop()
    await eng.stop()  # 已停：no-op
    assert eng._task is None
    await eng.start()
    assert eng._task is not None and eng._task is not first
    await eng.stop()


async def test_ac1d_shutdown_no_dangling_tasks(session_factory):
    """stop 后任务已取消，asyncio.all_tasks() 无心跳悬挂。"""
    eng = ReminderEngine(session_factory, settings=make_settings())
    await eng.start()
    assert len(_heartbeat_tasks()) == 1
    task = eng._task
    await eng.stop()
    assert eng._task is None
    assert task is not None and task.done() and task.cancelled()
    assert not _heartbeat_tasks()


# ---------------------------------------------------------------------------
# AC-1e / 风险③：异常不杀心跳
# ---------------------------------------------------------------------------


async def test_loop_survives_evaluator_failure(session_factory):
    """求值器持续抛异常：loop 仍按 tick 扫描（scan_once 吞掉，心跳不死）。"""
    await seed_subscription(factory=session_factory, subject="library_due", enabled=True)
    ev = FakeEvaluator(error=RuntimeError("boom"))
    eng = ReminderEngine(session_factory, evaluators=[ev], settings=make_settings())
    await eng.start()
    await asyncio.sleep(FAST * 5)
    await eng.stop()
    assert len(ev.calls) >= 2  # 多 tick 后仍在扫描


async def test_loop_survives_session_failure():
    """会话工厂持续炸（db down）：心跳不死，tick 继续推进。"""
    factory = ExplodingSessionFactory()
    eng = ReminderEngine(factory, settings=make_settings())  # type: ignore[arg-type]
    await eng.start()
    await asyncio.sleep(FAST * 5)
    await eng.stop()
    assert factory.calls >= 2


# ---------------------------------------------------------------------------
# scan_once 管线
# ---------------------------------------------------------------------------


async def test_scan_once_pulls_enabled_subs_and_feeds_evaluator(session_factory):
    """拉 enabled 订阅 → 按 subject 分发 → ScanStats 计数正确。"""
    await seed_subscription(factory=session_factory, subject="library_due", enabled=True)
    await seed_subscription(
        factory=session_factory, student_id="20230002", subject="library_due", enabled=True
    )
    await seed_subscription(
        factory=session_factory, student_id="20230003", subject="library_due", enabled=False
    )
    await seed_subscription(
        factory=session_factory, student_id="20230004", subject="course", enabled=True
    )
    ev = FakeEvaluator(n_candidates=2)
    eng = ReminderEngine(session_factory, evaluators=[ev], settings=make_settings())

    stats = await eng.scan_once()

    assert isinstance(stats, ScanStats)
    assert stats.subscriptions == 3  # 3 条 enabled（library_due×2 + course×1）
    assert stats.scan_errors == 0 and stats.evaluator_errors == 0
    assert stats.finished_at is not None and stats.duration_ms is not None
    # 只喂 enabled 且 subject 匹配的订阅（disabled 的 20230003 与 course 均不进）
    assert len(ev.calls) == 1
    fed = ev.calls[0]
    assert {s.student_id for s in fed} == {"20230001", "20230002"}
    assert stats.candidates == 2  # n_candidates=2 → 每次调用产 2 条


async def test_scan_once_evaluator_error_isolated(session_factory):
    """单求值器抛异常：计 evaluator_errors，其余求值器与本 tick 继续。"""
    await seed_subscription(factory=session_factory, subject="library_due", enabled=True)
    bad = FakeEvaluator(error=CandidateGenerationLikeError())
    good = FakeEvaluator(n_candidates=1)
    eng = ReminderEngine(
        session_factory, evaluators=[bad, good], settings=make_settings()
    )

    stats = await eng.scan_once()  # 不抛

    assert stats.evaluator_errors == 1
    assert stats.scan_errors == 0
    assert stats.candidates == 1
    assert len(good.calls) == 1


class CandidateGenerationLikeError(ValueError):
    """模拟 R3b 的 CandidateGenerationError（脏订阅毒不扩散）。"""


async def test_scan_once_session_failure_returns_stats():
    """会话级故障：计 scan_errors，返回 stats，不抛出。"""
    factory = ExplodingSessionFactory()
    eng = ReminderEngine(factory, settings=make_settings())  # type: ignore[arg-type]
    stats = await eng.scan_once()
    assert stats.scan_errors == 1
    assert stats.subscriptions == 0 and stats.candidates == 0


async def test_scan_once_no_evaluators_is_pure_read(session_factory):
    """evaluators 空集：纯读空转，只统计订阅数，零写副作用。"""
    await seed_subscription(factory=session_factory, subject="course", enabled=True)
    eng = ReminderEngine(session_factory, settings=make_settings())
    stats = await eng.scan_once()
    assert stats.subscriptions == 1
    assert stats.candidates == 0


# ---------------------------------------------------------------------------
# 抖动边界
# ---------------------------------------------------------------------------


def test_jittered_interval_bounds():
    """±10% 抖动落在 [0.9i, 1.1i]。"""
    settings = make_settings(reminder_scan_interval_seconds=300.0)
    eng = ReminderEngine(None, settings=settings, jitter_ratio=0.1)  # type: ignore[arg-type]
    samples = [eng._jittered_interval() for _ in range(200)]
    assert all(300.0 * 0.9 <= v <= 300.0 * 1.1 for v in samples)
    assert min(samples) < 300.0 and max(samples) > 300.0  # 确有抖动


def test_jittered_interval_zero_ratio_is_exact():
    settings = make_settings(reminder_scan_interval_seconds=300.0)
    eng = ReminderEngine(None, settings=settings, jitter_ratio=0.0)  # type: ignore[arg-type]
    assert all(eng._jittered_interval() == 300.0 for _ in range(50))


def test_jittered_interval_floor_clamp():
    """interval 趋零时命中 0.01s 下限，不退化为热循环。"""
    settings = make_settings(reminder_scan_interval_seconds=0.001)
    eng = ReminderEngine(None, settings=settings, jitter_ratio=0.5)  # type: ignore[arg-type]
    assert all(eng._jittered_interval() >= 0.01 for _ in range(50))


# 类型契约自检：FakeEvaluator 满足 ReminderEvaluator Protocol（runtime_checkable）
def test_evaluator_protocol_conformance():
    assert isinstance(FakeEvaluator(), ReminderEvaluator)
