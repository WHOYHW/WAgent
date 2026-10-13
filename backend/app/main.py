"""FastAPI 应用（C2 工具 Schema 导出 + 工具调用接口）。

安全约定（文档 12 §7 L4）：所有数据类接口的 ``student_id`` 一律由会话令牌注入，
请求体/查询参数中不出现 ``student_id``，从架构上根除越权。

本地启动（backend 目录下）：``uvicorn app.main:app --reload``
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, suppress
from datetime import date
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .adapters import EduAdapter, LibAdapter
from .cache import make_cache
from .config import settings
from .conversation import SessionStore
from .db import get_db, init_db
from .llm.adapter import ArkLLMClient
from .logging_util import get_logger
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
from .orchestrator import Event, LLMClient, Orchestrator
from .tools import (
    PreconditionError,
    Services,
    ToolContext,
    ToolError,
    build_registry,
)

logger = get_logger("api")

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

# LLM 客户端：进程级单例（SDK 连接池复用），lifespan shutdown 统一 aclose。
_llm_client = ArkLLMClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_dev:
        # 开发/测试：SQLite 自动建表；生产走 Alembic 迁移（AC3.1/3.2）。
        await init_db()
    if not _llm_client.is_configured:
        # P5：不阻止启动——红线拦截等 LLM 零参与路径不受影响（07 设计 F5）。
        logger.warning(
            "llm_not_configured",
            hint="缺少 LLM_API_BASE/LLM_API_KEY/LLM_MODEL，仅确定性路径可用",
        )
    yield
    # 释放共享适配器与 LLM 客户端的 HTTP 连接池。
    await _shared_services.edu.aclose()
    await _shared_services.lib.aclose()
    await _llm_client.aclose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

if settings.cors_allow_origins:
    # 配置门控（07 设计 F6）：空 = 不挂中间件，生产默认行为不变；
    # 仅放行对话所需最小面（POST/预检 + 身份/内容类型头），不带凭证。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()
        ],
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


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


# ---- 依赖注入点（07 设计 §2.4：测试经 dependency_overrides 整体替换）----


def get_llm() -> ArkLLMClient:
    """LLM 注入点：进程级单例直通。"""
    return _llm_client


def get_orchestrator(
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm),
) -> Orchestrator:
    """请求级编排器装配：db 会话 → Services → 注册表 → 会话存储 → Orchestrator。

    镜像 ``_services`` 的装配哲学，但挂成 FastAPI 依赖——API 层可测试性
    的唯一结构性要求（测试零 monkeypatch，只覆盖本依赖）。
    """
    services = _services(db)
    registry = build_registry(services)
    store = SessionStore(db, settings)
    return Orchestrator(store, registry, llm)


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


class ChatRequest(BaseModel):
    """对话入口请求体。``session_id`` 由 done 帧回传，前端续用；
    null/失效/所有者不匹配 → 静默新建。身份字段一律来自 JWT，不接受请求体。"""

    session_id: str | None = None
    content: str = Field(min_length=1, max_length=2000)


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


# ---- 对话入口（SSE，产品面）----


def _sse_frame(ev: Event) -> str:
    """单帧序列化（纯函数，07 设计 F2）：Event.data 原样 + 非空信封字段合入。"""
    payload = dict(ev.data)
    if ev.session_id:
        payload["session_id"] = ev.session_id
    if ev.trace_id:
        payload["trace_id"] = ev.trace_id
    return f"event: {ev.type.value}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _sse(agen) -> AsyncIterator[str]:
    """六类 Event 流 → SSE 帧流。

    断线清理（03 难点 2 的履约）：客户端断开时 Starlette 关闭本生成器，
    finally 内由端点（而非编排层）负责 ``aclose``；``suppress`` 兜底防止
    清理本身炸裂。中间消息已由编排层即时落库，断线最多丢最终 assistant
    消息一条——"宁缺半截"。
    """
    try:
        async for ev in agen:
            yield _sse_frame(ev)
    finally:
        with suppress(Exception):
            await agen.aclose()


@app.post("/agent/chat")
async def chat(
    body: ChatRequest,
    student_id: str = Depends(get_current_student),
    orch: Orchestrator = Depends(get_orchestrator),
):
    """对话主入口（07 设计 §2.2）：401/422 在流开始前返回；流一旦开始
    HTTP 恒 200，一切失败以 error 帧表达，前端以 done/error 帧为终态。"""
    return StreamingResponse(
        _sse(orch.run(student_id, body.content, body.session_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
