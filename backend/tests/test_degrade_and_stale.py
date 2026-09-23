"""C4/C5 验收测试：降级链路（AC4.3/4.4/4.5）与 is_stale 判定（AC5.1-5.5）。

适配器层用 respx mock 源系统 HTTP 行为（超时 / 5xx / 未配置 / 正常）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import respx

from app.adapters.base import SourceNotConfiguredError
from app.adapters.library import LibAdapter
from app.adapters.edu import EduAdapter
from app.cache import TTLCache
from app.config import Settings
from app.degrade import compute_is_stale, build_tool_meta
from app.schemas.meta import DegradeStatus, ToolSource

NOW = datetime.now(timezone.utc)


# ---- is_stale 判定规则（R5.1）----


def test_is_stale_on_stale_cache_status():
    assert compute_is_stale(
        source=ToolSource.EDU_SYSTEM, synced_at=NOW, ttl_seconds=900,
        degrade_status=DegradeStatus.STALE_CACHE, now=NOW,
    ) is True


def test_is_stale_false_on_fresh_and_live():
    for status in (DegradeStatus.FRESH, DegradeStatus.LIVE_OK):
        assert compute_is_stale(
            source=ToolSource.EDU_SYSTEM, synced_at=NOW, ttl_seconds=900,
            degrade_status=status, now=NOW,
        ) is False


def test_is_stale_on_expired_cache_source():
    old = NOW - timedelta(seconds=901)
    assert compute_is_stale(
        source=ToolSource.CACHE, synced_at=old, ttl_seconds=900,
        degrade_status=DegradeStatus.FRESH, now=NOW,
    ) is True


def test_meta_carries_degrade_status():
    meta = build_tool_meta(
        source=ToolSource.EDU_SYSTEM, synced_at=NOW,
        degrade_status=DegradeStatus.STALE_CACHE, ttl_seconds=900, now=NOW,
    )
    assert meta.degrade_status == DegradeStatus.STALE_CACHE
    assert meta.is_stale is True
    assert meta.stale_reason == "source_timeout"


# ---- 适配器降级链路 ----


def _edu_settings() -> Settings:
    return Settings(edu_api_base_url="http://edu.test")


@respx.mock
async def test_timeout_with_cache_returns_stale_cache():
    """源超时且有历史缓存 → STALE_CACHE（AC4.3）。"""
    cache = TTLCache()
    # 预置"已逻辑过期"的历史缓存：get 未命中（触发回源），get_stale 仍可降级。
    cache.set("k", [{"course_name": "x"}], -1, NOW)
    adapter = EduAdapter(cache, settings=_edu_settings())
    respx.get("http://edu.test/timetable").mock(side_effect=httpx.ReadTimeout("boom"))

    resp = await adapter._fetch_with_cache("k", 900, lambda: adapter._request_with_retry(
        "GET", "http://edu.test/timetable"))
    assert resp.degrade_status == DegradeStatus.STALE_CACHE
    assert resp.data == [{"course_name": "x"}]


@respx.mock
async def test_timeout_without_cache_returns_unavailable():
    """源超时且无缓存 → UNAVAILABLE（AC4.4）。"""
    adapter = EduAdapter(TTLCache(), settings=_edu_settings())
    route = respx.get("http://edu.test/timetable").mock(side_effect=httpx.ReadTimeout("boom"))

    resp = await adapter._fetch_with_cache("none", 900, lambda: adapter._request_with_retry(
        "GET", "http://edu.test/timetable"))
    assert resp.degrade_status == DegradeStatus.UNAVAILABLE
    assert resp.data is None
    # 只读重试 ≤2 次：首试 + 2 重试 = 3 次调用（R4.3）。
    assert route.call_count == 3


@respx.mock
async def test_http_5xx_degrades_instead_of_raising():
    """源 5xx 不穿透降级链路（R4.4 / 文档 12 §5）。"""
    cache = TTLCache()
    # 同上：预置已逻辑过期的历史缓存，回源 5xx 后走 STALE_CACHE 降级。
    cache.set("k", [{"course_name": "cached"}], -1, NOW)
    adapter = EduAdapter(cache, settings=_edu_settings())
    respx.get("http://edu.test/timetable").mock(return_value=httpx.Response(500))

    resp = await adapter._fetch_with_cache("k", 900, lambda: adapter._request_with_retry(
        "GET", "http://edu.test/timetable"))
    assert resp.degrade_status == DegradeStatus.STALE_CACHE
    assert resp.data == [{"course_name": "cached"}]


def test_not_configured_degrades():
    """源未配置且无缓存 → UNAVAILABLE（降级适配器语义）。"""

    async def fetch():
        raise SourceNotConfiguredError("EDU_API_BASE_URL 未配置")

    import asyncio

    resp = asyncio.run(EduAdapter(TTLCache(), settings=Settings())._fetch_with_cache(
        "k", 900, fetch))
    assert resp.degrade_status == DegradeStatus.UNAVAILABLE


@respx.mock
async def test_live_then_fresh_from_cache():
    """回源成功写缓存 → 第二次命中 FRESH（AC5.1）。"""
    cache = TTLCache()
    adapter = EduAdapter(cache, settings=_edu_settings())
    # 走真实 _fetch_timetable_live：fetch_fn 须返回 (data, synced_at) 元组契约。
    respx.get("http://edu.test/timetable").mock(
        return_value=httpx.Response(200, json={"items": [{
            "course_name": "高数", "course_code": "MA101", "teacher": "张老师",
            "location": "一教-101", "weekday": 1, "period_start": 1,
            "period_end": 2, "weeks": "1-16 周",
        }]})
    )
    fetch = lambda: adapter._fetch_timetable_live(  # noqa: E731
        "20230001", date(2026, 9, 20), date(2026, 9, 21), None)

    live = await adapter._fetch_with_cache("k", 900, fetch)
    assert live.degrade_status == DegradeStatus.LIVE_OK
    assert [i.course_name for i in live.data] == ["高数"]

    fresh = await adapter._fetch_with_cache("k", 900, fetch)
    assert fresh.degrade_status == DegradeStatus.FRESH


# ---- 写操作：失败不重试（AC4.5）----


@respx.mock
async def test_renew_write_failed_no_retry_with_reason():
    """续借遇 ILS 5xx：WRITE_FAILED + 原因透传，调用计数 = 1（R4.3）。"""
    adapter = LibAdapter(TTLCache(), settings=Settings(lib_sip2_host="lib.test"))
    route = respx.post("http://lib.test:6001/renew").mock(
        return_value=httpx.Response(409, json={"error": "已被预约"})
    )

    resp, reason = await adapter.renew("20230001", ["B001", "B002"], "key-1")
    assert resp.degrade_status == DegradeStatus.WRITE_FAILED
    assert resp.data is None
    assert reason is not None and "409" in reason
    assert route.call_count == 1  # 写失败不重试


@respx.mock
async def test_renew_not_configured_returns_write_failed():
    adapter = LibAdapter(TTLCache(), settings=Settings())
    resp, reason = await adapter.renew("20230001", ["B001"], "key-2")
    assert resp.degrade_status == DegradeStatus.WRITE_FAILED
    assert reason == "图书馆系统未接入"
