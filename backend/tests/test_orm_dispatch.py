"""ReminderDispatch 数据面验证（09 §D3 协议，v1.4 修复回写）。

锁定 claimed_at 作为 CAS 认领自愈时间锚的两个关键属性：
1. 列存在且 nullable（终态行/历史行缺省，claim 占坑时写入）；
2. **锚可刷新**——CAS 置换 ``SET claimed_at=now`` 后，重复执行同一语句
   不再命中（rowcount=0）：这是选择 claimed_at 而非不可变 created_at
   的根因（不可变锚会让另一实例下一 tick 立刻重复认领同一孤儿行，
   留双发窗口）。R6 引擎层的 CAS 认领直接消费此语义（AC-9）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.orm import ReminderDispatch

TZ = timezone(timedelta(hours=8))


def _cas_reclaim(row_id: int, threshold: datetime, now: datetime):
    """D3 协议第 2 步原句：claimed 超 threshold → 置换认领（锚刷新为 now）。"""
    return (
        update(ReminderDispatch)
        .where(
            ReminderDispatch.id == row_id,
            ReminderDispatch.status == "claimed",
            ReminderDispatch.claimed_at < threshold,
        )
        .values(status="claimed", claimed_at=now)
    )


def test_claimed_at_column_exists_and_nullable():
    col = ReminderDispatch.__table__.c["claimed_at"]
    assert col.nullable


async def test_cas_reclaim_anchor_is_refreshable(db):
    now = datetime(2026, 10, 21, 8, 0, tzinfo=TZ)
    threshold = now - timedelta(minutes=5)

    stale_row = ReminderDispatch(  # 孤儿行：claim 后崩溃（锚 10 分钟前）
        subscription_id=1, student_id="20230001", dedup_key="a" * 32,
        dedup_date=now.date(), channel="wecom", status="claimed",
        claimed_at=now - timedelta(minutes=10), is_degraded=False,
    )
    fresh_row = ReminderDispatch(  # 他实例刚占坑：未超阈值，不可置换
        subscription_id=2, student_id="20230002", dedup_key="b" * 32,
        dedup_date=now.date(), channel="wecom", status="claimed",
        claimed_at=now - timedelta(seconds=30), is_degraded=False,
    )
    db.add_all([stale_row, fresh_row])
    await db.commit()

    # stale 孤儿行 → rowcount=1，获得续发权（AC-9 机制面）
    res1 = await db.execute(_cas_reclaim(stale_row.id, threshold, now))
    await db.commit()
    assert res1.rowcount == 1

    # 锚已刷新为 now → 同一语句再次执行不命中：无双发窗口
    res2 = await db.execute(_cas_reclaim(stale_row.id, threshold, now))
    assert res2.rowcount == 0

    # 未超阈值的 claimed 行不可被置换（保护他实例的发送权）
    res3 = await db.execute(_cas_reclaim(fresh_row.id, threshold, now))
    assert res3.rowcount == 0
