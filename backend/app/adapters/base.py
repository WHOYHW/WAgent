"""适配器基类：统一「缓存 → 源系统 → 降级」链路（C4/C5）。

核心方法 :meth:`_fetch_with_cache` 固化文档 12 §5 的降级流程图：
缓存命中未过期 → FRESH；源系统调用（读重试 2 次）成功 → LIVE_OK 并写缓存；
源失败且有历史缓存 → STALE_CACHE；源失败且无缓存 → UNAVAILABLE。
写操作（续借）不重试（R4.3），失败直接 WRITE_FAILED。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..cache import TTLCache
from ..config import Settings, settings as default_settings
from ..schemas.meta import AdapterResponse, DegradeStatus, ToolSource

class SourceNotConfiguredError(Exception):
    """源系统未配置（EDU_API_BASE_URL / LIB_SIP2_HOST 为空）。不重试。"""


# 读源系统可重试的异常（超时/网络传输层）。
_RETRYABLE = (httpx.TimeoutException, httpx.TransportError)
# 触发降级链路的异常：可重试异常 + 源系统 4xx/5xx（HTTPStatusError，不重试直接降级）
# + 未配置。文档 12 §5：源 5xx 与超时同等进入 S2/S3 降级路径（R4.4 无静默失败）。
_DEGRADEABLE = (*_RETRYABLE, httpx.HTTPStatusError, SourceNotConfiguredError)


FetchFn = Callable[[], Awaitable[tuple[Any, datetime]]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BaseAdapter(ABC):
    source: ToolSource

    def __init__(
        self,
        cache: TTLCache | None = None,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.cache = cache or TTLCache()
        self._client = client
        self.settings = settings or default_settings

    # ---- 源系统调用 ----

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.source_timeout_seconds,
                headers=self._auth_headers(),
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        return {}

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        client = await self._get_client()
        resp = await client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """读源系统调用，指数退避重试（R4.3：2 次，200ms → 600ms）。"""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.settings.read_retry_times + 1),
            wait=wait_exponential(
                multiplier=self.settings.retry_backoff_initial,
                min=self.settings.retry_backoff_initial,
                max=self.settings.retry_backoff_max,
            ),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        ):
            with attempt:
                return await self._request(method, url, **kwargs)
        raise RuntimeError("unreachable")  # pragma: no cover

    # ---- 统一降级链路 ----

    async def _fetch_with_cache(
        self,
        cache_key: str,
        ttl_seconds: int,
        fetch_fn: FetchFn,
    ) -> AdapterResponse:
        """按文档 12 §5 流程返回 AdapterResponse。

        ``ttl_seconds <= 0`` 表示不缓存（成绩明细，红线 R3）：不走缓存读写，
        每次实时拉取，失败直接 UNAVAILABLE（无 is_stale）。
        """
        if ttl_seconds > 0:
            hit, value, synced_at = self.cache.get(cache_key)
            if hit:
                return AdapterResponse(
                    data=value,
                    degrade_status=DegradeStatus.FRESH,
                    synced_at=synced_at,
                    source=self.source,
                )

        try:
            data, synced_at = await fetch_fn()
        except _DEGRADEABLE:
            # 统一降级链路（超时/源 5xx/未配置）：有历史缓存 → STALE_CACHE，否则 UNAVAILABLE。
            if ttl_seconds > 0:
                exists, stale_value, stale_at = self.cache.get_stale(cache_key)
                if exists:
                    return AdapterResponse(
                        data=stale_value,
                        degrade_status=DegradeStatus.STALE_CACHE,
                        synced_at=stale_at,
                        source=self.source,
                    )
            return AdapterResponse(
                data=None,
                degrade_status=DegradeStatus.UNAVAILABLE,
                synced_at=utcnow(),
                source=self.source,
            )

        if ttl_seconds > 0:
            self.cache.set(cache_key, data, ttl_seconds, synced_at)
        return AdapterResponse(
            data=data,
            degrade_status=DegradeStatus.LIVE_OK,
            synced_at=synced_at,
            source=self.source,
        )

    @staticmethod
    def _parse_json(resp: httpx.Response) -> Any:
        return json.loads(resp.text)

    async def aclose(self) -> None:
        """关闭懒创建的底层 HTTP 客户端（应用 shutdown 时调用，防连接泄漏）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
