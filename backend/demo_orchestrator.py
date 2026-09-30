"""编排核心 Demo（任务设计 W2 学习配套）。

用 FakeLLM 桩驱动一次真实链路：内存 SQLite + 真 ToolRegistry + 真 SessionStore，
观察六类事件流的产出顺序。运行（backend 目录下）：

    python -m demo_orchestrator
"""

from __future__ import annotations

import asyncio
import json

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


def _print_events(events: list[Event]) -> None:
    for ev in events:
        if ev.type == EventType.TOKEN:
            print(f"  token      | {ev.data['delta']}", end="")
        else:
            detail = json.dumps(ev.data, ensure_ascii=False, default=str)
            print(f"\n  {ev.type.value:11s}| {detail}")
    print()


async def main() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        settings = Settings()
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

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
