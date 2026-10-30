"""提醒效果统计（R5.3，实施设计 15 §七：口径定版）。

纯函数，与 ``daily_dispatch_count``（candidates.py）同一鸭子类型先例：
ORM 行 / SimpleNamespace 均可消费，禁止 SQL 侧出现第二份口径逻辑。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_ACTED_ACTIONS = ("renewed", "returned")

__all__ = ["loss_avoidance_stats"]


def loss_avoidance_stats(rows: Iterable[Any]) -> dict[str, int | float]:
    """损失规避率统计（阶段一近似，15 §七口径定版）。

    分母 = ``status='sent'`` 且 ``is_degraded=False``：

    - ``skipped_quiet`` / ``skipped_quota`` 剔除：主动"不打扰"决策，非提醒失败；
    - ``failed`` / ``status='degraded'`` 剔除：通道确认未真实触达；
    - ``is_degraded=True`` 重发行剔除：与主行同属一个逻辑提醒（dedup_key
      ``|ch{N}`` 后缀独立成行，R4b D4），计入会双计分母；触达效果经主行观测。

    行鸭子契约：``status / opened_at / action_taken / is_degraded``。
    分母为 0 → 比率 ``0.0``（不抛除零，空表可演示）。
    """
    sent = opened = acted = 0
    for r in rows:
        if r.status != "sent" or r.is_degraded:
            continue
        sent += 1
        if r.opened_at is not None:
            opened += 1
        if r.action_taken in _ACTED_ACTIONS:
            acted += 1
    return {
        "sent": sent,
        "opened": opened,
        "acted": acted,
        "opened_rate": opened / sent if sent else 0.0,
        "action_rate": acted / sent if sent else 0.0,
    }


