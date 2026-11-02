"""提示词外移 + 版本化的守卫测试（P1 前置，为任务 17 黄金集归因铺路）。

约定（P1）：
- SYSTEM_PROMPT 唯一真源在 app/prompts.py，orchestrator 只保留别名；
- 每次修改提示词必须递增 PROMPT_VERSION（「日期.序号」格式）。
"""

import re

from app.orchestrator import DEFAULT_SYSTEM_PROMPT
from app.prompts import PROMPT_VERSION, SYSTEM_PROMPT


def test_version_format_is_date_dot_seq():
    """PROMPT_VERSION 必须是「YYYY-MM-DD.序号」——评测 run 记录按它归因。"""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", PROMPT_VERSION)


def test_orchestrator_alias_is_identity():
    """别名必须与真源是同一对象——防止有人复制一份文案造成两处漂移。"""
    assert DEFAULT_SYSTEM_PROMPT is SYSTEM_PROMPT


def test_prompt_contains_non_negotiable_rules():
    """关键规则在文案里必须存在：防手滑删行导致红线指令静默丢失。"""
    for fragment in (
        "不编造任何事实",            # 规则 1：不产生事实（12 §7）
        "可能不是最新",              # 规则 2：is_stale 陈旧标注复述
        "不要试图代替用户确认",      # 规则 8：续借二次确认（R2.4）
        "student_id",                # 规则 7：身份注入声明
    ):
        assert fragment in SYSTEM_PROMPT, f"系统提示词缺少关键规则片段：{fragment}"
