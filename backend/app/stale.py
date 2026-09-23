"""C5 is_stale 状态标注机制（§3.5）。

- TTL 阈值表（R5.2）按数据域配置化，经 ``config.Settings`` 注入。
- ``is_stale`` 判定规则由 :func:`degrade.compute_is_stale` 固化。
- 成绩域 TTL 恒为 0（不缓存明细），不产生 is_stale（AC5.5）。
"""

from __future__ import annotations

from .config import Settings

# 数据域 → TTL 取值的开关（R5.2）
DOMAIN_TTL_KEYS = {
    "timetable": "ttl_timetable",
    "library": "ttl_library",
    "card": "ttl_card",
}


def ttl_for_domain(domain: str, settings: Settings) -> int:
    """返回指定数据域的缓存 TTL（秒）。成绩域返回 0（不缓存）。"""
    key = DOMAIN_TTL_KEYS.get(domain)
    if key is None:
        # grade 及其它未登记域：不缓存，无 is_stale。
        return 0
    return int(getattr(settings, key))


def ttl_for_domain_peak(domain: str, settings: Settings) -> int:
    """高发期 TTL（调停课高发期课表 5min）。仅课表域有高发期值。"""
    if domain == "timetable":
        return int(settings.ttl_timetable_peak)
    return ttl_for_domain(domain, settings)
