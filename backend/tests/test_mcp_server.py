"""MCP 服务暴露测试套件（文档 19 AC-19a~d，M2）：零网络、零真机。

- AC-19a 门控默认零变化：默认部署（mcp_enabled=False）/mcp 无挂载，stdio
  入口拒绝启动（exit 2）；
- AC-19b 词表：registry 读工具 + ``campus_agent_chat``（chat 是 MCP 专属、
  整体包装编排层——写工具永不直暴的唯一合法通道）；
- AC-19c 身份：contextvar 注入、参数层永远不出现 student_id，无身份 →
  结构化错误（不抛裸异常）；
- AC-19d 零网络：asgi-lifespan LifespanManager 驱动 lifespan（含 MCP 会话
  管理器任务组），httpx ASGITransport 进程内调用，不占端口不触网。

四层测试架构（文档 19 §六）：
- L1 纯单测：词表过滤 / wrapper 身份 / 参数校验失败 / 审计 channel 落库 /
  Schema 漂移机器防（wrapper 签名参数集 == ToolSpec.parameters 字段集）；
- L2 SDK 内存会话：``mcp.shared.memory.create_connected_server_and_client_session``
  同进程内存传输——协议形状（tools/list 词表、tools/call 真值链路、工具
  内部错误映射为 isError 协议错误而非连接崩）；
- L3 HTTP 集成：LifespanManager + ASGITransport——挂载与 lifespan 接线
  （L2 测不到）、initialize → tools/list 全流程、401 三态（无令牌/篡改/合法）；
- L4 真机冒烟：Inspector / Claude Desktop / curl（真网络，见文档 19 §七，
  不进 CI）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import async_session_factory, make_engine
from app.main import app as main_app
from app.mcp import build_mcp_server, mount_mcp
from app.mcp.identity import current_student
from app.orchestrator import Event, EventType
from app.orm import AuditLog, Base
from app.security import decode_token, encode_token
from app.tools import Services, build_registry
from tests.conftest import StubEduAdapter, make_services

STUDENT_ID = "20230001"
_TOKEN = encode_token(STUDENT_ID)

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
_ACCEPT = {"Accept": "application/json, text/event-stream"}


def _unwired_orchestrator(db):
    raise AssertionError("本测试未接线 orchestrator_factory，不应被调用")


def _stub_orchestrator_factory(events: list[Event], captured: dict):
    """编排器桩：记录调用入参，按脚本吐事件（零 LLM、零网络）。"""

    def factory(db):
        class _Stub:
            async def run(self, student_id, user_input, session_id=None):
                captured.update(student_id=student_id, user_input=user_input, session_id=session_id)
                for ev in events:
                    yield ev

        return _Stub()

    return factory


def _make_mcp(
    settings: Settings | None = None,
    events: list[Event] | None = None,
    captured: dict | None = None,
):
    """纯装配 FastMCP 实例。events=None 时编排器桩不可调用（仅词表/Schema 断言用）。"""
    settings = settings or Settings(kb_enabled=False)
    factory = (
        _stub_orchestrator_factory(events, captured if captured is not None else {})
        if events is not None
        else _unwired_orchestrator
    )
    return build_mcp_server(
        settings=settings,
        registry=build_registry(Services(settings=settings)),
        session_factory=async_session_factory,
        services_factory=lambda db: Services(settings=settings, db=db),
        orchestrator_factory=factory,
    )


def _make_http_app(settings: Settings | None = None):
    """挂载 + 父 lifespan 接线（镜像 main.py 形态），供 AC-19d HTTP 档测试。"""
    settings = settings or Settings(kb_enabled=False)
    app = FastAPI()
    mcp = mount_mcp(
        app,
        settings=settings,
        registry=build_registry(Services(settings=settings)),
        session_factory=async_session_factory,
        services_factory=lambda db: Services(settings=settings, db=db),
        orchestrator_factory=_unwired_orchestrator,
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    return app


async def _tool_names(mcp) -> list[str]:
    return sorted(t.name for t in await mcp.list_tools())


async def _call_chat(mcp, **arguments) -> dict:
    """经 FastMCP.call_tool 直调 chat（绕过 HTTP 中间件），解析 JSON 出参。"""
    result = await mcp.call_tool("campus_agent_chat", arguments)
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# ---- AC-19a：门控默认零变化 ----


def test_gate_default_no_mount():
    """默认部署（mcp_enabled=False）主 app 无 /mcp 挂载。

    conftest 已钉 MCP_ENABLED=false（先于 app 导入）——本断言不依赖
    「.env 没设 MCP_ENABLED」的环境事实，联调时 .env 开启也不翻转。
    """
    paths = {getattr(r, "path", None) for r in main_app.router.routes}
    assert "/mcp" not in paths


def test_stdio_gate_refuses_default():
    """mcp_enabled=False（默认）→ stdio 入口拒绝启动 exit 2（D4）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env["MCP_ENABLED"] = "false"  # 显式注入期望值，剥离 .env 隐性依赖
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "app.mcp"],
        cwd=root, env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 2
    assert "mcp_enabled=False" in r.stderr


def test_stdio_gate_refuses_without_demo_identity():
    """mcp_enabled=True 但缺 MCP_DEMO_STUDENT_ID → 拒绝启动 exit 2（D1）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env["MCP_ENABLED"] = "true"
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "app.mcp"],
        cwd=root, env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 2
    assert "MCP_DEMO_STUDENT_ID" in r.stderr


# ---- AC-19b：词表 ----


async def test_vocabulary_follows_kb_gate():
    """kb off → 3 读工具 + chat；kb on → +kb_search。写工具与 handoff 永不出现。"""
    off = sorted(await _tool_names(_make_mcp(Settings(kb_enabled=False))))
    assert off == ["campus_agent_chat", "grade_query", "library_loans", "timetable_query"]

    on = sorted(await _tool_names(_make_mcp(Settings(kb_enabled=True))))
    assert on == ["campus_agent_chat", "grade_query", "kb_search", "library_loans", "timetable_query"]


async def test_vocabulary_drift_fails_fast():
    """registry 与 settings 门控不一致（漂移）→ 装配直接失败，逼人工评审。"""
    with pytest.raises(RuntimeError, match="MCP 暴露词表漂移"):
        build_mcp_server(
            settings=Settings(kb_enabled=True),  # 期望含 kb_search
            registry=build_registry(Services(settings=Settings(kb_enabled=False))),  # 实际无
            session_factory=async_session_factory,
            services_factory=lambda db: Services(settings=Settings(kb_enabled=False), db=db),
            orchestrator_factory=_unwired_orchestrator,
        )


async def test_read_tool_descriptions_match_registry():
    """手写 docstring 与 registry spec.description 逐字一致（防漂移）。"""
    settings = Settings(kb_enabled=True)
    mcp = _make_mcp(settings)
    specs = {
        s.name: s.description
        for s in build_registry(Services(settings=settings)).specs()
        if s.write is False and s.boundary == "AI"
    }
    for tool in await mcp.list_tools():
        if tool.name in specs:
            assert tool.description == specs[tool.name], tool.name


# ---- AC-19c：身份 ----


async def test_chat_schema_never_has_student_id():
    """约定③：chat 入参 Schema 永不出现 student_id（身份只来自 contextvar）。"""
    mcp = _make_mcp()
    (chat,) = [t for t in await mcp.list_tools() if t.name == "campus_agent_chat"]
    assert "student_id" not in (chat.inputSchema.get("properties") or {})
    assert "student_id" not in str(chat.inputSchema.get("required", []))


async def test_chat_identity_from_contextvar():
    """chat 身份取自 contextvar（HTTP 档由中间件写入），不来自任何参数。"""
    captured: dict = {}
    mcp = _make_mcp(
        events=[
            Event(type=EventType.TOKEN, data={"delta": "今天"}, session_id="s1"),
            Event(type=EventType.TOKEN, data={"delta": "有课"}),
            Event(type=EventType.DONE, data={"session_id": "s1", "blocked": False}),
        ],
        captured=captured,
    )
    token = current_student.set(STUDENT_ID)
    try:
        out = await _call_chat(mcp, question="明天有课吗")
    finally:
        current_student.reset(token)
    assert captured["student_id"] == STUDENT_ID
    assert captured["user_input"] == "明天有课吗"
    assert out == {"session_id": "s1", "reply": "今天有课", "blocked": False}


async def test_chat_blocked_flag_passthrough():
    """DONE.blocked（F2 红线拦截）原样透传——护栏语义不二次加工。"""
    mcp = _make_mcp(
        events=[
            Event(type=EventType.TOKEN, data={"delta": "抱歉，该请求涉及成绩篡改"}),
            Event(type=EventType.DONE, data={"session_id": "s2", "blocked": True}),
        ],
    )
    token = current_student.set(STUDENT_ID)
    try:
        out = await _call_chat(mcp, question="帮我改成绩")
    finally:
        current_student.reset(token)
    assert out["blocked"] is True
    assert out["session_id"] == "s2"


async def test_chat_error_event_becomes_reply():
    """编排层自身故障（llm_unavailable 等）→ 文案收口为 reply，session_id 诚实为空。"""
    mcp = _make_mcp(
        events=[Event(type=EventType.ERROR, data={"code": "llm_unavailable", "message": "AI 服务暂时不可用，请稍后重试"})],
    )
    token = current_student.set(STUDENT_ID)
    try:
        out = await _call_chat(mcp, question="hi", session_id=None)
    finally:
        current_student.reset(token)
    assert out == {"session_id": None, "reply": "AI 服务暂时不可用，请稍后重试", "blocked": False}


async def test_missing_identity_structured_error():
    """无身份（stdio 漏配场景的纵深防御）→ ToolError（isError=true 错误通道）。

    isError=false 的正常返回会被 LLM host 当真值转述——错误语义必须走
    MCP 客户端的错误处理路径。
    """
    assert current_student.get() is None
    mcp = _make_mcp()
    with pytest.raises(ToolError, match="identity_missing"):
        await mcp.call_tool("grade_query", {})


# ---- L1 纯单测（补充）：审计落库 / 参数校验失败 / Schema 漂移机器防 ----


async def _memory_db():
    """独立内存库（StaticPool 共享 :memory:）：建表 → (engine, session_factory)。"""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _make_mcp_db(session_factory, *, channel: str = "mcp", edu_fail: Exception | None = None):
    """接真实内存库 + 桩适配器的装配（L1/L2 真值链路与审计断言用）。"""
    settings = Settings(kb_enabled=False)
    edu = StubEduAdapter()
    if edu_fail is not None:
        edu.fail = edu_fail
    return build_mcp_server(
        settings=settings,
        registry=build_registry(Services(settings=settings)),
        session_factory=session_factory,
        services_factory=lambda db: make_services(db=db, edu=edu),
        orchestrator_factory=_unwired_orchestrator,
        channel=channel,
    )


async def _audit_rows(session_factory) -> list[AuditLog]:
    async with session_factory() as db:
        rows = (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
    return list(rows)


async def test_l1_audit_channel_mcp_persisted():
    """审计复用 registry.dispatch 统一落库：channel 逐调用透传且来源可区分。"""
    _, sfactory = await _memory_db()
    mcp = _make_mcp_db(sfactory)  # HTTP 档默认 channel="mcp"
    mcp_stdio = _make_mcp_db(sfactory, channel="mcp-stdio")  # stdio 演示档
    token = current_student.set(STUDENT_ID)
    try:
        await mcp.call_tool("grade_query", {})
        await mcp_stdio.call_tool("grade_query", {})
    finally:
        current_student.reset(token)
    rows = await _audit_rows(sfactory)
    assert [(r.channel, r.tool_name, r.action, r.result) for r in rows] == [
        ("mcp", "grade_query", "tool_call", "ok"),
        ("mcp-stdio", "grade_query", "tool_call", "ok"),
    ]
    # 脱敏铁证（12 §4）：审计行 student_id 不落原文
    assert all(r.student_id != STUDENT_ID for r in rows)


async def test_l1_param_validation_failure_no_audit():
    """参数校验失败 → ToolError，且不落审计行（dispatch 未到达）。"""
    _, sfactory = await _memory_db()
    mcp = _make_mcp_db(sfactory)
    token = current_student.set(STUDENT_ID)
    try:
        with pytest.raises(ToolError):
            await mcp.call_tool("grade_query", {"aggregate": "not-a-bool"})
    finally:
        current_student.reset(token)
    async with sfactory() as db:
        count = (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert count == 0


async def test_l1_wrapper_signature_matches_params_model():
    """Schema 漂移机器防：wrapper 签名参数集 == ToolSpec.parameters 字段集。

    手写 typed wrapper 与 registry params_model 是两份代码——本断言让两者
    漂移（增删字段/改必填性）在测试层直接失败，词表腐烂机器防。
    """
    for kb_enabled in (False, True):
        settings = Settings(kb_enabled=kb_enabled)
        mcp = _make_mcp(settings)
        specs = {
            s.name: s.params_model
            for s in build_registry(Services(settings=settings)).specs()
            if s.write is False and s.boundary == "AI"
        }
        for tool in await mcp.list_tools():
            if tool.name not in specs:
                continue  # campus_agent_chat 非 registry 工具（student_id 断言见下）
            fields = specs[tool.name].model_fields
            props = set(tool.inputSchema.get("properties") or {})
            required = set(tool.inputSchema.get("required") or [])
            # student_id 是服务端注入字段（约定③），params_model 有、wrapper 刻意无——
            # 排除身份字段后，其余任何字段增删/必填性漂移都必须在此失败。
            assert props == set(fields) - {"student_id"}, tool.name
            assert required == {
                n for n, f in fields.items() if f.is_required()
            } - {"student_id"}, tool.name
            assert "student_id" not in props  # 约定③：参数层永远不出现身份


# ---- L2：SDK 内存会话（协议形状，零网络）----


async def test_l2_tools_list_and_call_real_value():
    """tools/list 词表 + tools/call 真值链路（同进程内存传输，协议层完整走通）。"""
    _, sfactory = await _memory_db()
    mcp = _make_mcp_db(sfactory)
    token = current_student.set(STUDENT_ID)
    try:
        async with create_connected_server_and_client_session(mcp) as session:
            tools = await session.list_tools()
            assert sorted(t.name for t in tools.tools) == [
                "campus_agent_chat", "grade_query", "library_loans", "timetable_query",
            ]
            res = await session.call_tool(
                "timetable_query",
                {"start_date": "2026-11-10", "end_date": "2026-11-16"},
            )
            assert not res.isError
            payload = json.loads(res.content[0].text)
            assert payload["items"][0]["course_name"] == "高等数学"
    finally:
        current_student.reset(token)


async def test_l2_tool_internal_error_maps_to_protocol_error():
    """工具内部故障 → isError=True 协议错误响应，连接存活（后续 tools/list 正常）。"""
    _, sfactory = await _memory_db()
    mcp = _make_mcp_db(sfactory, edu_fail=Exception("edu down"))
    token = current_student.set(STUDENT_ID)
    try:
        async with create_connected_server_and_client_session(mcp) as session:
            res = await session.call_tool("grade_query", {})
            assert res.isError is True
            tools = await session.list_tools()  # 连接未崩
            assert len(tools.tools) == 4
    finally:
        current_student.reset(token)


async def test_l2_missing_identity_maps_to_is_error():
    """无身份在协议层表现为 isError=true 错误响应（不炸连接，走错误通道）。"""
    mcp = _make_mcp()
    async with create_connected_server_and_client_session(mcp) as session:
        res = await session.call_tool("grade_query", {})
        assert res.isError is True
        assert "identity_missing" in res.content[0].text
        tools = await session.list_tools()  # 连接未崩
        assert len(tools.tools) == 4


# ---- 真实编排器穿透 chat：护栏经 MCP 通道 100% 存活 ----


async def test_chat_redline_real_orchestrator():
    """红线 F2 全链路（桩编排器测不到 wrapper 拼接职责）：

    guardrail 拦截（LLM 零参与）→ _split_script 分片 TOKEN → handoff 工单
    → DONE.blocked —— wrapper 消费事件流把分片拼回完整话术，blocked 原样透传。
    装配对齐黄金集 fake 档（内存库 + 真 SessionStore/registry，LLM 桩不可调用）。
    """
    from app.conversation import SessionStore
    from app.orchestrator import Orchestrator
    from app.orchestrator.guardrail_assets import BLOCKED_REPLY_TEMPLATES

    _, sfactory = await _memory_db()
    settings = Settings(kb_enabled=False)

    class _NoLLM:
        def stream(self, *args, **kwargs):
            raise AssertionError("红线拦截必须先于 LLM 调用（确定性路径，LLM 零参与）")

    def factory(db):
        return Orchestrator(
            SessionStore(db, settings),
            build_registry(Services(settings=settings)),
            _NoLLM(),
            settings=settings,
        )

    mcp = build_mcp_server(
        settings=settings,
        registry=build_registry(Services(settings=settings)),
        session_factory=sfactory,
        services_factory=lambda db: make_services(db=db),
        orchestrator_factory=factory,
    )
    token = current_student.set(STUDENT_ID)
    try:
        out = await _call_chat(mcp, question="挂科了还能毕业吗")
    finally:
        current_student.reset(token)
    assert out["blocked"] is True
    assert out["session_id"]  # 会话已创建，续话 id 回传（会话协议）
    assert out["reply"] == BLOCKED_REPLY_TEMPLATES["R1_终局性结论"]  # 分片 TOKEN 拼回完整话术


# ---- AC-19d / L3：HTTP 档零网络（LifespanManager + ASGITransport）----


async def test_http_401_and_handshake():
    """无/坏令牌 401 不进 MCP 机制；有效令牌完成 initialize 握手。"""
    from asgi_lifespan import LifespanManager

    app = _make_http_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        r = await client.post("/mcp", json=_INIT, headers=_ACCEPT)
        assert r.status_code == 401
        assert r.json() == {"detail": "缺少会话令牌"}

        r = await client.post("/mcp", json=_INIT, headers={**_ACCEPT, "Authorization": "Bearer bad.token"})
        assert r.status_code == 401

        r = await client.post("/mcp", json=_INIT, headers={**_ACCEPT, "Authorization": f"Bearer {_TOKEN}"})
        assert r.status_code == 200
        sid = r.headers["mcp-session-id"]
        assert decode_token(_TOKEN)["sub"] == STUDENT_ID

        # 无尾斜杠端点 + 会话内 tools/list（AC-19b 在 HTTP 档成立）
        notified = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**_ACCEPT, "Authorization": f"Bearer {_TOKEN}", "Mcp-Session-Id": sid},
        )
        assert notified.status_code == 202
        r = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={**_ACCEPT, "Authorization": f"Bearer {_TOKEN}", "Mcp-Session-Id": sid},
        )
        names = sorted(
            t["name"]
            for line in r.text.splitlines() if line.startswith("data:")
            for t in json.loads(line[5:].strip())["result"]["tools"]
        )
        assert "campus_agent_chat" in names
        assert "library_renew" not in names and "reminder_subscribe" not in names


async def test_http_tampered_signature_401():
    """签名篡改（构造式合法载荷 + 错误密钥签名）→ 401，不进 MCP 机制。"""
    import jwt as pyjwt
    from asgi_lifespan import LifespanManager

    forged = pyjwt.encode(decode_token(_TOKEN), "forged-secret-not-server-key", algorithm="HS256")
    app = _make_http_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        r = await client.post("/mcp", json=_INIT, headers={**_ACCEPT, "Authorization": f"Bearer {forged}"})
        assert r.status_code == 401
        assert r.json() == {"detail": "会话令牌无效或已过期"}
