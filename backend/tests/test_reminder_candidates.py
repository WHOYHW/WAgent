"""提醒候选生成纯函数单测（R1，任务设计 09 §2.3/§2.4，验收：纯函数单测全绿）。

零 IO：订阅用鸭子类型桩（与 ORM ReminderSubscription 字段面对齐），
快照直接构造 Pydantic 契约模型；时间用固定 +08:00 偏移（不依赖 tzdata）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.reminder import (
    CandidateGenerationError,
    ReminderCandidate,
    daily_dispatch_count,
    due_course_candidates,
    due_library_candidates,
    make_dedup_key,
    parse_timing,
    suffixed_dedup_key,
)
from app.schemas.library import LibraryLoanItem
from app.schemas.reminder import TimingFormatError, TriggerConfig
from app.schemas.timetable import TimetableItem

TZ = timezone(timedelta(hours=8))

HORIZON = timedelta(minutes=10)  # 09 §2.3：horizon = 扫描间隔 × 2（默认 300s × 2）

SUB_ID = 7
STUDENT = "20230001"


def make_sub(**over) -> SimpleNamespace:
    """library_due 订阅桩（ORM 字段面鸭子类型）。"""
    base = dict(
        id=SUB_ID,
        student_id=STUDENT,
        subject="library_due",
        trigger_type="time",
        trigger_config={"timing": ["T-3d", "T-1d"], "conditions": {}},
        channel="wecom",
        priority="P1",
        enabled=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def make_loan(due: date, **over) -> LibraryLoanItem:
    base = dict(
        title="高等数学（第七版）",
        barcode="BC0001",
        due_date=due,
        overdue_days=0,
        renewable=True,
    )
    base.update(over)
    return LibraryLoanItem(**base)


def make_item(**over) -> TimetableItem:
    base = dict(
        course_name="高等数学",
        course_code="MATH101",
        teacher="张三",
        location="5-108",
        weekday=1,
        period_start=1,
        period_end=2,
        weeks="1-16 周",
        is_changed=False,
    )
    base.update(over)
    return TimetableItem(**base)


# ---- parse_timing / dedup_key 规则 ----


@pytest.mark.parametrize(
    ("timing", "expected"),
    [("T-3d", timedelta(days=3)), ("T-2h", timedelta(hours=2)), ("T-30min", timedelta(minutes=30))],
)
def test_parse_timing_valid(timing, expected):
    assert parse_timing(timing) == expected


@pytest.mark.parametrize("timing", ["3d", "T-d", "T-30s", "", "T-3w"])
def test_parse_timing_invalid(timing):
    with pytest.raises(TimingFormatError):
        parse_timing(timing)


# ---- 契约层：TriggerConfig.timing 词表校验（脏参数入库前被拒，09 风险③入库侧防线） ----


@pytest.mark.parametrize("timing", ["3天前", "T-1w", "T-30s", "T+3d", "3d", ""])
def test_trigger_config_rejects_dirty_timing(timing):
    # LLM 编出的脏 timing（schemas/reminder.py validator）在入库前被拒
    with pytest.raises(ValidationError):
        TriggerConfig(timing=[timing])


def test_trigger_config_accepts_vocabulary_and_empty():
    assert TriggerConfig(timing=["T-3d", "T-30min"]).timing == ["T-3d", "T-30min"]
    assert TriggerConfig().timing == []  # 缺省 = 空列表（候选层回退目录默认）


def test_dedup_key_deterministic_and_shaped():
    d = date(2026, 10, 19)
    k1 = make_dedup_key(STUDENT, SUB_ID, "library_due", "T-3d", d)
    k2 = make_dedup_key(STUDENT, SUB_ID, "library_due", "T-3d", d)
    assert k1 == k2
    assert len(k1) == 32
    int(k1, 16)  # 全 hex


def test_dedup_key_every_component_matters():
    d = date(2026, 10, 19)
    base = make_dedup_key(STUDENT, SUB_ID, "library_due", "T-3d", d)
    assert base != make_dedup_key("20230002", SUB_ID, "library_due", "T-3d", d)
    assert base != make_dedup_key(STUDENT, 8, "library_due", "T-3d", d)
    assert base != make_dedup_key(STUDENT, SUB_ID, "course", "T-3d", d)
    assert base != make_dedup_key(STUDENT, SUB_ID, "library_due", "T-1d", d)
    assert base != make_dedup_key(STUDENT, SUB_ID, "library_due", "T-3d", date(2026, 10, 20))


def test_quiet_suffix_occupies_separate_namespace():
    base = make_dedup_key(STUDENT, SUB_ID, "library_due", "T-3d", date(2026, 10, 19))
    quiet = suffixed_dedup_key(base, "quiet")
    assert quiet != base and quiet.startswith(base + "|")  # D2：跳过行不顶掉真实发送


# ---- 生成器一：借阅到期 ----


def test_library_due_t3d_hits_window():
    # due 10-22，T-3d 锚点 = 10-19 08:00；now 07:55、对称窗 10min ⇒ 命中
    now = datetime(2026, 10, 19, 7, 55, tzinfo=TZ)
    subs = [make_sub()]
    loans = [make_loan(date(2026, 10, 22))]
    out = due_library_candidates(subs, loans, now, horizon=HORIZON)
    assert len(out) == 1
    c = out[0]
    assert c.timing == "T-3d"
    assert c.trigger_at == datetime(2026, 10, 19, 8, 0, tzinfo=TZ)
    assert c.dedup_date == date(2026, 10, 19)  # 窗口归属日 = trigger_at.date()
    assert c.dedup_key == make_dedup_key(STUDENT, SUB_ID, "library_due", "T-3d", date(2026, 10, 19))
    assert c.priority == "P1" and c.channel == "wecom"
    assert c.payload_vars["items"] == [
        {"title": "高等数学（第七版）", "barcode": "BC0001", "due_date": date(2026, 10, 22)}
    ]


def test_library_due_t1d_same_day():
    # due 明天：T-1d 锚点 = 今天 08:00 ⇒ 命中；T-3d 锚点在 10-17，已过且不在窗内
    now = datetime(2026, 10, 19, 7, 55, tzinfo=TZ)
    out = due_library_candidates([make_sub()], [make_loan(date(2026, 10, 20))], now, horizon=HORIZON)
    assert [c.timing for c in out] == ["T-1d"]


def test_library_due_beyond_horizon_no_candidate():
    # due 10-25：T-3d 锚点 10-22，远在窗口外
    now = datetime(2026, 10, 19, 7, 55, tzinfo=TZ)
    out = due_library_candidates([make_sub()], [make_loan(date(2026, 10, 25))], now, horizon=HORIZON)
    assert out == []


def test_library_due_skips_overdue_and_disabled_and_mismatch():
    now = datetime(2026, 10, 19, 7, 55, tzinfo=TZ)
    loans = [
        make_loan(date(2026, 10, 15), overdue_days=4),  # 已超期 → 阈值型目录，不在本范围
        make_loan(date(2026, 10, 22)),
    ]
    subs = [
        make_sub(enabled=False),
        make_sub(subject="course", trigger_config={"timing": ["T-30min"], "conditions": {}}),
        make_sub(),
    ]
    assert len(due_library_candidates(subs, loans, now, horizon=HORIZON)) == 1


def test_library_due_default_timings_when_empty():
    # trigger_config.timing 为空 → 回退目录默认（T-3d/T-1d，09 §F2）
    now = datetime(2026, 10, 19, 7, 55, tzinfo=TZ)
    sub = make_sub(trigger_config={"timing": [], "conditions": {}})
    out = due_library_candidates([sub], [make_loan(date(2026, 10, 22))], now, horizon=HORIZON)
    assert [c.timing for c in out] == ["T-3d"]


def test_library_due_ignores_sub_day_timings():
    # 借阅无事件时刻，h/min 粒度时机无锚点 → 忽略（不产出也不报错）
    now = datetime(2026, 10, 19, 7, 55, tzinfo=TZ)
    sub = make_sub(trigger_config={"timing": ["T-2h"], "conditions": {}})
    assert due_library_candidates([sub], [make_loan(date(2026, 10, 22))], now, horizon=HORIZON) == []


def test_library_due_same_day_loans_merged_into_one_candidate():
    # 同日到期多本书 → 一条聚合候选（库级去重：候选 ↔ dedup 身份一一对应，
    # 杜绝同键候选在 claim 处被静默丢书——R1 实现时发现并修正的规则缺口）
    now = datetime(2026, 10, 19, 7, 55, tzinfo=TZ)
    loans = [
        make_loan(date(2026, 10, 22), barcode="A"),
        make_loan(date(2026, 10, 22), title="线性代数", barcode="B"),
    ]
    out = due_library_candidates([make_sub()], loans, now, horizon=HORIZON)
    assert len(out) == 1
    assert [it["barcode"] for it in out[0].payload_vars["items"]] == ["A", "B"]


def test_library_due_different_anchor_dates_distinct_dedup():
    # 不同锚点日（宽回看窗口覆盖两天）：候选互异、dedup 身份互异
    now = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
    loans = [make_loan(date(2026, 10, 21), barcode="A"),  # T-3d 锚点 10-18 08:00
             make_loan(date(2026, 10, 22), barcode="B")]  # T-3d 锚点 10-19 08:00
    out = due_library_candidates([make_sub()], loans, now,
                                 horizon=HORIZON, lookback=timedelta(days=2))
    assert len(out) == 2
    assert out[0].dedup_key != out[1].dedup_key
    assert {c.dedup_date for c in out} == {date(2026, 10, 18), date(2026, 10, 19)}


def test_library_due_naive_now_rejected():
    with pytest.raises(CandidateGenerationError, match="时区"):
        due_library_candidates([make_sub()], [make_loan(date(2026, 10, 22))],
                               datetime(2026, 10, 19, 7, 55), horizon=HORIZON)


def test_dirty_timing_in_db_raises_generation_error():
    # 历史脏库数据（validator 上线前入库）在候选层显式失败并包装为
    # CandidateGenerationError——R3 引擎按订阅隔离（09 风险③：单点毒
    # 不扩散，而非全员灭）。节次映射缺失（due_course_candidates）同理。
    now = datetime(2026, 10, 19, 7, 55, tzinfo=TZ)
    sub = make_sub(trigger_config={"timing": ["T-1w"], "conditions": {}})
    with pytest.raises(CandidateGenerationError, match="trigger_config"):
        due_library_candidates([sub], [make_loan(date(2026, 10, 22))], now, horizon=HORIZON)
    course_sub = make_sub(subject="course",
                          trigger_config={"timing": ["T-1w"], "conditions": {}})
    with pytest.raises(CandidateGenerationError, match="trigger_config"):
        due_course_candidates([course_sub], [make_item()], now,
                              snapshot_date=date(2026, 10, 20), horizon=HORIZON)


# ---- 生成器二：明天课表 ----


def test_course_t30min_hits_window():
    # 明天（10-20）第 1 节 08:00 → T-30min 触发点 07:30；now 07:25 对称窗命中
    now = datetime(2026, 10, 20, 7, 25, tzinfo=TZ)
    sub = make_sub(subject="course", priority="P2",
                   trigger_config={"timing": ["T-30min"], "conditions": {}})
    out = due_course_candidates([sub], [make_item()], now,
                                snapshot_date=date(2026, 10, 20), horizon=HORIZON)
    assert len(out) == 1
    c = out[0]
    assert c.trigger_at == datetime(2026, 10, 20, 7, 30, tzinfo=TZ)
    assert c.payload_vars["course_name"] == "高等数学"
    assert c.payload_vars["start"] == datetime(2026, 10, 20, 8, 0, tzinfo=TZ)
    assert c.payload_vars["is_changed"] is False


def test_course_tomorrow_snapshot_produces_nothing_today():
    # "明天课表"今天生成：触发点未到不产出（claimed 不得提前占坑，09 §2.4 门序）
    now = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
    sub = make_sub(subject="course", trigger_config={"timing": ["T-30min"], "conditions": {}})
    out = due_course_candidates([sub], [make_item()], now,
                                snapshot_date=date(2026, 10, 20), horizon=HORIZON)
    assert out == []


def test_course_started_class_skipped_even_with_wide_lookback():
    # 已开课（08:00 ≤ now）的课节不再产出——迟到噪音抑制（09 §F2 course 规则）
    now = datetime(2026, 10, 20, 8, 5, tzinfo=TZ)
    sub = make_sub(subject="course", trigger_config={"timing": ["T-30min"], "conditions": {}})
    out = due_course_candidates([sub], [make_item()], now,
                                snapshot_date=date(2026, 10, 20),
                                horizon=HORIZON, lookback=timedelta(hours=2))
    assert out == []


def test_course_period3_and_changed_note():
    now = datetime(2026, 10, 20, 9, 25, tzinfo=TZ)  # 第 3 节 10:00 → 触发 09:30
    sub = make_sub(subject="course", trigger_config={"timing": ["T-30min"], "conditions": {}})
    item = make_item(period_start=3, period_end=4, is_changed=True, change_note="本周改至 5-208")
    out = due_course_candidates([sub], [item], now,
                                snapshot_date=date(2026, 10, 20), horizon=HORIZON)
    assert len(out) == 1
    assert out[0].payload_vars["start"] == datetime(2026, 10, 20, 10, 0, tzinfo=TZ)
    assert out[0].payload_vars["change_note"] == "本周改至 5-208"


def test_course_missing_section_time_raises():
    now = datetime(2026, 10, 20, 7, 25, tzinfo=TZ)
    sub = make_sub(subject="course", trigger_config={"timing": ["T-30min"], "conditions": {}})
    with pytest.raises(CandidateGenerationError, match="节次"):
        due_course_candidates([sub], [make_item(period_start=12)], now,
                              snapshot_date=date(2026, 10, 20), horizon=HORIZON)


def test_course_respects_subscription_timing():
    # T-2h：第 1 节 08:00 → 触发 06:00；now 05:55 命中
    now = datetime(2026, 10, 20, 5, 55, tzinfo=TZ)
    sub = make_sub(subject="course", trigger_config={"timing": ["T-2h"], "conditions": {}})
    out = due_course_candidates([sub], [make_item()], now,
                                snapshot_date=date(2026, 10, 20), horizon=HORIZON)
    assert [c.timing for c in out] == ["T-2h"]
    assert out[0].trigger_at == datetime(2026, 10, 20, 6, 0, tzinfo=TZ)


def test_course_same_day_classes_distinct_dedup():
    # 同一天上午课 + 下午课：判别位（开课时刻）使 dedup 身份互异——
    # 否则下午课会被上午课的 claim 顶掉（R1 实现时发现并修正的规则缺口）。
    # 宽 horizon（提前生成场景）+ 对称回看使两节课的触发点落在同一窗口内。
    now = datetime(2026, 10, 20, 7, 40, tzinfo=TZ)  # 窗口 [01:40, 13:40) ∋ 07:30 与 13:30
    sub = make_sub(subject="course", trigger_config={"timing": ["T-30min"], "conditions": {}})
    items = [make_item(), make_item(period_start=5, period_end=6, course_name="大学物理")]
    out = due_course_candidates([sub], items, now,
                                snapshot_date=date(2026, 10, 20), horizon=timedelta(hours=6))
    assert len(out) == 2
    assert out[0].dedup_key != out[1].dedup_key


def test_dedup_discriminator_bit():
    d = date(2026, 10, 20)
    base = make_dedup_key(STUDENT, SUB_ID, "course", "T-30min", d)
    with_disc = make_dedup_key(STUDENT, SUB_ID, "course", "T-30min", d, discriminator="2026-10-20T08:00:00+08:00")
    assert base != with_disc


# ---- 日频计数（门4 纯计数侧） ----


def drow(**over) -> SimpleNamespace:
    base = dict(
        student_id=STUDENT,
        status="sent",
        priority="P1",
        pushed_at=datetime(2026, 10, 19, 9, 0, tzinfo=TZ),
        dedup_date=date(2026, 10, 19),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_daily_dispatch_count_billable_only():
    today = date(2026, 10, 19)
    rows = [
        drow(),                                                   # sent 今天 P1 → 计 1
        drow(priority="P0"),                                      # P0 不计入
        drow(status="failed"),                                    # 失败不计
        drow(status="skipped_quiet"),                             # 跳过不计
        drow(status="skipped_quota"),
        drow(student_id="20230002"),                              # 他人不计
        drow(pushed_at=datetime(2026, 10, 18, 9, 0, tzinfo=TZ)),  # 昨天不计
        drow(status="claimed", pushed_at=None),                   # claimed（按 dedup_date 归今天）→ 计 1
    ]
    assert daily_dispatch_count(rows, STUDENT, today) == 2


def test_daily_dispatch_count_claimed_attributed_by_dedup_date():
    # claimed 行无 pushed_at：日归属 = dedup_date（D3：频控计数含并发 claim）
    rows = [drow(status="claimed", pushed_at=None, dedup_date=date(2026, 10, 18))]
    assert daily_dispatch_count(rows, STUDENT, date(2026, 10, 19)) == 0
    assert daily_dispatch_count(rows, STUDENT, date(2026, 10, 18)) == 1
