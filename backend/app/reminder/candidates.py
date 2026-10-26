"""到期生成纯函数核心（R1，任务设计 09 §2.3/§2.4）。

职责边界（对齐 P4 确定性优先）：
- **零 IO / 零调度 / 零推送**：输入订阅与数据源快照，输出候选提醒；
- 数据拉取由调用方复用 edu/lib 适配器（降级语义在适配器层白拿）——
  快照进入本模块前必须已完成"新鲜度裁决"：stale_cache / unavailable
  的快照**不得**传入（宁可不发，不发错误数据，C4 R4.4）；
- dedup_key 规则与日频计数也在此定型，供 R2 引擎与 R3 时机门消费；
- 时机词表（``T-<N><unit>``）属契约层，定义于 schemas/reminder.py
  （``parse_timing``/``TriggerConfig.timing`` validator 同源），本模块反向引用。

时间锚点规则（09 §2.3 窗口时间语义的实现定版）：
- **天粒度**时机（``T-3d``/``T-1d``）锚定在"截止日 − N 天"的
  :data:`DEFAULT_REMIND_TIME`（08:00，避开静默期 23:00–07:00）——
  若锚定在 00:00，首个日间扫描永远错过触发点，D2 的次晨重放叙事不成立；
- **时/分粒度**时机（``T-2h``/``T-30min``）锚定在事件时刻 − N；
- 窗口判定：``now − lookback <= trigger_at < now + horizon``。
  ``lookback`` 默认等于 ``horizon``（对称窗）：容忍一次错过的 tick
  （horizon = 扫描间隔 × 2 ⇒ 连续两个 tick 故障仍自愈），claim 去重
  保证放宽的回看不产生重复候选。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from pydantic import ValidationError

from ..schemas.reminder import (  # 时机词表属契约层，candidates 反向引用（禁止反向依赖）
    ReminderSubject,
    TriggerConfig,
    parse_timing,
)

# ---------------------------------------------------------------------------
# 常量与规则
# ---------------------------------------------------------------------------

#: 天粒度时机的默认投递时刻（本地时，避开静默期；R2 起可经 Settings 覆盖）
DEFAULT_REMIND_TIME = time(8, 0)

#: 订阅未填 timing 时的目录默认（文档 10 §7.3：#2 图书到期 / #1 上课提醒）
DEFAULT_TIMINGS: dict[str, tuple[str, ...]] = {
    ReminderSubject.LIBRARY_DUE.value: ("T-3d", "T-1d"),
    ReminderSubject.COURSE.value: ("T-30min",),
}

#: 节次 → 开课时刻默认映射（学校标准作息；R2 起可经配置覆盖）
DEFAULT_SECTION_TIMES: Mapping[int, time] = {
    1: time(8, 0), 2: time(8, 55), 3: time(10, 0), 4: time(10, 55),
    5: time(14, 0), 6: time(14, 55), 7: time(16, 0), 8: time(16, 55),
    9: time(19, 0), 10: time(19, 55), 11: time(20, 45),
}

#: dedup_key 命名空间后缀（09 D2/D3：:quiet 不占真实去重名额；降级链 |ch{N}）
DEDUP_SUFFIX_QUIET = "quiet"


class CandidateGenerationError(ValueError):
    """候选生成的前置违反（如缺节次映射、now 未带时区）——显式失败，无静默降级。"""


@dataclass(frozen=True)
class ReminderCandidate:
    """候选提醒（引擎管线的最小传递单元，09 §F2）。

    ``payload_vars`` 只含模板变量（书名/条码/到期日/课程/教室），
    不含成绩等敏感明细（R3 红线）。``dedup_date`` = 窗口归属日 =
    ``trigger_at.date()``——跨零点扫描不分裂同一次提醒的去重身份。
    """

    subscription_id: int
    student_id: str
    subject: str
    channel: str
    priority: str
    timing: str              # 原始时机串，如 "T-3d" / "T-30min"
    trigger_at: datetime     # 绝对触发时刻（带时区）
    dedup_key: str
    payload_vars: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_date(self) -> date:
        return self.trigger_at.date()


def make_dedup_key(
    student_id: str,
    subscription_id: int,
    subject: str,
    timing: str,
    dedup_date: date,
    *,
    discriminator: str | None = None,
) -> str:
    """dedup_key 生成规则（09 §2.4）：sha256(五元组[+判别位]) 截 32 位 hex。

    - 五元组缺一不可：漏掉 subscription_id 会让同学生两个同类订阅互挤；
      漏掉 timing 会让 T-3d 与 T-1d 互顶；漏掉 dedup_date 会跨天重复拦截；
    - ``discriminator`` 用于**同一天多次触发**的场景（课程按开课时刻判别，
      如上午课与下午课各自成立）；缺省 = 库级去重（借阅到期按日聚合，
      同日多本书合并为一条提醒，与聚合门语义一致）。
    """
    raw = f"{student_id}|{subscription_id}|{subject}|{timing}|{dedup_date.isoformat()}"
    if discriminator is not None:
        raw = f"{raw}|{discriminator}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def suffixed_dedup_key(key: str, namespace: str) -> str:
    """命名空间后缀（后缀在 hash 之外，可观测）：``<hash32>|<ns>``。

    skipped_quiet 行用 ``|quiet``（D2：不占真实去重名额，否则跳过变永久丢失）；
    降级链重发用 ``|ch2``/``|ch3``（09 §2.5）。
    """
    return f"{key}|{namespace}"


# ---------------------------------------------------------------------------
# 到期窗口（两个生成器共用）
# ---------------------------------------------------------------------------


def _in_due_window(trigger_at: datetime, now: datetime, horizon: timedelta, lookback: timedelta) -> bool:
    return (now - lookback) <= trigger_at < (now + horizon)


def _sub_timings(sub: Any, subject_value: str) -> tuple[str, ...]:
    """订阅的时机序列：trigger_config.timing，缺省回退目录默认（R3.3 经契约模型）。

    库中历史脏数据（validator 上线前入库）在契约模型处显式失败——统一包装为
    :class:`CandidateGenerationError`，供 R3 引擎按订阅隔离异常（09 风险③：
    单点毒不扩散，而非全员灭）。
    """
    try:
        cfg = TriggerConfig.model_validate(sub.trigger_config or {})
    except ValidationError as e:
        raise CandidateGenerationError(
            f"订阅 {getattr(sub, 'id', '?')} trigger_config 违反契约：{e}"
        ) from e
    return tuple(cfg.timing) or DEFAULT_TIMINGS.get(subject_value, ())


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise CandidateGenerationError("now 必须带时区（确定性契约，禁止隐式本地时）")
    return now


# ---------------------------------------------------------------------------
# 生成器一：借阅到期（"借阅 N 天内到期"）
# ---------------------------------------------------------------------------


def due_library_candidates(
    subs: Iterable[Any],
    loans: Sequence[Any],
    now: datetime,
    *,
    horizon: timedelta,
    lookback: timedelta | None = None,
) -> list[ReminderCandidate]:
    """借阅到期候选（"借阅 N 天内到期"）。

    契约：``loans`` 已是该学生的在借快照（来自 LibAdapter，新鲜度由调用方
    裁决——stale/unavailable 快照不得传入）；``subs`` 中仅
    ``subject=library_due`` 且 ``enabled`` 的订阅参与配对。

    规则：
    - 跳过已超期借阅（``overdue_days > 0``）——超期属阈值型目录 #3，非本范围；
    - 天粒度时机锚定 ``due_date − N`` 的 :data:`DEFAULT_REMIND_TIME`；
      h/min 粒度时机无事件锚点，忽略（不产出也不报错）；
    - **库级去重**：同一 ``(订阅, timing)`` 在同一锚点日的全部命中借阅
      **合并为一条候选**（``payload_vars["items"]`` 列表）——与聚合门语义
      一致，且保证候选 ↔ dedup 身份一一对应，杜绝 claim 静默丢书。
    """
    now = _require_aware(now)
    lb = lookback if lookback is not None else horizon
    tz = now.tzinfo
    out: list[ReminderCandidate] = []
    for sub in subs:
        if not sub.enabled or sub.subject != ReminderSubject.LIBRARY_DUE.value:
            continue
        for timing in _sub_timings(sub, ReminderSubject.LIBRARY_DUE.value):
            if not timing.endswith("d"):
                continue  # 借阅无"事件时刻"，仅支持天粒度锚点（d 单位）
            offset = parse_timing(timing)
            days = offset.days
            if days < 0:
                continue
            # 按锚点日分组（同日到期多本书 → 一条聚合提醒）
            grouped: dict[date, dict[str, Any]] = {}
            for loan in loans:
                if getattr(loan, "overdue_days", 0) > 0:
                    continue
                trigger_at = datetime.combine(
                    loan.due_date - timedelta(days=days), DEFAULT_REMIND_TIME, tzinfo=tz
                )
                if not _in_due_window(trigger_at, now, horizon, lb):
                    continue
                grouped.setdefault(
                    trigger_at.date(),
                    {"trigger_at": trigger_at, "items": []},
                )["items"].append(
                    {"title": loan.title, "barcode": loan.barcode, "due_date": loan.due_date}
                )
            for dedup_date, bundle in grouped.items():
                key = make_dedup_key(sub.student_id, sub.id, sub.subject, timing, dedup_date)
                out.append(
                    ReminderCandidate(
                        subscription_id=sub.id,
                        student_id=sub.student_id,
                        subject=sub.subject,
                        channel=sub.channel,
                        priority=sub.priority,
                        timing=timing,
                        trigger_at=bundle["trigger_at"],
                        dedup_key=key,
                        payload_vars={"items": bundle["items"]},
                    )
                )
    return out


# ---------------------------------------------------------------------------
# 生成器二：明天课表（上课提醒）
# ---------------------------------------------------------------------------


def due_course_candidates(
    subs: Iterable[Any],
    items: Sequence[Any],
    now: datetime,
    *,
    snapshot_date: date,
    horizon: timedelta,
    lookback: timedelta | None = None,
    section_times: Mapping[int, time] = DEFAULT_SECTION_TIMES,
) -> list[ReminderCandidate]:
    """上课提醒候选（"明天课表"即调用方传 ``snapshot_date=明天`` 的课表快照）。

    契约：``items`` 已是 ``snapshot_date`` 当日有效课节（周次匹配由
    适配器/调用方完成——``weeks`` 是自由文本，纯函数不解析）。
    ``TimetableItem`` 无绝对时刻，开课时刻 = ``section_times[period_start]``
    （缺映射即配置不完整，显式报错，无静默降级）。

    规则：
    - 时/分粒度时机锚定 ``开课时刻 − N``；
    - 已开课（``start <= now``）的课节不再产出——迟到噪音抑制；
    - ``is_changed`` 调停课变更进 payload_vars，渲染层必须高亮（文档 10 §7.3 #5）。
    """
    now = _require_aware(now)
    lb = lookback if lookback is not None else horizon
    tz = now.tzinfo
    out: list[ReminderCandidate] = []
    for sub in subs:
        if not sub.enabled or sub.subject != ReminderSubject.COURSE.value:
            continue
        for timing in _sub_timings(sub, ReminderSubject.COURSE.value):
            offset = parse_timing(timing)
            for item in items:
                start_t = section_times.get(item.period_start)
                if start_t is None:
                    raise CandidateGenerationError(
                        f"节次 {item.period_start} 无开课时刻映射（配置不完整）"
                    )
                start_at = datetime.combine(snapshot_date, start_t, tzinfo=tz)
                if start_at <= now:
                    continue  # 已开课：T-30min 已失去意义
                trigger_at = start_at - offset
                if not _in_due_window(trigger_at, now, horizon, lb):
                    continue
                # 判别位 = 开课时刻：同一天多节课各自成立（否则下午课会被
                # 上午课的 claim 顶掉，09 §2.4 dedup 规则的 discriminator 位）
                key = make_dedup_key(
                    sub.student_id, sub.id, sub.subject, timing, trigger_at.date(),
                    discriminator=start_at.isoformat(),
                )
                out.append(
                    ReminderCandidate(
                        subscription_id=sub.id,
                        student_id=sub.student_id,
                        subject=sub.subject,
                        channel=sub.channel,
                        priority=sub.priority,
                        timing=timing,
                        trigger_at=trigger_at,
                        dedup_key=key,
                        payload_vars={
                            "course_name": item.course_name,
                            "location": item.location,
                            "teacher": item.teacher,
                            "start": start_at,
                            "is_changed": item.is_changed,
                            "change_note": item.change_note,
                        },
                    )
                )
    return out


# ---------------------------------------------------------------------------
# 日频计数（门4 的纯计数侧；行获取/JOIN priority 是 R2 引擎的查询侧）
# ---------------------------------------------------------------------------

#: 计费状态：sent（已发）与 claimed（已占坑，含并发认领——D3 频控 TOCTOU 收敛）
_BILLABLE_STATUSES = frozenset({"sent", "claimed"})


def daily_dispatch_count(rows: Iterable[Any], student_id: str, day: date) -> int:
    """学生 ``day`` 当日已计费提醒条数（非 P0；10 §7.4 日频上限 8 条的分母侧）。

    行契约（鸭子类型，与 ``ReminderDispatch`` ORM 行 + JOIN 出的
    ``priority`` 兼容）：``student_id / status / priority / pushed_at / dedup_date``。
    R2 查询侧参考形态（日期粗剪枝在 SQL，精确归属在此处）::

        SELECT d.*, s.priority
        FROM reminder_dispatch d JOIN reminder_subscription s ON s.id = d.subscription_id
        WHERE d.student_id = :sid AND d.status IN ('sent','claimed')
          AND d.dedup_date >= :day - 1

    日归属规则：sent 行按 ``pushed_at.date()``；claimed 行（尚未发送）
    按 ``dedup_date``。跨零点边缘（23:59 claimed → 00:01 sent）对两天各计
    一次——软性注意力保护的已知可接受偏差（09 D3 代价说明）。
    """
    count = 0
    for r in rows:
        if r.student_id != student_id or r.status not in _BILLABLE_STATUSES:
            continue
        if r.priority == "P0":
            continue  # P0 不计入日频上限（10 §7.4）
        effective_day = r.pushed_at.date() if r.pushed_at is not None else r.dedup_date
        if effective_day == day:
            count += 1
    return count


__all__ = [
    "DEFAULT_REMIND_TIME",
    "DEFAULT_SECTION_TIMES",
    "DEFAULT_TIMINGS",
    "DEDUP_SUFFIX_QUIET",
    "CandidateGenerationError",
    "ReminderCandidate",
    "daily_dispatch_count",
    "due_course_candidates",
    "due_library_candidates",
    "make_dedup_key",
    "parse_timing",
    "suffixed_dedup_key",
]
