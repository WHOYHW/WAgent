"""MCP 桥接层（文档 19 §三，M2）：registry 只读工具 → MCP tools。

零网络纯装配：构建 FastMCP server 零副作用（不连库、不触网）；每调用
独立的 db 会话由 ``session_factory`` 在工具调用时打开，Services 由注入的
``services_factory`` 装配（main.py 传 ``_services``，测试传桩工厂）。

词表裁决（文档 19 §二 D2 / 附录 A）：``spec.write is False and
spec.boundary == "AI"`` 属性过滤 + **期望集冻结断言**——实际过滤结果 ≠
期望集时装配直接失败（fail fast），新工具进入读暴露面必须人工评审并更新
``_EXPECTED_BASE``，暴露面不随工具注册静默扩大。
``reminder_subscribe`` 已标 ``write=True``（语义是写：订阅库幂等 upsert），
否则属性过滤会把它错放行（M2 修正的既有标注瑕疵）。

约定③落地（D1）：身份从 JWT 传输层注入 contextvar，wrapper 里取——
工具参数层永远不出现 ``student_id``；每调用 ``channel="mcp"``，
审计免费复用 ``registry.dispatch`` 的统一审计且来源可区分。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..kb.schemas import KbSearchParams
from ..orchestrator import EventType, Orchestrator
from ..schemas import GradeQueryParams, LibraryLoansParams, TimetableQueryParams
from ..tools import Services, ToolContext, ToolRegistry
from .identity import current_student

# 期望暴露词表（文档 19 附录 A：逐个人工评审点名，kb_search 随 kb_enabled 条件加入）。
_EXPECTED_BASE = frozenset({"timetable_query", "grade_query", "library_loans"})

# 无身份时抛 ToolError（isError=true）——MCP 客户端对 isError 有专门处理
# 路径，LLM host 会把 isError=false 的正常结果当真值转述，错误语义必须走
# 错误通道。正常路径中间件已 401，本分支是纵深防御（如 stdio 入口漏配身份）。
_MISSING_IDENTITY_MESSAGE = "缺少会话身份（identity_missing）：MCP 调用必须携带有效会话令牌"


def _exposed_names(registry: ToolRegistry) -> set[str]:
    """词表过滤（D2）：只读（write=False）且 AI 边界。"""
    return {
        spec.name for spec in registry.specs() if spec.write is False and spec.boundary == "AI"
    }


def build_mcp_server(
    settings: Settings,
    registry: ToolRegistry,
    session_factory: async_sessionmaker[AsyncSession],
    services_factory: Callable[[AsyncSession], Services],
    orchestrator_factory: Callable[[AsyncSession], Orchestrator],
    channel: str = "mcp",
) -> FastMCP:
    """构建 MCP server（零副作用纯装配，文档 19 §三）。

    - ``settings``：门控与期望集的裁决来源（kb_enabled）；
    - ``registry``：词表唯一真源（工具名/Schema/描述/审计全从这里来）；
    - ``session_factory`` / ``services_factory``：每调用独立 db 会话与 Services
      装配——main.py 传 ``_services``（共享缓存/适配器单例），测试传桩工厂；
    - ``orchestrator_factory``：``campus_agent_chat`` 用——每调用以独立 db 会话
      装配请求级 Orchestrator（镜像 main.py ``get_orchestrator`` 依赖的装配）；
    - ``channel``：审计来源标识——HTTP 档 ``"mcp"``，stdio 演示档
      ``"mcp-stdio"``（dev-only 通道，__main__.py 传入）。
    """
    expected = set(_EXPECTED_BASE) | ({"kb_search"} if settings.kb_enabled else set())
    exposed = _exposed_names(registry)
    if exposed != expected:
        raise RuntimeError(
            f"MCP 暴露词表漂移：实际 {sorted(exposed)} ≠ 期望 {sorted(expected)}"
            "——新工具进入读暴露面（write=False & boundary=AI）需人工评审并更新"
            " _EXPECTED_BASE（文档 19 附录 A）"
        )

    async def dispatch(name: str, build_params: Callable[[str], BaseModel]) -> str:
        student_id = current_student.get()
        if student_id is None:
            raise ToolError(_MISSING_IDENTITY_MESSAGE)
        params = build_params(student_id)  # params_model 构造即校验（体内校验，不搞动态 schema）
        async with session_factory() as db:
            ctx = ToolContext(student_id=student_id, services=services_factory(db), channel=channel)
            result = await registry.dispatch(name, ctx, params)
        return result.model_dump_json()

    mcp = FastMCP(
        name="wagent-campus",
        instructions=(
            "WAgent 校园助手只读工具面。身份由会话令牌注入：参数里不存在 "
            "student_id，所有查询只返回令牌主体本人的数据。"
        ),
    )

    @mcp.tool()
    async def timetable_query(
        start_date: date, end_date: date, course_name: str | None = None
    ) -> str:
        """查询学生本人的课表。仅返回调用者本人的数据。"""
        return await dispatch(
            "timetable_query",
            lambda sid: TimetableQueryParams(
                student_id=sid, start_date=start_date, end_date=end_date, course_name=course_name
            ),
        )

    @mcp.tool()
    async def grade_query(
        term: str | None = None, course_name: str | None = None, aggregate: bool = False
    ) -> str:
        """查询学生本人的已发布成绩。不得缓存明细，不得写入长期记忆。"""
        return await dispatch(
            "grade_query",
            lambda sid: GradeQueryParams(
                student_id=sid, term=term, course_name=course_name, aggregate=aggregate
            ),
        )

    @mcp.tool()
    async def library_loans(include_history: bool = False) -> str:
        """查询学生本人的在借图书清单与可续借资格。"""
        return await dispatch(
            "library_loans",
            lambda sid: LibraryLoansParams(student_id=sid, include_history=include_history),
        )

    if settings.kb_enabled:

        @mcp.tool()
        async def kb_search(query: str, top_k: int | None = None) -> str:
            """检索学校规章制度与办事指南公开知识库（转专业、休学复学、证件补办、开放时间等）。未命中就如实说明未找到，不要编造。"""
            # kb 语料是公开制度库：入参契约无 student_id（约定③从根源不适用，
            # 文档 18 §五），ctx.student_id 仅用于审计归属。
            return await dispatch(
                "kb_search",
                lambda sid: KbSearchParams(query=query, top_k=top_k),
            )

    @mcp.tool()
    async def campus_agent_chat(question: str, session_id: str | None = None) -> str:
        """与校园助手进行完整对话（唯一带全部安全护栏的通道：红线拦截、写操作确认门禁、预算熔断，续借/订阅等写操作请通过本工具的会话协议完成）。返回 JSON：session_id（继续对话时原样回传）、reply（回复全文）、blocked（本次回复是否被红线规则拦截）。"""
        # 整体包装 Orchestrator.run()（文档 19 §三 chat）：写工具永不直暴——
        # 想 renew 必须走 chat 的会话协议（F4b 确认门禁在编排层落地，MCP 直调
        # 绕过确认即不可接受）。身份同 contextvar；事件流收口为三元组。
        student_id = current_student.get()
        if student_id is None:
            raise ToolError(_MISSING_IDENTITY_MESSAGE)
        reply_parts: list[str] = []
        done: dict | None = None
        async with session_factory() as db:
            orch = orchestrator_factory(db)
            async for ev in orch.run(student_id, question, session_id):
                if ev.type is EventType.TOKEN:
                    reply_parts.append(ev.data.get("delta", ""))
                elif ev.type is EventType.DONE:
                    done = ev.data
                elif ev.type is EventType.ERROR:
                    # 编排层自身故障（llm_unavailable / storage_error / internal）：
                    # 文案已是用户可读的诚实话术，直接作为 reply 收口
                    reply_parts.append(ev.data.get("message", ""))
        done = done or {}
        return json.dumps(
            {
                "session_id": done.get("session_id"),
                "reply": "".join(reply_parts),
                "blocked": bool(done.get("blocked", False)),
            },
            ensure_ascii=False,
        )

    return mcp
