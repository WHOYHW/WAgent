"""R5.4 效果回流测试（4 组，实施设计 15 §五 AC-R5.1~10 + AC-12 闭环）。

复用 R3c/R5.3 基建：ASGITransport 不跑 lifespan + get_db 依赖覆盖注入内存库。
端点：POST /agent/reminders/feedback（R5.2）× GET /agent/reminders/stats（R5.3）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import get_db, make_engine
from app.main import app
from app.orm import Base, ReminderDispatch
from app.security import encode_token

NOW = datetime(2026, 10, 28, 8, 0, 0, tzinfo=UTC)
OWNER = "20230001"
OTHER = "20230002"
AUTH = {"Authorization": f"Bearer {encode_token(OWNER)}"}
OTHER_AUTH = {"Authorization": f"Bearer {encode_token(OTHER)}"}


# ---- 夹具与种子辅助 ----


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
    async def override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    app.dependency_overrides.clear()


async def seed_dispatch(
    factory,
    *,
    dispatch_id: int = 1,
    student_id: str = OWNER,
    status: str = "sent",
    **kw,
) -> None:
    """种一条 dispatch 行（默认 sent 主行，id 由调用方指定便于 404 用例）。"""
    from datetime import date

    async with factory() as db:
        db.add(
            ReminderDispatch(
                id=dispatch_id,
                subscription_id=1,
                student_id=student_id,
                dedup_key=f"k{dispatch_id}",
                dedup_date=date(2026, 10, 28),
                channel="wecom",
                status=status,
                pushed_at=NOW if status == "sent" else None,
                **kw,
            )
        )
        await db.commit()


async def get_row(factory, dispatch_id: int = 1) -> ReminderDispatch:
    async with factory() as db:
        row = await db.execute(
            select(ReminderDispatch).where(ReminderDispatch.id == dispatch_id)
        )
        return row.scalar_one()


async def feedback(client, dispatch_id: int, action: str, headers: dict = AUTH):
    return await client.post(
        "/agent/reminders/feedback",
        json={"dispatch_id": dispatch_id, "action": action},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 组一 归属：令牌身份 ≠ 行归属 → 404 且无写入；不存在 → 404
# ---------------------------------------------------------------------------


async def test_group1_no_token_401(client):
    resp = await client.post(
        "/agent/reminders/feedback",
        json={"dispatch_id": 1, "action": "viewed"},
    )
    assert resp.status_code == 401


async def test_group1_other_student_404_and_no_write(client, session_factory):
    """他人令牌 + 他人 dispatch → 404（不泄露存在性），且行零写入。"""
    await seed_dispatch(session_factory)

    resp = await feedback(client, 1, "viewed", headers=OTHER_AUTH)

    assert resp.status_code == 404
    row = await get_row(session_factory)
    assert row.opened_at is None and row.action_taken is None  # 无写入


async def test_group1_nonexistent_id_404(client, session_factory):
    await seed_dispatch(session_factory)

    resp = await feedback(client, 999, "viewed")

    assert resp.status_code == 404


async def test_group1_illegal_action_422(client, session_factory):
    """契约层 Literal 拒绝（AC-R5.10，schema 校验非业务拒绝）。"""
    await seed_dispatch(session_factory)

    resp = await feedback(client, 1, "clicked")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 组二 open 幂等：两次 open，opened_at 保持首次时间
# ---------------------------------------------------------------------------


async def test_group2_first_viewed_sets_opened_at(client, session_factory):
    await seed_dispatch(session_factory)

    resp = await feedback(client, 1, "viewed")

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True and body["action_taken"] == "viewed"
    row = await get_row(session_factory)
    assert row.opened_at is not None  # 服务端时钟（UTC），不信任请求体


async def test_group2_second_viewed_noop_keeps_first_opened_at(client, session_factory):
    await seed_dispatch(session_factory)
    await feedback(client, 1, "viewed")
    first = (await get_row(session_factory)).opened_at

    resp = await feedback(client, 1, "viewed")

    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    assert (await get_row(session_factory)).opened_at == first  # 首次即锚


# ---------------------------------------------------------------------------
# 组三 状态机：单向推进，非法回跳 no-op
# ---------------------------------------------------------------------------


async def test_group3_viewed_then_renewed_advances(client, session_factory):
    await seed_dispatch(session_factory)

    await feedback(client, 1, "viewed")
    resp = await feedback(client, 1, "renewed")

    assert resp.json()["applied"] is True
    row = await get_row(session_factory)
    assert row.action_taken == "renewed"


async def test_group3_renewed_then_viewed_noop_no_regression(client, session_factory):
    """终态不回退：renewed → viewed 静默 no-op（D2：吞掉迟到报文）。"""
    await seed_dispatch(session_factory)
    await feedback(client, 1, "viewed")
    await feedback(client, 1, "renewed")

    resp = await feedback(client, 1, "viewed")

    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    assert (await get_row(session_factory)).action_taken == "renewed"


async def test_group3_none_direct_to_renewed_sets_opened_at(client, session_factory):
    """none → renewed 直跳合法，且 opened_at 同步首次写入（renewed 隐含已打开）。"""
    await seed_dispatch(session_factory)

    resp = await feedback(client, 1, "renewed")

    assert resp.json()["applied"] is True
    row = await get_row(session_factory)
    assert row.action_taken == "renewed" and row.opened_at is not None


async def test_group3_non_sent_row_noop_200(client, session_factory):
    """failed/skipped_*/claimed 无可观测对象 → 200 no-op（D4，不报错）。"""
    await seed_dispatch(session_factory, dispatch_id=7, status="skipped_quota")

    resp = await feedback(client, 7, "viewed")

    assert resp.status_code == 200
    assert resp.json()["applied"] is False


# ---------------------------------------------------------------------------
# 组四 统计口径：feedback 写入 → stats 端点反映（AC-12 演示闭环）
# ---------------------------------------------------------------------------


async def test_group4_ac12_feedback_to_stats_loop(client, session_factory):
    """2 条 sent 主行（1 条被 viewed→renewed 推进）+ skipped/failed 各 1：
    分母剔除后 sent=2，action_rate=0.5——回流写入直接驱动 G6 统计。"""
    await seed_dispatch(session_factory, dispatch_id=1)
    await seed_dispatch(session_factory, dispatch_id=2)
    await seed_dispatch(session_factory, dispatch_id=3, status="skipped_quota")
    await seed_dispatch(session_factory, dispatch_id=4, status="failed")

    before = (await client.get("/agent/reminders/stats", headers=AUTH)).json()
    assert before == {
        "sent": 2, "opened": 0, "acted": 0,
        "opened_rate": 0.0, "action_rate": 0.0,
    }

    await feedback(client, 1, "viewed")
    await feedback(client, 1, "renewed")

    after = (await client.get("/agent/reminders/stats", headers=AUTH)).json()
    assert after == {
        "sent": 2, "opened": 1, "acted": 1,
        "opened_rate": 0.5, "action_rate": 0.5,
    }


async def test_group4_degraded_resend_row_not_double_counted(client, session_factory):
    """重发行（is_degraded=True）不计分母：主行 + 重发行并存 → sent 仍按主行算。"""
    await seed_dispatch(session_factory, dispatch_id=1)
    await seed_dispatch(session_factory, dispatch_id=2, is_degraded=True)

    stats = (await client.get("/agent/reminders/stats", headers=AUTH)).json()

    assert stats["sent"] == 1  # 主行才是分母成员
