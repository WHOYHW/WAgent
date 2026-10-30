"""R5.3 效果统计测试（实施设计 15 §七：口径定版）。

纯函数逐口径用例 + GET /agent/reminders/stats 调试面（复用 R3c 基建：
ASGITransport 不跑 lifespan，get_db 依赖覆盖注入内存库）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import make_engine
from app.main import app
from app.orm import Base, ReminderDispatch
from app.reminder.stats import loss_avoidance_stats
from tests.test_api_reminder_scan import AUTH  # 同款令牌（学生 20230001）

NOW = datetime(2026, 10, 28, 8, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 纯函数：口径逐项锁定
# ---------------------------------------------------------------------------


def row(**kw) -> SimpleNamespace:
    """dispatch 行鸭子替身：status/opened_at/action_taken/is_degraded。"""
    kw.setdefault("status", "sent")
    kw.setdefault("opened_at", None)
    kw.setdefault("action_taken", None)
    kw.setdefault("is_degraded", False)
    return SimpleNamespace(**kw)


def test_stats_empty_rows_zero_without_error():
    """空表 → 全零 + 比率 0.0（不抛除零，空库可演示）。"""
    assert loss_avoidance_stats([]) == {
        "sent": 0, "opened": 0, "acted": 0,
        "opened_rate": 0.0, "action_rate": 0.0,
    }


def test_stats_denominator_counts_only_sent_primary_rows():
    """分母 = sent 且非重发行：skipped/failed/degraded/重发行全剔除。"""
    rows = [
        row(),                                          # sent 主行（分母）
        row(status="skipped_quota"),                    # 主动不打扰，剔除
        row(status="skipped_quiet"),                    # 同上
        row(status="failed"),                           # 未触达，剔除
        row(status="degraded"),                         # 未触达，剔除
        row(is_degraded=True),                          # 重发行，防双计剔除
    ]
    assert loss_avoidance_stats(rows)["sent"] == 1


def test_stats_opened_and_acted_rates():
    """4 条主行：2 条 opened、1 条 renewed → opened_rate=0.5、action_rate=0.25。"""
    rows = [
        row(opened_at=NOW),                                  # 只打开
        row(opened_at=NOW, action_taken="viewed"),           # 打开+ viewed（不计 acted）
        row(opened_at=NOW, action_taken="renewed"),          # 行动
        row(),                                               # 沉默
    ]
    d = loss_avoidance_stats(rows)
    assert d == {
        "sent": 4, "opened": 3, "acted": 1,
        "opened_rate": 0.75, "action_rate": 0.25,
    }


def test_stats_viewed_not_counted_as_acted():
    """acted 只认 renewed/returned（viewed 是打开不是规避损失的行动）。"""
    rows = [row(action_taken="viewed"), row(action_taken="returned")]
    d = loss_avoidance_stats(rows)
    assert d["acted"] == 1 and d["opened"] == 0  # opened 独立看 opened_at


# ---------------------------------------------------------------------------
# 调试面：GET /agent/reminders/stats
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory):
    """ASGI 客户端 + get_db 覆盖注入内存库（退出还原，隔离用例）。"""
    from app.db import get_db

    async def override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    app.dependency_overrides.clear()


async def test_stats_endpoint_no_token_401(client):
    resp = await client.get("/agent/reminders/stats")
    assert resp.status_code == 401


async def test_stats_endpoint_empty_db(client):
    """带令牌 + 空库 → 200 全零口径（G6/AC-12 可演示入口）。"""
    resp = await client.get("/agent/reminders/stats", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {
        "sent": 0, "opened": 0, "acted": 0,
        "opened_rate": 0.0, "action_rate": 0.0,
    }


async def test_stats_endpoint_seeded_rows(client, session_factory):
    """内存库种 3 行（1 sent 已行动 / 1 skipped_quota / 1 failed）→ 只按口径计。"""
    async with session_factory() as db:
        db.add_all([
            ReminderDispatch(
                subscription_id=1, student_id="20230001",
                dedup_key="k1", dedup_date=NOW.date(), channel="wecom",
                status="sent", pushed_at=NOW, opened_at=NOW, action_taken="renewed",
            ),
            ReminderDispatch(
                subscription_id=1, student_id="20230001",
                dedup_key="k2", dedup_date=NOW.date(), channel="wecom",
                status="skipped_quota",
            ),
            ReminderDispatch(
                subscription_id=1, student_id="20230001",
                dedup_key="k3", dedup_date=NOW.date(), channel="wecom",
                status="failed",
            ),
        ])
        await db.commit()

    resp = await client.get("/agent/reminders/stats", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] == 1 and body["acted"] == 1 and body["opened"] == 1
    assert body["action_rate"] == 1.0 and body["opened_rate"] == 1.0
