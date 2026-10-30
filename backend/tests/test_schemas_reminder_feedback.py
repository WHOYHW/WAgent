"""R5 效果回流契约层测试（15 §2.1/§2.2，R5.1 切片）。

API 层行为（401/404/CAS no-op）在 R5.2 端点测试覆盖；本文件只锁契约本身。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.reminder import ReminderFeedbackAction, ReminderFeedbackParams


def test_enum_values_align_with_orm_comment():
    """枚举值与 orm/models.py action_taken 列注释（viewed/renewed/returned）逐字对齐。"""
    assert ReminderFeedbackAction.VIEWED.value == "viewed"
    assert ReminderFeedbackAction.RENEWED.value == "renewed"
    assert ReminderFeedbackAction.RETURNED.value == "returned"
    assert {a.value for a in ReminderFeedbackAction} == {"viewed", "renewed", "returned"}


def test_params_parses_dispatch_id_and_action():
    params = ReminderFeedbackParams(dispatch_id=123, action="renewed")
    assert params.dispatch_id == 123
    assert params.action is ReminderFeedbackAction.RENEWED


def test_params_has_no_student_id_field():
    """契约面不含 student_id（L4：身份只来自 JWT，15 §D1）。"""
    assert "student_id" not in ReminderFeedbackParams.model_fields


def test_params_ignores_forged_student_id():
    """body 伪造同名字段被忽略（L4「请求体同名段一律忽略」的契约侧体现）。"""
    params = ReminderFeedbackParams(
        dispatch_id=1, action="viewed", student_id="9999"
    )
    assert params.dispatch_id == 1


@pytest.mark.parametrize("bad", [{"action": "viewed"}, {"dispatch_id": 1, "action": "clicked"}])
def test_params_rejects_missing_or_illegal_action(bad):
    """缺字段 / 非法 action（R5.2 端点将表现为 422，AC-R5.10）。"""
    with pytest.raises(ValidationError):
        ReminderFeedbackParams(**bad)


def test_params_rejects_non_positive_dispatch_id():
    with pytest.raises(ValidationError):
        ReminderFeedbackParams(dispatch_id=0, action="viewed")
