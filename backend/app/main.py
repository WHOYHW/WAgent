"""FastAPI 应用（C2 工具 Schema 导出 + 工具调用接口）。

安全约定（文档 12 §7 L4）：所有数据类接口的 ``student_id`` 一律由会话令牌注入，
请求体/查询参数中不出现 ``student_id``，从架构上根除越权。

本地启动（backend 目录下）：``uvicorn app.main:app --reload``
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .adapters import EduAdapter, LibAdapter
from .cache import make_cache
from .config import settings
from .db import get_db, init_db
from .schemas import (
    FactSummary,
    HandoffCreateParams,
    HandoffPriority,
    LibraryLoansParams,
    LibraryRenewParams,
    ReminderChannel,
    ReminderSubject,
    TimetableQueryParams,
    GradeQueryParams,
)
from .security import get_current_student
from .tools import (
    PreconditionError,
    Services,
    ToolContext,
    ToolError,
    build_registry,
)

# ---- 共享缓存与适配器（跨请求复用缓存，降级语义生效）----
# cache_backend=redis 时用 Redis（agent-redis 容器），不可达自动降级进程内缓存。
_shared_cache = make_cache(settings)
_shared_services = Services(
    cache=_shared_cache,
    settings=settings,
    edu=EduAdapter(_shared_cache, settings=settings),
    lib=LibAdapter(_shared_cache, settings=settings),
)
# 续借幂等与会话频控必须跨请求存活（AC2.2 / R2.4）：
# 进程内共享；生产迁移 Redis / DB 唯一约束（见 Services 注释）。
_shared_idempotency: dict = {}
_shared_renew_counts: dict[str, int] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_dev:
        # 开发/测试：SQLite 自动建表；生产走 Alembic 迁移（AC3.1/3.2）。
        await init_db()
    yield
    # 释放共享适配器的 HTTP 连接池。
    await _shared_services.edu.aclose()
    await _shared_services.lib.aclose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


def _services(db: AsyncSession = Depends(get_db)) -> Services:
    """按请求构造 Services：请求级数据库会话 + 进程级缓存/适配器/幂等/频控。"""
    return Services(
        cache=_shared_cache,
        settings=settings,
        edu=_shared_services.edu,
        lib=_shared_services.lib,
        db=db,
        idempotency=_shared_idempotency,
        renew_session_count=_shared_renew_counts,
    )


# ---- 请求体模型（不含 student_id，身份由令牌注入）----


class TimetableRequest(BaseModel):
    start_date: date
    end_date: date
    course_name: str | None = None


class GradeRequest(BaseModel):
    term: str | None = None
    course_name: str | None = None
    aggregate: bool = False


class LoansRequest(BaseModel):
    include_history: bool = False


class RenewRequest(BaseModel):
    barcodes: list[str] = Field(min_length=1, max_length=10)
    idempotency_key: str
    confirmed: bool = False  # 二次确认态（R2.4：已展示新到期日并获用户确认）


class SubscribeRequest(BaseModel):
    subject: ReminderSubject
    timing: list[str] | None = None
    channel: ReminderChannel = ReminderChannel.WECOM
    enabled: bool = True


class HandoffRequest(BaseModel):
    session_id: str
    user_question: str
    intent_history: list[str] = []
    hit_rules: list[str] = Field(min_length=1)
    retrieved_facts: list[FactSummary] = []
    agent_response_log: str
    suggested_owner: str | None = None
    priority: HandoffPriority


# ---- 元数据与 Schema 导出 ----

_registry = build_registry(_shared_services)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "env": settings.env}


@app.get("/tools")
async def list_tools() -> list[dict]:
    """6 个工具的完整 Schema（含前置/后置条件）。"""
    return [spec.to_dict() for spec in _registry.specs()]


@app.get("/tools/openai")
async def list_openai_tools() -> list[dict]:
    """OpenAI Function Calling 兼容的 tools 参数（R2.1）。"""
    return _registry.openai_tools()


# ---- 工具调用接口（student_id 由令牌注入）----


@app.post("/agent/timetable")
async def timetable(
    body: TimetableRequest,
    student_id: str = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    return await _dispatch(
        db, student_id, "timetable_query",
        TimetableQueryParams(
            student_id=student_id,
            start_date=body.start_date,
            end_date=body.end_date,
            course_name=body.course_name,
        ),
    )


@app.post("/agent/grades")
async def grades(
    body: GradeRequest,
    student_id: str = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    return await _dispatch(
        db, student_id, "grade_query",
        GradeQueryParams(
            student_id=student_id, term=body.term,
            course_name=body.course_name, aggregate=body.aggregate,
        ),
    )


@app.post("/agent/library/loans")
async def loans(
    body: LoansRequest,
    student_id: str = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    return await _dispatch(
        db, student_id, "library_loans",
        LibraryLoansParams(student_id=student_id, include_history=body.include_history),
    )


@app.post("/agent/library/renew")
async def renew(
    body: RenewRequest,
    student_id: str = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    return await _dispatch(
        db, student_id, "library_renew",
        LibraryRenewParams(
            student_id=student_id,
            barcodes=body.barcodes,
            idempotency_key=body.idempotency_key,
            confirmed=body.confirmed,
        ),
    )


@app.post("/agent/reminder/subscribe")
async def subscribe(
    body: SubscribeRequest,
    student_id: str = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    from .schemas import ReminderSubscribeParams

    return await _dispatch(
        db, student_id, "reminder_subscribe",
        ReminderSubscribeParams(
            student_id=student_id, subject=body.subject,
            timing=body.timing, channel=body.channel, enabled=body.enabled,
        ),
    )


@app.post("/agent/handoff")
async def handoff(
    body: HandoffRequest,
    student_id: str = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    return await _dispatch(
        db, student_id, "handoff_create",
        HandoffCreateParams(
            student_id=student_id, session_id=body.session_id,
            user_question=body.user_question, intent_history=body.intent_history,
            hit_rules=body.hit_rules, retrieved_facts=body.retrieved_facts,
            agent_response_log=body.agent_response_log,
            suggested_owner=body.suggested_owner, priority=body.priority,
        ),
    )


async def _dispatch(db: AsyncSession, student_id: str, tool: str, params):
    svc = _services(db)
    ctx = ToolContext(student_id=student_id, services=svc)
    try:
        return await _registry.dispatch(tool, ctx, params)
    except PreconditionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
