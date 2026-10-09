"""编排核心 Demo（任务设计 W2 学习配套 · 子任务 3 增补 --live 联调开关）。

用 FakeLLM 桩驱动一次真实链路：内存 SQLite + 真 ToolRegistry + 真 SessionStore，
观察六类事件流的产出顺序。运行（backend 目录下）：

    python -m demo_orchestrator           # 离线模式（默认，FakeLLM 桩，不触网）
    python -m demo_orchestrator --live    # 联调模式：真 ArkLLMClient（工具仍为桩）

live 模式消费 backend/.env 的 LLM_API_BASE / LLM_API_KEY / LLM_MODEL，
打印首字延迟（TTFT）——方舟 SSE 兼容性与思考模型延迟的唯一真验证手段
（测试分层：单测 FakeOpenAI 协议形状 / 联调真端点）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.conversation import SessionStore
from app.db import make_engine
from app.orm import Base
from app.orchestrator import Event, EventType, LLMChunk, Orchestrator, ToolCallRequest
from app.tools import build_registry
from tests.conftest import make_services


class FakeLLM:
    """脚本化 LLM 桩：按顺序弹出每次 stream() 的 chunk 序列。"""

    def __init__(self, script: list[list[LLMChunk]]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def stream(self, messages, tools=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        for chunk in self.script.pop(0):
            yield chunk


def _print_event(ev: Event) -> None:
    if ev.type == EventType.TOKEN:
        print(f"  token      | {ev.data['delta']}", end="")
    else:
        detail = json.dumps(ev.data, ensure_ascii=False, default=str)
        print(f"\n  {ev.type.value:11s}| {detail}")


def _print_events(events: list[Event]) -> None:
    for ev in events:
        _print_event(ev)
    print()


async def main() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        settings = Settings()
        if "--live" in sys.argv:
            await _run_live(db, settings)
        else:
            await _run_offline(db, settings)

    await engine.dispose()


async def _run_offline(db, settings: Settings) -> None:
    store = SessionStore(db, settings)
    registry = build_registry(make_services(db))
    llm = FakeLLM([
        # 第 1 次 stream：决定调用课表工具
        [LLMChunk(tool_calls=[ToolCallRequest(
            id="call_1", name="timetable_query",
            arguments=json.dumps({
                "student_id": "99999999",  # 模型伪造身份（会被剥离）
                "start_date": "2026-09-28", "end_date": "2026-09-30",
            }),
        )], finish_reason="tool_calls")],
        # 第 2 次 stream：基于工具结果流式作答
        [LLMChunk(delta="明天有 1 节课：", finish_reason=None),
         LLMChunk(delta="高等数学 08:00 一教-101。", finish_reason="stop")],
    ])
    orch = Orchestrator(store, registry, llm, settings=settings)

    print("=== 场景 1：正常对话（1 次工具调用）===")
    events = [ev async for ev in orch.run("20230001", "我明天有什么课")]
    _print_events(events)
    done = events[-1]
    print(f"  会话：{done.data['session_id']}，工具调用 {done.data['tool_iterations']} 次\n")

    print("=== 场景 2：红线拦截（LLM 零参与）===")
    llm2 = FakeLLM([])
    orch2 = Orchestrator(store, registry, llm2, settings=settings)
    events2 = [ev async for ev in orch2.run("20230001", "我的毕业资格够吗")]
    _print_events(events2)
    print(f"  LLM 是否被调用：{len(llm2.calls) == 0}")


async def _run_live(db, settings: Settings) -> None:
    """联调模式：真 ArkLLMClient 驱动场景 1（工具仍为桩适配器）。"""
    from app.llm import ArkLLMClient

    llm = ArkLLMClient(settings=settings)
    if not llm.is_configured:
        print("LLM 未配置（backend/.env 缺 LLM_API_BASE / LLM_API_KEY / LLM_MODEL），退出。")
        return

    store = SessionStore(db, settings)
    registry = build_registry(make_services(db))
    orch = Orchestrator(store, registry, llm, settings=settings)

    print("=== 场景 1（live）：真 LLM 驱动（工具为桩，事件实时打印）===")
    t0 = time.perf_counter()
    ttft: float | None = None
    done: Event | None = None
    try:
        async for ev in orch.run("20230001", "我明天有什么课"):
            if ev.type == EventType.TOKEN and ttft is None:
                ttft = time.perf_counter() - t0
            if ev.type == EventType.DONE:
                done = ev
            _print_event(ev)
        print()
        if done is not None:
            print(
                f"  会话：{done.data['session_id']}，"
                f"工具调用 {done.data['tool_iterations']} 次"
            )
        if ttft is not None:
            print(f"  TTFT：{ttft:.2f}s，总耗时 {time.perf_counter() - t0:.2f}s")
    finally:
        await llm.aclose()  # 释放 SDK 连接池


if __name__ == "__main__":
    asyncio.run(main())
