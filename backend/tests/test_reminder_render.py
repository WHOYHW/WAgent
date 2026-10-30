"""R4a 渲染纯函数测试（实施设计 13 §三：AC-4a ~ 4e）。

纯函数直调，零 IO。文案逐字断言——渲染层是用户可见面的最后一步，
逐字锁定才能保证审计口径（对齐 03 设计"固定模板可单测逐字断言"）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.reminder.candidates import ReminderCandidate
from app.reminder.render import (
    render_candidate,
    render_course_content,
    render_library_content,
)

CAMPUS = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 27, 8, 0, 0, tzinfo=CAMPUS)  # 校园本地周二 08:00


def lib_vars(*due_dates: date) -> dict:
    return {
        "items": [
            {"title": f"书{i}", "barcode": f"barcode-{i}", "due_date": d}
            for i, d in enumerate(due_dates, start=1)
        ]
    }


def course_vars(**kw) -> dict:
    base = {
        "course_name": "数据结构",
        "location": "敬贤楼201",
        "teacher": "王老师",
        "start": datetime(2026, 10, 27, 14, 0, tzinfo=CAMPUS),
        "is_changed": False,
        "change_note": "",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# AC-4a 借阅文案
# ---------------------------------------------------------------------------


def test_ac4a_single_book_with_barcode():
    """单本：含条码；3 天后到期（due=10-30，今天=10-27）。"""
    content = render_library_content(lib_vars(date(2026, 10, 30)), NOW)
    assert content == (
        "【图书到期提醒】你有 1 本书3天后到期："
        "《书1》（条码 barcode-1），到期日 2026年10月30日。请及时归还或续借。"
    )


def test_ac4a_aggregated_books_share_due_date():
    """聚合：多本一条文案，书名顿号连接，条码不出现（聚合后是噪音）。"""
    content = render_library_content(lib_vars(date(2026, 10, 30), date(2026, 10, 30)), NOW)
    assert content == (
        "【图书到期提醒】你有 2 本书3天后到期："
        "《书1》《书2》，到期日 2026年10月30日。请及时归还或续借。"
    )
    assert "barcode" not in content


def test_ac4a_mixed_due_dates_rejected():
    """同束 due_date 不一致（契约破坏）→ 显式 ValueError。"""
    with pytest.raises(ValueError, match="due_date 不一致"):
        render_library_content(lib_vars(date(2026, 10, 30), date(2026, 10, 31)), NOW)


# ---------------------------------------------------------------------------
# AC-4b 天数词三分支
# ---------------------------------------------------------------------------


def test_ac4b_due_word_branches():
    """0 → 今天、1 → 明天、N → N 天后。"""
    today = render_library_content(lib_vars(date(2026, 10, 27)), NOW)
    tomorrow = render_library_content(lib_vars(date(2026, 10, 28)), NOW)
    later = render_library_content(lib_vars(date(2026, 11, 3)), NOW)
    assert "今天到期" in today
    assert "明天到期" in tomorrow
    assert "7天后到期" in later

# ---------------------------------------------------------------------------
# AC-4c 课程文案
# ---------------------------------------------------------------------------


def test_ac4c_normal_course():
    content = render_course_content(course_vars())
    assert content == "【上课提醒】14:00 《数据结构》在 敬贤楼201 上课，主讲 王老师。"


def test_ac4c_changed_course_highlighted():
    """is_changed=True：【调停课】前缀（高亮落法）+ 变更说明。"""
    content = render_course_content(
        course_vars(is_changed=True, change_note="调至敬贤楼305")
    )
    assert content == (
        "【调停课】《数据结构》课程安排有调整：14:00 敬贤楼201，主讲 王老师。"
        "变更说明：调至敬贤楼305。"
    )


def test_ac4c_changed_without_note():
    """is_changed=True 且 note 为空：保留前缀，省略说明句。"""
    content = render_course_content(course_vars(is_changed=True, change_note=""))
    assert content.startswith("【调停课】")
    assert "变更说明" not in content


# ---------------------------------------------------------------------------
# AC-4d 显式失败路径
# ---------------------------------------------------------------------------


def test_ac4d_naive_now_rejected():
    with pytest.raises(ValueError, match="时区"):
        render_library_content(lib_vars(date(2026, 10, 30)), datetime(2026, 10, 27, 8, 0))


def test_ac4d_empty_items_rejected():
    with pytest.raises(ValueError, match="非空列表"):
        render_library_content({"items": []}, NOW)
    with pytest.raises(ValueError, match="非空列表"):
        render_library_content({}, NOW)


def test_ac4d_overdue_book_rejected():
    """已到期书进渲染（09 K3 P1 事故路径）→ 显式失败。"""
    with pytest.raises(ValueError, match="不得进入渲染"):
        render_library_content(lib_vars(date(2026, 10, 20)), NOW)


def test_ac4d_course_missing_key_rejected():
    bad = course_vars()
    del bad["location"]
    with pytest.raises(ValueError, match="缺键"):
        render_course_content(bad)


# ---------------------------------------------------------------------------
# AC-4e 分发入口 + 纯函数性
# ---------------------------------------------------------------------------


def _candidate(subject: str, payload_vars: dict) -> ReminderCandidate:
    return ReminderCandidate(
        subscription_id=1,
        student_id="20230001",
        subject=subject,
        channel="wecom",
        priority="P2",
        timing="T-3d",
        trigger_at=NOW + timedelta(days=3),
        dedup_key="k-1",
        payload_vars=payload_vars,
    )


def test_ac4e_dispatch_by_subject():
    lib = render_candidate(_candidate("library_due", lib_vars(date(2026, 10, 30))), NOW)
    course = render_candidate(_candidate("course", course_vars()), NOW)
    assert lib.startswith("【图书到期提醒】")
    assert course.startswith("【上课提醒】")
    with pytest.raises(ValueError, match="未知 subject"):
        render_candidate(_candidate("grade", {}), NOW)


def test_ac4e_pure_function_no_side_effects():
    """同入参两次调用逐字相同，且 payload_vars 不被修改。"""
    vars_before = course_vars()
    snapshot = dict(vars_before)
    a = render_course_content(vars_before)
    b = render_course_content(vars_before)
    assert a == b
    assert vars_before == snapshot
