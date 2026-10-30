"""Redis 缓存后端（C4）：与 :class:`~app.cache.TTLCache` 同接口。

与进程内实现的差异（跨进程共享所必需）：
- 过期判定用墙钟（``time.time``，可注入）而非 ``time.monotonic``——
  过期时间写入 Redis，须在进程间可比。
- 逻辑过期不依赖 Redis 原生 TTL：键带 ``ex`` 兜底 GC（逻辑 TTL 的
  ``_GC_MULTIPLIER`` 倍），保证 ``get_stale`` 在逻辑过期后仍能降级回源，
  同时陈旧数据最终被清理。
- 键统一加前缀，``clear`` 只清本模块写入的键。

接口契约见 ``app.cache.TTLCache``；调用方（适配器）对两种后端无感知。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import redis

_GC_MULTIPLIER = 10          # Redis 兜底 TTL = 逻辑 TTL × 此倍数
_SOCKET_TIMEOUT = 0.5        # 缓存操作不阻塞事件循环过久


class RedisCache:
    """与 TTLCache 同接口的 Redis 后端（get / get_stale / set / delete / clear）。"""

    def __init__(
        self,
        redis_url: str,
        *,
        prefix: str = "campus_agent:",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=_SOCKET_TIMEOUT,
            socket_timeout=_SOCKET_TIMEOUT,
        )
        self._prefix = prefix
        self._clock = clock

    def ping(self) -> None:
        """连接探活（供 make_cache 启动检查与测试跳过判定）。"""
        self._client.ping()

    def _full(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _load(self, key: str) -> dict[str, Any] | None:
        raw = self._client.get(self._full(key))
        if raw is None:
            return None
        return json.loads(raw)

    def get(self, key: str) -> tuple[bool, Any, Any]:
        """返回 (hit, value, synced_at)。仅逻辑未过期视为命中。"""
        entry = self._load(key)
        if entry is None:
            return False, None, None
        if self._clock() > entry["e"]:
            return False, None, None
        return True, entry["v"], _parse_synced(entry.get("s"))

    def get_stale(self, key: str) -> tuple[bool, Any, Any]:
        """返回 (exists, value, synced_at)，忽略逻辑过期（供降级回源）。"""
        entry = self._load(key)
        if entry is None:
            return False, None, None
        return True, entry["v"], _parse_synced(entry.get("s"))

    def set(self, key: str, value: Any, ttl_seconds: int, synced_at: Any = None) -> None:
        payload = {
            "v": value,
            "s": synced_at.isoformat() if isinstance(synced_at, datetime) else None,
            "e": self._clock() + ttl_seconds,
        }
        self._client.set(
            self._full(key),
            json.dumps(payload, ensure_ascii=False),
            ex=max(ttl_seconds * _GC_MULTIPLIER, 1),
        )

    def delete(self, key: str) -> None:
        self._client.delete(self._full(key))

    def clear(self) -> None:
        keys = list(self._client.scan_iter(match=f"{self._prefix}*"))
        if keys:
            self._client.delete(*keys)

    def __contains__(self, key: str) -> bool:
        hit, _, _ = self.get(key)
        return hit


def _parse_synced(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso)
