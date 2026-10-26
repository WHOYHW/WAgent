"""提醒引擎包（M5，任务设计 09）。

R1 交付纯函数核心：到期生成、dedup_key 规则、日频计数（零 IO/零调度/零推送）。
调度循环、时机门、下发器随 R2+ 逐层加入。
"""

from .candidates import (
    CandidateGenerationError,
    ReminderCandidate,
    daily_dispatch_count,
    due_course_candidates,
    due_library_candidates,
    make_dedup_key,
    parse_timing,
    suffixed_dedup_key,
)

__all__ = [
    "CandidateGenerationError",
    "ReminderCandidate",
    "daily_dispatch_count",
    "due_course_candidates",
    "due_library_candidates",
    "make_dedup_key",
    "parse_timing",
    "suffixed_dedup_key",
]
