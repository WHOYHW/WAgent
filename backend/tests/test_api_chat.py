"""SSE 端点 API 级测试（任务设计 W4.3 · 验收 AC-IMPL07-1 ~ 10）。

首个 HTTP 层测试：``httpx.ASGITransport``（不跑 lifespan，规避 ``init_db``
触碰 PG）+ ``app.dependency_overrides`` 覆盖 ``get_orchestrator``——零
monkeypatch。分层原则：本层只测「端点装配 + 帧序列化 + 鉴权」，编排逻辑
本身已由 test_orchestrator（23 例）覆盖，FakeLLM / 桩适配器 / 内存库全部
复用既有件。
"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest_asyncio
from sqlalchemy import select

from app.config import Settings, get_settings
from app.conversation import SessionStore
from app.main import _sse, app, get_orchestrator
from app.orchestrator import Event, EventType, Orchestrator
from app.orm import AuditLog
from app.security import encode_token
from app.tools import build_registry
from tests.conftest import make_services
from tests.test_orchestrator import FakeLLM, text_script, tool_call_script

STUDENT = "20230001"
OTHER = "20239999"
TIMETABLE_ARGS = {
    "student_id": "99999999",  # 模型伪造身份：编排层应剥离并注入令牌身份
    "start_date": "2026-10-14",
    "end_date": "2026-10-15",
}


# ---- 夹具与辅助 ----


@pytest_asyncio.fixture
async def client():
    """ASGI 测试客户端（不跑 lifespan）；退出时清空依赖覆盖，隔离用例。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    app.dependency_overrides.clear()


def install_orch(db, llm) -> None:
    """覆盖 ``get_orchestrator``：内存库 + 桩适配器 + FakeLLM（AC-8 姿势）。"""

    def override() -> Orchestrator:
        services = make_services(db)
        registry = build_registry(services)
        store = SessionStore(db, Settings())
        return Orchestrator(store, registry, llm)

    app.dependency_overrides[get_orchestrator] = override


async def post_chat(client, content, *, session_id=None, student=STUDENT):
    """带会话令牌的对话请求。"""
    return await client.post(
        "/agent/chat",
        json={"session_id": session_id, "content": content},
        headers={"Authorization": f"Bearer {encode_token(student)}"},
    )


def parse_frames(text: str) -> list[tuple[str, dict]]:
    """SSE 响应体 → [(event 名, data dict)]。按空行切帧，每帧两行。"""
    frames = []
    for block in text.strip().split("\n\n"):
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        frames.append((name, data))
    return frames


def frames_of(resp) -> list[tuple[str, dict]]:
    return parse_frames(resp.text)


def done_data(frames) -> dict:
    return next(d for n, d in frames if n == "done")


def token_text(frames) -> str:
    return "".join(d["delta"] for n, d in frames if n == "token")


# ---- AC-1 正常流全链路：帧序与响应头 ----


async def test_ac1_normal_flow_frame_sequence(db, client):
    llm = FakeLLM([
        tool_call_script("timetable_query", TIMETABLE_ARGS),
        text_script("明天 2 节课：高等数学与大学物理。"),
    ])
    install_orch(db, llm)

    resp = await post_chat(client, "我明天有什么课")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"

    names = [n for n, _ in frames_of(resp)]
    assert names == ["thinking", "tool_call", "tool_result", "token", "done"]
    assert frames_of(resp)[0][1]["phase"] == "session_ready"
    # 信封字段：非 error 帧一律携带 session_id / trace_id
    for _, data in frames_of(resp):
        assert data.get("session_id")
        assert data.get("trace_id")


# ---- AC-2 帧格式忠实性：token 拼接 = 脚本文本；done 回传 session_id ----


async def test_ac2_frame_fidelity(db, client):
    reply = "你好，我是校园助手，可以查课表、成绩和图书馆借阅。"
    llm = FakeLLM([text_script(reply)])
    install_orch(db, llm)

    resp = await post_chat(client, "你好")

    frames = frames_of(resp)
    assert token_text(frames) == reply
    assert done_data(frames)["session_id"]


# ---- AC-3 红线流：LLM 零参与，done(blocked) + 工单号 ----


async def test_ac3_redline_flow(db, client):
    llm = FakeLLM([])  # 红线路径 LLM 从未被调用，脚本为空
    install_orch(db, llm)

    resp = await post_chat(client, "我的毕业资格够吗")

    frames = frames_of(resp)
    assert frames[0][0] == "thinking"  # 首帧仍是 session_ready
    assert frames[1][0] == "tool_call"
    assert frames[1][1]["tool"] == "handoff_create"
    done = done_data(frames)
    assert done["blocked"] is True
    assert done["handoff_ticket_id"]
    assert llm.calls == []  # LLM 零调用
    # 拦截话术经 token 帧流出（40 字分片可能多帧，以拼接文本断言）
    assert "终局性" in token_text(frames)


# ---- AC-4 错误流：首请求失败 → error(llm_unavailable)，无 done 帧 ----


async def test_ac4_llm_unavailable_error_frame(db, client):
    llm = FakeLLM([[RuntimeError("boom")]])
    install_orch(db, llm)

    resp = await post_chat(client, "我明天有什么课")

    assert resp.status_code == 200  # 流已开始，HTTP 恒 200
    frames = frames_of(resp)
    assert [n for n, _ in frames] == ["thinking", "error"]
    assert frames[1][1]["code"] == "llm_unavailable"


# ---- AC-5 会话续用：第二轮 LLM 上下文含第一轮消息 ----


async def test_ac5_session_reuse(db, client):
    llm = FakeLLM([
        text_script("你好，我是校园助手。"),
        text_script("请讲，你想查什么？"),
    ])
    install_orch(db, llm)

    r1 = await post_chat(client, "你好")
    sid = done_data(frames_of(r1))["session_id"]
    r2 = await post_chat(client, "我明天有什么课", session_id=sid)

    assert r2.status_code == 200
    seen = [
        (m["role"], m.get("content"))
        for m in llm.calls[1]["messages"]
    ]
    assert ("user", "你好") in seen
    assert ("assistant", "你好，我是校园助手。") in seen
    assert done_data(frames_of(r2))["session_id"] == sid  # 续用不换号


# ---- AC-6 所有者不匹配：静默新建 + owner_mismatch 审计 ----


async def test_ac6_owner_mismatch(db, client):
    llm = FakeLLM([text_script("你好。"), text_script("你好。")])
    install_orch(db, llm)

    r1 = await post_chat(client, "你好")
    sid = done_data(frames_of(r1))["session_id"]

    r2 = await post_chat(client, "你好", session_id=sid, student=OTHER)

    assert r2.status_code == 200
    assert done_data(frames_of(r2))["session_id"] != sid  # 静默新建
    rows = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "owner_mismatch")
        )
    ).scalars().all()
    assert len(rows) == 1  # 越权尝试留痕


# ---- AC-7 鉴权与校验：401/422 均在流开始前 ----


async def test_ac7_auth_and_validation(db, client):
    install_orch(db, FakeLLM([]))

    no_token = await client.post("/agent/chat", json={"content": "你好"})
    assert no_token.status_code == 401

    empty = await client.post(
        "/agent/chat",
        json={"content": ""},
        headers={"Authorization": f"Bearer {encode_token(STUDENT)}"},
    )
    assert empty.status_code == 422


# ---- AC-8 依赖注入可替换：全程仅 dependency_overrides ----


async def test_ac8_injection_via_overrides_only(db, client):
    llm = FakeLLM([text_script("你好呀。")])
    install_orch(db, llm)

    resp = await post_chat(client, "你好")

    assert resp.status_code == 200
    assert len(llm.calls) == 1  # 注入的桩确实被端点消费
    assert get_orchestrator in app.dependency_overrides


# ---- AC-9 断线清理：_sse 收尾闭合内层生成器，无异常外泄 ----


async def test_ac9_disconnect_cleanup():
    closed = False

    async def agen():
        nonlocal closed
        try:
            yield Event(
                type=EventType.TOKEN, data={"delta": "a"},
                session_id="s", trace_id="t",
            )
            yield Event(type=EventType.DONE, data={}, session_id="s")
        finally:
            closed = True

    g = _sse(agen())
    first = await g.__anext__()
    assert first.startswith("event: token")

    await g.aclose()  # 模拟 Starlette 在客户端断开时关闭响应生成器

    assert closed  # 内层 agen 被正确收尾（suppress 兜底，无异常外泄）


# ---- AC-10 CORS 门控：默认空无 CORS 头；配置后预检通过 ----


async def test_ac10_cors_default_empty(client):
    """默认（未配置 cors_allow_origins）：不挂中间件，预检无 CORS 头。"""
    resp = await client.options(
        "/agent/chat",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 405  # 无 OPTIONS 路由，落到方法不允许
    assert "access-control-allow-origin" not in resp.headers


async def test_ac10_cors_configured_preflight(monkeypatch):
    """配置后：预检 200 且仅放行最小面（POST/OPTIONS + 身份/内容类型头）。"""
    import app.config as config_mod
    import app.main as main_mod

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:5173")
    get_settings.cache_clear()
    importlib.reload(config_mod)  # 重建 settings（含 CORS）
    importlib.reload(main_mod)    # 以新 settings 重挂中间件
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_mod.app),
            base_url="http://testserver",
        ) as c:
            resp = await c.options(
                "/agent/chat",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "POST" in resp.headers["access-control-allow-methods"]
        assert "OPTIONS" in resp.headers["access-control-allow-methods"]
        assert "Authorization" in resp.headers["access-control-allow-headers"]
        assert "Content-Type" in resp.headers["access-control-allow-headers"]
        assert "access-control-allow-credentials" not in resp.headers  # 不带凭证
    finally:
        monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
        get_settings.cache_clear()
        importlib.reload(config_mod)  # 还原为默认（无 CORS）装配
        importlib.reload(main_mod)
