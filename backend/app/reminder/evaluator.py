"""求值器接线（R3b，任务设计 09 §F2，实施设计 WAgent-IMPL-11）。

两个时间型求值器实现 R3a 的 :class:`~app.reminder.engine.ReminderEvaluator`
协议，接线既有纯函数（R1：``due_*_candidates``）与既有适配器
（``LibAdapter`` / ``EduAdapter``，缓存与降级语义白拿）：

管线（两求值器同构，实施设计 11 §二）::

    订阅（引擎已过滤 enabled）→ 按学生分组
      → 逐学生：源快照 → 新鲜度裁决（AC-10：stale/unavailable → 零候选）
      → 逐订阅：喂纯函数 → 候选汇总

隔离粒度（实施设计 11 §2.3）：
- 源快照失败（异常 / degraded）→ 跳过**该学生**（快照是学生级所有权）；
- 脏数据（``CandidateGenerationError``）→ 跳过**该订阅**（09 v1.3：单点毒不扩散）；
- 两层都不向上抛出——单学生故障不杀整轮扫描。

阶段一源调用**串行**（订阅量小）；并发拉取（asyncio.gather 按学生分组）
登记为阶段二优化，接口无需改动。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..adapters import EduAdapter, LibAdapter
from ..config import Settings
from ..config import settings as default_settings
from ..logging_util import get_logger
from ..schemas.meta import DegradeStatus
from ..schemas.reminder import ReminderSubject, parse_timing
from ..security import mask_student_id
from .candidates import (
    CandidateGenerationError,
    ReminderCandidate,
    due_course_candidates,
    due_library_candidates,
    subscription_timings,
)

logger = get_logger("reminder.evaluator")

#: 校园时区（实施设计 11 §2.4）：09 风险⑤单时区假设（服务器在校内）。
#: 固定 +8 偏移而非 ZoneInfo——中国无夏令时，免 tzdata 依赖（零新依赖约束）。
#: 求值器入口统一换算：节次开课时刻 / dedup_date 回归校园日历日；
#: R4 静默门（本地时 23:00–07:00）消费同一常量，保证三处时区同源。
CAMPUS_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

#: 允许产出候选的新鲜度（AC-10：stale_cache / unavailable 零候选——
#: 宁可不发，不发错误数据，C4 R4.4；FRESH 是未过期的缓存命中，放行）。
_FRESH_STATUSES = frozenset({DegradeStatus.FRESH, DegradeStatus.LIVE_OK})


def _normalize_now(now: datetime) -> datetime:
    """入口时区归一：aware 必填（显式失败）+ 换算到校园时区。"""
    if now.tzinfo is None:
        raise ValueError("now 必须带时区（求值器入口契约，禁止隐式本地时）")
    return now.astimezone(CAMPUS_TZ)


def _horizon(settings: Settings) -> timedelta:
    """窗口前瞻 = 扫描间隔 × horizon_ticks（09 §2.3：防 tick 抖动漏窗）。"""
    return timedelta(
        seconds=settings.reminder_scan_interval_seconds * settings.reminder_horizon_ticks
    )


def _group_by_student(subs: Sequence[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for s in subs:
        grouped.setdefault(s.student_id, []).append(s)
    return grouped


class LibraryDueEvaluator:
    """借阅到期求值器（"借阅 N 天内到期"，subject=library_due）。

    快照 = ``LibAdapter.fetch_loans``（TTL 5min 缓存白拿）；新鲜度裁决后
    逐订阅喂 :func:`due_library_candidates`（超期借阅由纯函数跳过）。
    """

    subjects = frozenset({ReminderSubject.LIBRARY_DUE.value})

    def __init__(self, lib: LibAdapter, settings: Settings | None = None) -> None:
        self._lib = lib
        self.settings = settings or default_settings

    async def evaluate(
        self, subs: Sequence[Any], now: datetime
    ) -> list[ReminderCandidate]:
        now = _normalize_now(now)
        horizon = _horizon(self.settings)
        out: list[ReminderCandidate] = []
        for student_id, student_subs in _group_by_student(subs).items():
            masked = mask_student_id(student_id)
            try:
                resp = await self._lib.fetch_loans(student_id)
            except Exception:
                # 按学生隔离：快照意外失败（缓存/适配器层已消化可预期降级）
                logger.exception("library_snapshot_failed", student=masked)
                continue
            # 新鲜度裁决（AC-10）：stale/unavailable → 零候选 + degraded 日志
            if resp.degrade_status not in _FRESH_STATUSES or resp.data is None:
                logger.warning(
                    "library_snapshot_degraded",
                    student=masked,
                    degrade_status=resp.degrade_status.value,
                )
                continue
            for sub in student_subs:
                try:
                    out.extend(
                        due_library_candidates([sub], resp.data, now, horizon=horizon)
                    )
                except CandidateGenerationError as exc:
                    # 按订阅隔离：脏 trigger_config 只跳过该订阅（09 风险③②）
                    logger.warning(
                        "library_sub_skipped",
                        student=masked,
                        subscription_id=getattr(sub, "id", None),
                        reason=str(exc),
                    )
        return out


class CourseEvaluator:
    """上课提醒求值器（"明天课表"，subject=course）。

    快照日规则（实施设计 11 §2.2）：``snapshot_date = 今天 + max(订阅 timing
    提前量的天数部分)``——``{T-30min}``→今天、``{T-1d}``→明天、``{T-3d}``→今天+3。
    同一学生按 snapshot_date 分组共用一次拉取（适配器缓存 key 含日期，天然去重）。

    已知限制：``weeks`` 周次匹配未实现（自由文本，实施设计 11 §六——真实源
    接入前必须补适配器侧过滤；当前源未配置 → UNAVAILABLE → 零候选）。
    """

    subjects = frozenset({ReminderSubject.COURSE.value})

    def __init__(self, edu: EduAdapter, settings: Settings | None = None) -> None:
        self._edu = edu
        self.settings = settings or default_settings

    async def evaluate(
        self, subs: Sequence[Any], now: datetime
    ) -> list[ReminderCandidate]:
        now = _normalize_now(now)
        horizon = _horizon(self.settings)
        today = now.date()
        out: list[ReminderCandidate] = []
        for student_id, student_subs in _group_by_student(subs).items():
            masked = mask_student_id(student_id)
            try:
                # 按快照日分组：同学生同日的订阅共用一次课表拉取。
                by_date: dict[date, list[Any]] = {}
                for sub in student_subs:
                    try:
                        timings = subscription_timings(sub, ReminderSubject.COURSE.value)
                    except CandidateGenerationError as exc:
                        logger.warning(
                            "course_sub_skipped",
                            student=masked,
                            subscription_id=getattr(sub, "id", None),
                            reason=str(exc),
                        )
                        continue
                    advance = max(
                        (max(0, parse_timing(t).days) for t in timings), default=0
                    )
                    by_date.setdefault(today + timedelta(days=advance), []).append(sub)

                for snapshot_date, date_subs in by_date.items():
                    try:
                        resp = await self._edu.fetch_timetable(
                            student_id, snapshot_date, snapshot_date
                        )
                    except Exception:
                        logger.exception(
                            "course_snapshot_failed",
                            student=masked,
                            snapshot_date=snapshot_date.isoformat(),
                        )
                        continue
                    if resp.degrade_status not in _FRESH_STATUSES:
                        logger.warning(
                            "course_snapshot_degraded",
                            student=masked,
                            snapshot_date=snapshot_date.isoformat(),
                            degrade_status=resp.degrade_status.value,
                        )
                        continue
                    for sub in date_subs:
                        try:
                            out.extend(
                                due_course_candidates(
                                    [sub],
                                    resp.data or [],
                                    now,
                                    snapshot_date=snapshot_date,
                                    horizon=horizon,
                                )
                            )
                        except CandidateGenerationError as exc:
                            logger.warning(
                                "course_sub_skipped",
                                student=masked,
                                subscription_id=getattr(sub, "id", None),
                                reason=str(exc),
                            )
            except Exception:
                # 按学生隔离兜底：分组阶段之外的意外异常不扩散到其他学生。
                logger.exception("course_student_failed", student=masked)
        return out
