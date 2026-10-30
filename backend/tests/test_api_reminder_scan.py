"""R3c 手动触发调试面测试（实施设计 12 §五：AC-3a ~ 3d）。

复用 test_api_chat 的 HTTP 基建（ASGITransport 不跑 lifespan，规避 init_db
触碰 PG）与 test_reminder_engine 的内存库 + 种子辅助。分层原则：本层只测
「端点装配 + 鉴权 + 序列化」，引擎/求值器内部逻辑已由既有测试覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import make_engine
from app.main import _reminder_engine, app
from app.orm import Base
from app.reminder.engine import ReminderEngine, ScanStats
from app.security import encode_token
from tests.test_reminder_engine import seed_subscription

STUDENT = "20230001"
AUTH = {"Authorization": f"Bearer {encode_token(STUDENT)}"}


# ---- 夹具与辅助 ----


@pytest_asyncio.fixture
async def client():
    """ASGI 测试客户端（不跑 lifespan）；退出清依赖覆盖，隔离用例。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    app.dependency_overrides.clear()


class FakeEngine:
    """假引擎：返回预设 ScanStats，带调用计数（AC-3a 断言"未被触碰"）。"""

    def __init__(self, stats: ScanStats) -> None:
        self.stats = stats
        self.calls = 0

    async def scan_once(self) -> ScanStats:
        self.calls += 1
        return self.stats


def make_stats(**kw) -> ScanStats:
    """构造带 finished_at 的完整统计（duration_ms 可计算）。"""
    started = kw.pop(
        "started_at", datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)
    )
    return ScanStats(started_at=started, **kw)


@pytest_asyncio.fixture
async def patch_fake_engine():
    """替换模块级引擎为 FakeEngine（stats 注入），退出时还原真实单例。"""
    import app.main as m

    holders: dict = {}

    def install(stats: ScanStats) -> FakeEngine:
        fake = FakeEngine(stats)
        holders["fake"] = fake
        m._reminder_engine = fake
        return fake

    yield install

    # 还原真实引擎（ASGITransport 不跑 lifespan，模块级单例必须手动复位）
    m._reminder_engine = _reminder_engine


# ---------------------------------------------------------------------------
# AC-3a 鉴权
# ---------------------------------------------------------------------------


async def test_ac3a_no_token_401_and_engine_untouched(client, patch_fake_engine):
    """无令牌 → 401，且 FakeEngine 一次都没被调（鉴权在业务之前）。"""
    fake = patch_fake_engine(make_stats(subscriptions=3, candidates=1))

    resp = await client.post("/agent/reminders/scan")

    assert resp.status_code == 401
    assert fake.calls == 0


# ---------------------------------------------------------------------------
# AC-3b 统计透传
# ---------------------------------------------------------------------------


async def test_ac3b_stats_passthrough(client, patch_fake_engine):
    """带令牌 → 200，响应与 FakeEngine 注入的统计逐字段一致，恰调用 1 次。"""
    stats = make_stats(
        finished_at=datetime(2026, 10, 27, 8, 0, 0, 333000, tzinfo=UTC),
        subscriptions=3,
        candidates=1,
        evaluator_errors=0,
        scan_errors=0,
    )
    fake = patch_fake_engine(stats)

    resp = await client.post("/agent/reminders/scan", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "started_at": "2026-10-27T08:00:00+00:00",
        "finished_at": "2026-10-27T08:00:00.333000+00:00",
        "subscriptions": 3,
        "candidates": 1,
        "evaluator_errors": 0,
        "scan_errors": 0,
        "sent": 0,
        "failed": 0,
        "degraded": 0,
        "skipped_quiet": 0,
        "skipped_quota": 0,
        "duplicates": 0,
        "duration_ms": 333,
    }
    assert fake.calls == 1


# ---------------------------------------------------------------------------
# AC-3c 真实引擎端到端（内存库）
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """函数级内存库会话工厂（与 test_reminder_engine 同构）。"""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_ac3c_real_engine_end_to_end(client, session_factory):
    """真实引擎 + 内存库：只数 enabled 订阅，空求值器纯读空转不报错。"""
    await seed_subscription(session_factory, student_id="20230001", enabled=True)
    await seed_subscription(
        session_factory, student_id="20230002", subject="course", enabled=False
    )
    real = ReminderEngine(
        session_factory, evaluators=(), settings=Settings(reminder_enabled=False)
    )
    import app.main as m

    original = m._reminder_engine
    m._reminder_engine = real
    try:
        resp = await client.post("/agent/reminders/scan", headers=AUTH)
    finally:
        m._reminder_engine = original

    assert resp.status_code == 200
    body = resp.json()
    assert body["subscriptions"] == 1  # disabled 不计入
    assert body["candidates"] == 0  # 空求值器：纯读空转
    assert body["scan_errors"] == 0 and body["evaluator_errors"] == 0
    assert body["duration_ms"] is not None


# ---------------------------------------------------------------------------
# AC-3d ScanStats.to_dict 纯单测
# ---------------------------------------------------------------------------


def test_ac3d_to_dict_incomplete_stats():
    """finished_at=None：duration_ms 与 finished_at 均为 None，不抛异常。"""
    stats = ScanStats(started_at=datetime(2026, 10, 27, tzinfo=UTC))
    d = stats.to_dict()
    assert set(d) == {
        "started_at",
        "finished_at",
        "subscriptions",
        "candidates",
        "evaluator_errors",
        "scan_errors",
        "sent",
        "failed",
        "degraded",
        "skipped_quiet",
        "skipped_quota",
        "duplicates",
        "duration_ms",
    }
    assert d["finished_at"] is None and d["duration_ms"] is None
