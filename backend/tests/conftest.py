"""测试公共夹具：内存 SQLite + 会话 + 桩适配器。

运行（backend 目录下）：``pytest``
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cache import TTLCache  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import make_engine  # noqa: E402
from app.orm import Base  # noqa: E402
from app.schemas.meta import AdapterResponse, DegradeStatus, ToolSource  # noqa: E402
from app.schemas.timetable import TimetableItem  # noqa: E402
from app.tools.base import Services  # noqa: E402


class StubEduAdapter:
    """教务桩：返回固定课表，可注入失败。"""

    source = ToolSource.EDU_SYSTEM

    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.calls = 0

    async def fetch_timetable(self, student_id, start_date, end_date, course_name=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        item = TimetableItem(
            course_name="高等数学", course_code="MA101", teacher="张老师",
            location="一教-101", weekday=1, period_start=1, period_end=2,
            weeks="1-16 周",
        )
        return AdapterResponse(
            data=[item],
            degrade_status=DegradeStatus.LIVE_OK,
            synced_at=datetime.now(UTC),
            source=self.source,
        )

    async def fetch_grades(self, student_id, term=None, course_name=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return AdapterResponse(
            data=[], degrade_status=DegradeStatus.LIVE_OK,
            synced_at=datetime.now(UTC), source=self.source,
        )

    async def aclose(self) -> None:
        return None


class StubLibAdapter:
    """图书馆桩：可配置续借结果与调用计数（AC2.2 / AC4.5 断言用）。"""

    source = ToolSource.LIBRARY_ILS

    def __init__(self) -> None:
        self.renew_calls = 0
        self.renew_ok = True
        self.renew_args: tuple | None = None  # (student_id, barcodes, idempotency_key)

    async def fetch_loans(self, student_id, include_history=False):
        return AdapterResponse(
            data=[], degrade_status=DegradeStatus.LIVE_OK,
            synced_at=datetime.now(UTC), source=self.source,
        )

    async def renew(self, student_id, barcodes, idempotency_key):
        self.renew_calls += 1
        self.renew_args = (student_id, list(barcodes), idempotency_key)
        from app.schemas.library import LibraryRenewItem

        if self.renew_ok:
            items = [LibraryRenewItem(barcode=b, ok=True, new_due_date=date(2026, 10, 1))
                     for b in barcodes]
            return AdapterResponse(
                data=items, degrade_status=DegradeStatus.LIVE_OK,
                synced_at=datetime.now(UTC), source=self.source,
            ), None
        return AdapterResponse(
            data=None, degrade_status=DegradeStatus.WRITE_FAILED,
            synced_at=datetime.now(UTC), source=self.source,
        ), "图书馆系统不可用"

    async def aclose(self) -> None:
        return None


def make_services(db=None, *, edu=None, lib=None) -> Services:
    """构造隔离的 Services（每测试独立缓存/幂等/频控）。"""
    return Services(
        cache=TTLCache(),
        settings=Settings(),
        edu=edu or StubEduAdapter(),
        lib=lib or StubLibAdapter(),
        db=db,
        idempotency={},
        renew_session_count={},
    )


@pytest_asyncio.fixture
async def db():
    """函数级内存库：建表 → 提供会话 → 释放。"""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
