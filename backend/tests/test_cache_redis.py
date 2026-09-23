"""RedisCache（C4 缓存的 Redis 后端）与 make_cache 工厂测试。

与 TTLCache（test_degrade_and_stale.py）互补：接口语义必须一致，
过期判定走注入时钟（墙钟，进程间可比），Redis 不可用时整组跳过。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

import pytest

from app.cache import TTLCache, make_cache
from app.config import Settings

REDIS_URL = "redis://localhost:6379/0"

_now: list[float] = [1000.0]


def _clock() -> float:
    return _now[0]


def _key() -> str:
    return f"test:{uuid.uuid4().hex}"


@pytest.fixture
def redis_cache():
    """RedisCache 实例；Redis 不可用则跳过本组（不依赖外部服务的测试在下方独立存在）。"""
    pytest.importorskip("redis")
    from app.cache_redis import RedisCache

    cache = RedisCache(REDIS_URL, clock=_clock)
    try:
        cache.ping()
    except Exception:
        pytest.skip("本机 Redis 不可用，跳过 RedisCache 集成测试")
    return cache


# ---- 接口语义与 TTLCache 一致 ----


def test_set_get_roundtrip(redis_cache):
    """写入后命中：返回值与 synced_at（datetime）还原一致。"""
    synced = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
    key = _key()
    redis_cache.set(key, [{"course_name": "高数"}], 900, synced)

    hit, value, synced_back = redis_cache.get(key)
    assert hit is True
    assert value == [{"course_name": "高数"}]
    assert synced_back == synced


def test_get_miss_on_unknown_key(redis_cache):
    hit, value, synced = redis_cache.get(_key())
    assert (hit, value, synced) == (False, None, None)


def test_expired_is_miss_but_stale_available(redis_cache):
    """逻辑过期后 get 未命中、get_stale 仍可降级回源（STALE_CACHE 语义）。"""
    key = _key()
    redis_cache.set(key, "v", 100, datetime.now(timezone.utc))

    _now[0] += 101  # 时钟推进越过 TTL
    hit, _, _ = redis_cache.get(key)
    assert hit is False

    exists, stale_value, _ = redis_cache.get_stale(key)
    assert exists is True
    assert stale_value == "v"


def test_delete_removes_key(redis_cache):
    key = _key()
    redis_cache.set(key, "v", 100)
    redis_cache.delete(key)
    assert redis_cache.get(key)[0] is False


def test_clear_only_removes_own_prefix(redis_cache):
    """clear 只清本前缀键，不动同一 Redis 里的其他数据。"""
    ours = _key()
    foreign = f"foreign:{uuid.uuid4().hex}"
    redis_cache.set(ours, "v", 100)
    redis_cache._client.set(foreign, "keep")

    redis_cache.clear()
    assert redis_cache.get(ours)[0] is False
    assert redis_cache._client.get(foreign) == "keep"
    redis_cache._client.delete(foreign)


# ---- make_cache 工厂（不依赖 Redis 是否在线）----


def test_make_cache_default_is_memory():
    assert isinstance(make_cache(Settings()), TTLCache)


def test_make_cache_redis_backend(redis_cache):
    """cache_backend=redis 且可达 → RedisCache。"""
    assert isinstance(make_cache(Settings(cache_backend="redis", redis_url=REDIS_URL)),
                      type(redis_cache))


def test_make_cache_falls_back_when_redis_down(caplog):
    """Redis 不可达 → 降级 TTLCache 并留日志（降级哲学：缓存后端故障不阻塞启动）。"""
    with caplog.at_level("WARNING"):
        cache = make_cache(Settings(
            cache_backend="redis",
            redis_url="redis://localhost:9/0",  # 无人监听的端口
        ))
    assert isinstance(cache, TTLCache)
    assert any("降级" in r.message or "Redis" in r.message for r in caplog.records)
