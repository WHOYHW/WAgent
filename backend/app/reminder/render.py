"""渲染纯函数（R4a，任务设计 09 §F3，实施设计 13）。

``payload_vars → content`` 的 f-string 模板渲染。与 R1 同套路：
**零 IO / 零调度 / 零推送**，同入参必得同输出（AC-4e 纯函数性），
单测直调逐字断言（03 设计话术定版同理由：可单测、可审计、零延迟）。

输入形状由 R1 候选生成器定死（``candidates.py``）：

- ``library_due``：``{"items": [{"title", "barcode", "due_date"}, ...]}``，
  同束共享同一 ``due_date``（锚点日分组即 ``due_date − days``）；
- ``course``：``{"course_name", "location", "teacher", "start",
  "is_changed", "change_note"}``。

失败语义：形状不对 / 空束 / 已到期书 / 无时区 / 未知 subject 一律显式
``ValueError``——宁可炸出来，不静默渲染错文案（R6 时间版；09 K3：
错误文案是 P1 事故路径）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .evaluator import CAMPUS_TZ

__all__ = [
    "render_candidate",
    "render_course_content",
    "render_library_content",
]


def render_library_content(payload_vars: Mapping[str, Any], now: datetime) -> str:
    """借阅到期文案（单本 / 聚合束共用一个模板，条码只在单本时展示）。"""
    if now.tzinfo is None:
        raise ValueError("now 必须带时区（渲染层入口契约，禁止隐式本地时）")
    items = payload_vars.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("payload_vars.items 必须是非空列表（候选生成器契约）")

    due_date = items[0]["due_date"]
    for it in items:
        if it["due_date"] != due_date:
            raise ValueError("聚合束内 due_date 不一致（锚点日分组契约被破坏）")
    days = (due_date - now.astimezone(CAMPUS_TZ).date()).days
    if days < 0:
        # 候选生成器已滤超期（due_library_candidates L200）；出现即上游 bug
        # 或扫描时点异常——09 K3："向学生推送已到期" 是 P1 事故路径。
        raise ValueError(f"借阅已到期（due_date={due_date}），不得进入渲染")
    due_word = _due_word(days)
    due_str = f"{due_date.year}年{due_date.month}月{due_date.day}日"

    if len(items) == 1:
        it = items[0]
        return (
            f"【图书到期提醒】你有 1 本书{due_word}到期："
            f"《{it['title']}》（条码 {it['barcode']}），到期日 {due_str}。"
            "请及时归还或续借。"
        )
    titles = "".join(f"《{it['title']}》" for it in items)
    return (
        f"【图书到期提醒】你有 {len(items)} 本书{due_word}到期："
        f"{titles}，到期日 {due_str}。请及时归还或续借。"
    )


def render_course_content(payload_vars: Mapping[str, Any]) -> str:
    """上课提醒文案；``is_changed=True`` → 【调停课】高亮前缀 + 变更说明。

    高亮的落法（candidates.py L256 契约）：文本通道无样式，用固定标记
    前缀承担视觉突出。``change_note`` 为空则省略说明句。
    """
    try:
        course_name = payload_vars["course_name"]
        location = payload_vars["location"]
        teacher = payload_vars["teacher"]
        start = payload_vars["start"]
        is_changed = payload_vars["is_changed"]
        change_note = payload_vars["change_note"]
    except KeyError as e:
        raise ValueError(f"课程 payload_vars 缺键：{e.args[0]}") from e

    start_str = f"{start.hour:02d}:{start.minute:02d}"
    if is_changed:
        base = (
            f"【调停课】《{course_name}》课程安排有调整："
            f"{start_str} {location}，主讲 {teacher}。"
        )
        if change_note:
            base += f"变更说明：{change_note}。"
        return base
    return f"【上课提醒】{start_str} 《{course_name}》在 {location} 上课，主讲 {teacher}。"


def render_candidate(candidate: Any, now: datetime) -> str:
    """按 subject 分发的渲染入口（R4b 时机门聚合后的统一消费点）。"""
    subject = getattr(candidate, "subject", None)
    payload_vars = getattr(candidate, "payload_vars", None)
    if subject == "library_due":
        return render_library_content(payload_vars or {}, now)
    if subject == "course":
        return render_course_content(payload_vars or {})
    raise ValueError(f"未知 subject，无法渲染：{subject!r}")


def _due_word(days: int) -> str:
    """天数词三分支：0 → 今天、1 → 明天、N → N 天后（"到期"由模板补齐）。"""
    if days == 0:
        return "今天"
    if days == 1:
        return "明天"
    return f"{days}天后"
