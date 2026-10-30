"""时机门四闸全序 + 下发闭环（R4b，任务设计 09 §2.4/D2/D3，实施设计 14）。

门序（14 §D1 定版）：**静默 → claim → 频控 → 聚合渲染 → 下发 → 终态**。

- 静默在 claim 前跳过不占坑（skipped_quiet 行 dedup_key 加 ``|quiet`` 后缀，
  落独立命名空间——否则次晨重放被唯一约束顶掉，跳过变永久丢失，09 D2）；
- claim-first 占坑：INSERT 成功即获得唯一发送权；IntegrityError 先做 CAS
  认领自愈（claimed_at 可刷新锚，迁移 0003），rowcount=1 接管孤儿行续发，
  rowcount=0 让路（09 D3）；
- 频控在 claim 后计数（消 TOCTOU）：SQL 粗剪枝 + :func:`daily_dispatch_count`
  纯函数精确归属——零第二份计数逻辑（14 §D3）；P0 永不计数、永不聚合；
- 聚合只改渲染分组，不改变 claim 粒度（09 §F3）；终态单语句回写。

本模块自开独立 db 会话；任何异常上抛由引擎 scan_once 的 scan_errors 兜底
（不杀心跳）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.push import PushAdapter
from ..config import Settings
from ..logging_util import get_logger
from ..orm import ReminderDispatch, ReminderSubscription
from .candidates import daily_dispatch_count
from .evaluator import CAMPUS_TZ
from .render import render_candidate

logger = get_logger("reminder.gate")

#: 静默期跳过行的 dedup_key 后缀（独立命名空间，不占真实去重名额，09 D2）
QUIET_SUFFIX = "|quiet"

#: P0 优先级字面量（ORM 列是普通 String；与 daily_dispatch_count L336 同源）
P0 = "P0"


@dataclass
class GateStats:
    """单次下发管线的闸门计数（观测面，回填进 ScanStats）。"""

    sent: int = 0
    failed: int = 0
    degraded: int = 0
    skipped_quiet: int = 0
    skipped_quota: int = 0
    duplicates: int = 0  # claim 让路（含 CAS 接管失败）


@dataclass
class _Passed:
    """过静默门 + claim 成功的候选及其 dispatch 行 id。"""

    candidate: Any
    row_id: int


# ---------------------------------------------------------------------------
# 门1：静默期
# ---------------------------------------------------------------------------


def _in_quiet(local_t: time, quiet_start: time, quiet_end: time) -> bool:
    """静默窗口判定（14 §D4）：start>end 跨午夜；start==end 视为全天静默。"""
    if quiet_start == quiet_end:
        return True
    if quiet_start > quiet_end:  # 跨午夜：23:00–07:00
        return local_t >= quiet_start or local_t < quiet_end
    return quiet_start <= local_t < quiet_end


# ---------------------------------------------------------------------------
# 门2：claim（占坑 + CAS 自愈）
# ---------------------------------------------------------------------------


async def _claim(
    db: AsyncSession, candidate: Any, dedup_date: Any, now: datetime, *, stale_seconds: int
) -> int | None:
    """占坑。返回 dispatch 行 id；None = 让路（他人持有且锚新鲜 / 已终态）。"""
    row = ReminderDispatch(
        subscription_id=candidate.subscription_id,
        student_id=candidate.student_id,
        dedup_key=candidate.dedup_key,
        dedup_date=dedup_date,
        channel=candidate.channel,
        status="claimed",
        claimed_at=now,
    )
    try:
        async with db.begin_nested():  # SAVEPOINT：IntegrityError 只回滚占坑点
            db.add(row)
            await db.flush()
    except IntegrityError:
        # 撞唯一约束：先做 CAS 认领自愈（09 D3），不是直接让路。
        return await _cas_takeover(db, candidate, now, stale_seconds)
    return row.id


async def _cas_takeover(
    db: AsyncSession, candidate: Any, now: datetime, stale_seconds: int
) -> int | None:
    """CAS 续锚：孤儿 claimed 行超阈值 → 接管续发权；否则让路。"""
    threshold = now - timedelta(seconds=stale_seconds)
    existing = await db.execute(
        select(ReminderDispatch).where(ReminderDispatch.dedup_key == candidate.dedup_key)
    )
    row = existing.scalar_one_or_none()
    if row is None:
        return None
    result = await db.execute(
        update(ReminderDispatch)
        .where(
            ReminderDispatch.id == row.id,
            ReminderDispatch.status == "claimed",
            ReminderDispatch.claimed_at < threshold,
        )
        .values(claimed_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return None  # 已终态或锚新鲜：另一实例正在处理，让路
    return row.id


# ---------------------------------------------------------------------------
# 门4：频控（claim 后计数，消 TOCTOU）
# ---------------------------------------------------------------------------


async def _billable_rows(db: AsyncSession, student_id: str, day: Any) -> list[Any]:
    """SQL 粗剪枝（09 daily_dispatch_count docstring 参考形态）：学生当日
    billable 行 + JOIN 订阅 priority；精确归属交给纯函数。"""
    result = await db.execute(
        select(
            # 平铺列（而非双实体 Row）：daily_dispatch_count 鸭子访问
            # r.student_id / r.status / r.pushed_at / r.dedup_date，
            # 双实体 Row 的键只有实体名，属性访问必炸（2026-10-27 复现修复）。
            ReminderDispatch.student_id,
            ReminderDispatch.status,
            ReminderDispatch.pushed_at,
            ReminderDispatch.dedup_date,
            ReminderSubscription.priority.label("priority"),
        )
        .join(ReminderSubscription, ReminderSubscription.id == ReminderDispatch.subscription_id)
        .where(
            ReminderDispatch.student_id == student_id,
            ReminderDispatch.status.in_(("sent", "claimed")),
            ReminderDispatch.dedup_date >= day - timedelta(days=1),
        )
    )
    return list(result.all())


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def dispatch(
    candidates: Sequence[Any],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    push: PushAdapter,
    settings: Settings,
    now: datetime,
) -> GateStats:
    """四闸全序 + 下发闭环。``now`` 须带时区（校园本地时判定同源 CAMPUS_TZ）。"""
    if now.tzinfo is None:
        raise ValueError("now 必须带时区（gate 入口契约）")
    stats = GateStats()
    if not candidates:
        return stats

    local_now = now.astimezone(CAMPUS_TZ)
    today = local_now.date()

    # ---- 门1 静默（claim 前，跳过不占坑；P0 直通）----
    survivors: list[Any] = []
    async with session_factory() as db:
        for cand in candidates:
            if cand.priority != P0 and _in_quiet(
                local_now.time(), settings.reminder_quiet_start, settings.reminder_quiet_end
            ):
                db.add(
                    ReminderDispatch(
                        subscription_id=cand.subscription_id,
                        student_id=cand.student_id,
                        dedup_key=cand.dedup_key + QUIET_SUFFIX,
                        dedup_date=cand.trigger_at.astimezone(CAMPUS_TZ).date(),
                        channel=cand.channel,
                        status="skipped_quiet",
                    )
                )
                stats.skipped_quiet += 1
                continue
            survivors.append(cand)
        await db.commit()
    if stats.skipped_quiet:
        logger.info(
            "reminder_skipped_quiet", count=stats.skipped_quiet, hint="窗口仍有效者次晨重放"
        )
    if not survivors:
        return stats

    # ---- 门2 claim → 门4 频控（均按学生分组推进）----
    by_student: dict[str, list[Any]] = {}
    for cand in survivors:
        by_student.setdefault(cand.student_id, []).append(cand)

    claimed: list[_Passed] = []
    async with session_factory() as db:
        for student_id, cands in by_student.items():
            # 频控基数：本批 claim 前的当日 billable 计数（纯函数精确归属）
            base = daily_dispatch_count(
                await _billable_rows(db, student_id, today), student_id, today
            )
            allowance = max(0, settings.reminder_daily_cap - base)
            batch_used = 0
            for cand in cands:
                dedup_date = cand.trigger_at.astimezone(CAMPUS_TZ).date()
                row_id = await _claim(
                    db, cand, dedup_date, now,
                    stale_seconds=settings.reminder_stale_claim_seconds,
                )
                if row_id is None:
                    stats.duplicates += 1
                    continue
                over_quota = (
                    cand.priority != P0 and batch_used >= allowance
                )
                if over_quota:
                    await db.execute(
                        update(ReminderDispatch)
                        .where(ReminderDispatch.id == row_id)
                        .values(status="skipped_quota")
                        .execution_options(synchronize_session=False)
                    )
                    stats.skipped_quota += 1
                    continue
                batch_used += 1
                claimed.append(_Passed(candidate=cand, row_id=row_id))
        await db.commit()
    if not claimed:
        return stats

    # ---- 门3 聚合渲染（按学生分组；P0 永不聚合）----
    messages: list[tuple[list[int], str, str, str]] = []  # (row_ids, student, channel, content)
    claimed_by_student: dict[str, list[_Passed]] = {}
    for p in claimed:
        claimed_by_student.setdefault(p.candidate.student_id, []).append(p)
    for student_id, group in claimed_by_student.items():
        p0 = [p for p in group if p.candidate.priority == P0]
        rest = [p for p in group if p.candidate.priority != P0]
        for p in p0:  # P0 立即单发
            messages.append(
                ([p.row_id], student_id, p.candidate.channel,
                 render_candidate(p.candidate, now))
            )
        if not rest:
            continue
        if len(rest) == 1:
            p = rest[0]
            messages.append(
                ([p.row_id], student_id, p.candidate.channel,
                 render_candidate(p.candidate, now))
            )
        else:
            lines = "\n".join(
                f"{i}. {render_candidate(p.candidate, now)}"
                for i, p in enumerate(rest, start=1)
            )
            content = f"【提醒汇总】共 {len(rest)} 条：\n{lines}"
            messages.append(
                ([p.row_id for p in rest], student_id, rest[0].candidate.channel, content)
            )

    # ---- 下发 + 终态单语句回写 ----
    async with session_factory() as db:
        for row_ids, student_id, channel, content in messages:
            receipt = await push.send(student_id, channel, content)
            await db.execute(
                update(ReminderDispatch)
                .where(ReminderDispatch.id.in_(row_ids))
                .values(
                    status=receipt.status,
                    pushed_at=now,
                    is_degraded=receipt.is_degraded,
                ).execution_options(synchronize_session=False)
            )
            if receipt.status == "sent":
                stats.sent += len(row_ids)
            elif receipt.status == "degraded":
                stats.degraded += len(row_ids)
            else:
                stats.failed += len(row_ids)
        await db.commit()

    logger.info(
        "reminder_dispatch",
        sent=stats.sent,
        failed=stats.failed,
        degraded=stats.degraded,
        skipped_quiet=stats.skipped_quiet,
        skipped_quota=stats.skipped_quota,
        duplicates=stats.duplicates,
    )
    return stats
