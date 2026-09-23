"""教务数据适配器（文档 12 §2.2 EduAdapter）。

- 课表：缓存 TTL 15min（调停课高发期 5min）。
- 成绩：不缓存明细（红线 R3），每次实时拉取。
- 源系统留空（EDU_API_BASE_URL 未配置）走降级适配器 → UNAVAILABLE / STALE_CACHE。
"""

from __future__ import annotations

from datetime import date, datetime

import httpx

from ..schemas.grade import GradeItem
from ..schemas.meta import AdapterResponse, ToolSource
from ..schemas.timetable import TimetableItem
from ..stale import ttl_for_domain
from .base import BaseAdapter, SourceNotConfiguredError, utcnow


class EduAdapter(BaseAdapter):
    source = ToolSource.EDU_SYSTEM

    def _auth_headers(self) -> dict[str, str]:
        if self.settings.edu_api_token:
            return {"Authorization": f"Bearer {self.settings.edu_api_token}"}
        return {}

    def _base_url(self) -> str:
        if not self.settings.edu_api_base_url:
            raise SourceNotConfiguredError("EDU_API_BASE_URL 未配置")
        return self.settings.edu_api_base_url.rstrip("/")

    # ---- 课表 ----

    async def fetch_timetable(
        self,
        student_id: str,
        start_date: date,
        end_date: date,
        course_name: str | None = None,
    ) -> AdapterResponse[list[TimetableItem]]:
        ttl = ttl_for_domain("timetable", self.settings)
        key = f"timetable:{student_id}:{start_date}:{end_date}:{course_name or ''}"
        return await self._fetch_with_cache(
            key,
            ttl,
            lambda: self._fetch_timetable_live(student_id, start_date, end_date, course_name),
        )

    async def _fetch_timetable_live(
        self, student_id: str, start_date: date, end_date: date, course_name: str | None
    ) -> tuple[list[TimetableItem], datetime]:
        url = f"{self._base_url()}/timetable"
        resp = await self._request_with_retry(
            "GET",
            url,
            params={
                "student_id": student_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "course_name": course_name,
            },
        )
        payload = self._parse_json(resp)
        items = [TimetableItem.model_validate(it) for it in payload.get("items", [])]
        return items, utcnow()

    # ---- 成绩 ----

    async def fetch_grades(
        self, student_id: str, term: str | None = None, course_name: str | None = None
    ) -> AdapterResponse[list[GradeItem]]:
        # 成绩不缓存（ttl=0），每次实时拉取；失败无缓存可回退 → UNAVAILABLE。
        return await self._fetch_with_cache(
            "grade:nocache",  # key 仅占位，ttl=0 时不读缓存
            0,
            lambda: self._fetch_grades_live(student_id, term, course_name),
        )

    async def _fetch_grades_live(
        self, student_id: str, term: str | None, course_name: str | None
    ) -> tuple[list[GradeItem], datetime]:
        url = f"{self._base_url()}/grades"
        resp = await self._request_with_retry(
            "GET",
            url,
            params={
                "student_id": student_id,
                "term": term,
                "course_name": course_name,
            },
        )
        payload = self._parse_json(resp)
        items = [GradeItem.model_validate(it) for it in payload.get("items", [])]
        return items, utcnow()
