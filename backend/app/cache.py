"""进程内 TTL 缓存（缓存与降级策略的第一道）。

生产可替换为 Redis（``redis_url`` 已配置），本实现面向零配置开发与测试：
- ``get``：命中且未过期才返回，否则视为未命中。
- ``get_stale``：无论是否过期都返回最近一次写入（供 stale_cache 降级回源使用）。
- 时间基于 ``time.monotonic``（不受系统时钟回拨影响），TTL 计算不受 freeze 干扰。

单测（C4/C5）直接依赖本类的确定性行为。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    value: Any
    synced_at: Any          # 业务时间戳（datetime），供溯源展示
    expires_at: float       # monotonic 过期点


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}

    def get(self, key: str) -> tuple[bool, Any, Any]:
        """返回 (hit, value, synced_at)。仅未过期视为命中。"""
        entry = self._store.get(key)
        if entry is None:
            return False, None, None
        if time.monotonic() > entry.expires_at:
            return False, None, None
        return True, entry.value, entry.synced_at

    def get_stale(self, key: str) -> tuple[bool, Any, Any]:
        """返回 (exists, value, synced_at)，忽略过期（供降级回源）。"""
        entry = self._store.get(key)
        if entry is None:
            return False, None, None
        return True, entry.value, entry.synced_at

    def set(self, key: str, value: Any, ttl_seconds: int, synced_at: Any = None) -> None:
        self._store[key] = _Entry(
            value=value,
            synced_at=synced_at,
            expires_at=time.monotonic() + ttl_seconds,
        )

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def __contains__(self, key: str) -> bool:
        hit, _, _ = self.get(key)
        return hit


def make_cache(settings) -> TTLCache | RedisCache:  # noqa: F821 - 延迟导入避免硬依赖
    """按配置构造缓存后端：``cache_backend="redis"`` 用 Redis，否则进程内。

    Redis 不可达时不阻塞启动——记警告日志并降级为 TTLCache
    （缓存后端故障不应影响服务可用，与适配器降级哲学一致）。
    """
    if (getattr(settings, "cache_backend", "memory") or "").lower() != "redis":
        return TTLCache()
    try:
        from .cache_redis import RedisCache

        cache = RedisCache(settings.redis_url)
        cache.ping()
        return cache
    except Exception as exc:  # 含 redis 包缺失 / 连接失败
        logger.warning("Redis 不可达（%s），缓存降级为进程内 TTLCache", exc)
        return TTLCache()
