"""图书馆数据适配器（文档 12 §2.2 LibAdapter）。

- 借阅：缓存 TTL 5min。
- 续借：白名单写操作，**不重试**（R4.3），失败直接 WRITE_FAILED。
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ..schemas.library import LibraryLoanItem, LibraryRenewItem
from ..schemas.meta import AdapterResponse, DegradeStatus, ToolSource
from ..stale import ttl_for_domain
from .base import BaseAdapter, SourceNotConfiguredError, utcnow


class LibAdapter(BaseAdapter):
    source = ToolSource.LIBRARY_ILS

    def _base_url(self) -> str:
        if not self.settings.lib_sip2_host:
            raise SourceNotConfiguredError("LIB_SIP2_HOST 未配置")
        host = self.settings.lib_sip2_host.rstrip("/")
        if not host.startswith("http"):
            host = f"http://{host}:{self.settings.lib_sip2_port}"
        return host

    async def fetch_loans(
        self, student_id: str, include_history: bool = False
    ) -> AdapterResponse[list[LibraryLoanItem]]:
        ttl = ttl_for_domain("library", self.settings)
        key = f"loans:{student_id}:{include_history}"
        return await self._fetch_with_cache(
            key,
            ttl,
            lambda: self._fetch_loans_live(student_id, include_history),
        )

    async def _fetch_loans_live(
        self, student_id: str, include_history: bool
    ) -> tuple[list[LibraryLoanItem], datetime]:
        url = f"{self._base_url()}/loans"
        resp = await self._request_with_retry(
            "GET", url, params={"student_id": student_id, "include_history": include_history}
        )
        payload = self._parse_json(resp)
        items = [LibraryLoanItem.model_validate(it) for it in payload.get("items", [])]
        return items, utcnow()

    # ---- 续借（写，不重试）----

    async def renew(
        self, student_id: str, barcodes: list[str], idempotency_key: str
    ) -> tuple[AdapterResponse[list[LibraryRenewItem]], str | None]:
        """续借（写，不重试）。返回 ``(封装, 失败原因)``；成功时 reason 为 None。"""
        try:
            url = f"{self._base_url()}/renew"
            resp = await self._request(
                "POST",
                url,
                json={
                    "student_id": student_id,
                    "barcodes": barcodes,
                    "idempotency_key": idempotency_key,
                },
            )
            payload = self._parse_json(resp)
            items = [LibraryRenewItem.model_validate(it) for it in payload.get("results", [])]
            return AdapterResponse(
                data=items,
                degrade_status=DegradeStatus.LIVE_OK,
                synced_at=utcnow(),
                source=self.source,
            ), None
        except (httpx.HTTPError, SourceNotConfiguredError) as exc:
            # 写失败不降级、不重试（R4.3 / AC4.5：调用计数 = 1）。
            # 覆盖超时/传输错误/源 4xx-5xx（如"已被预约"被 ILS 拒绝）/未配置。
            if isinstance(exc, httpx.HTTPStatusError):
                reason = f"图书馆系统拒绝（HTTP {exc.response.status_code}）"
            elif isinstance(exc, SourceNotConfiguredError):
                reason = "图书馆系统未接入"
            else:
                reason = "图书馆系统不可用"
            return AdapterResponse(
                data=None,
                degrade_status=DegradeStatus.WRITE_FAILED,
                synced_at=utcnow(),
                source=self.source,
            ), reason
