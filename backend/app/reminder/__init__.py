"""提醒引擎包（M5，任务设计 09）。

R1 交付纯函数核心：到期生成、dedup_key 规则、日频计数（零 IO/零调度/零推送）。
R2 交付推送通道适配器（adapters/push.py）。R3a 交付心跳骨架：
start/stop 幂等生命周期、抖动睡眠循环、scan_once 空转（求值器 R3b 接线）。
"""

from .candidates import (
    CandidateGenerationError,
    ReminderCandidate,
    daily_dispatch_count,
    due_course_candidates,
    due_library_candidates,
    make_dedup_key,
    parse_timing,
    subscription_timings,
    suffixed_dedup_key,
)
from .engine import HEARTBEAT_TASK_NAME, ReminderEngine, ReminderEvaluator, ScanStats
from .evaluator import CourseEvaluator, LibraryDueEvaluator
from .feedback import FeedbackResult, apply_feedback
from .render import (
    render_candidate,
    render_course_content,
    render_library_content,
)
from .stats import loss_avoidance_stats

__all__ = [
    "CandidateGenerationError",
    "CourseEvaluator",
    "HEARTBEAT_TASK_NAME",
    "FeedbackResult",
    "LibraryDueEvaluator",
    "ReminderCandidate",
    "ReminderEngine",
    "ReminderEvaluator",
    "ScanStats",
    "daily_dispatch_count",
    "due_course_candidates",
    "due_library_candidates",
    "apply_feedback",
    "loss_avoidance_stats",
    "make_dedup_key",
    "parse_timing",
    "render_candidate",
    "render_course_content",
    "render_library_content",
    "subscription_timings",
    "suffixed_dedup_key",
]
