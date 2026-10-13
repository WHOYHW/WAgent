"""LLM 适配器单元测试（任务设计 W3 · 验收 AC-IMPL05-1 ~ 16）。

全程 ``FakeOpenAI`` 桩注入（P2 依赖倒置），不触网络；SDK 分片形态用
``SimpleNamespace`` 树伪造——测的是适配器的翻译逻辑而非 pydantic。
真端点兼容性由 ``demo_orchestrator --live`` 联调覆盖（测试分层）。
E2E 用例复用 ``test_orchestrator.make_orch`` 的 ``llm=`` 注入口。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm import ArkLLMClient, LLMNotConfiguredError, LLMStreamError
from app.orchestrator import EventType, LLMChunk
from tests.test_orchestrator import collect, make_orch, token_text, types_of

STUDENT = "20230001"


# ---- FakeOpenAI 桩区（§6.1）----


class FakeAsyncStream:
    """伪造 openai.AsyncStream：items 为分片序列，Exception 项原样抛。"""

    def __init__(self, items):
        self.items = list(items)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.items:
            raise StopAsyncIteration
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeCompletions:
    """scripts：每次 create() 弹出的分片序列（或首抛异常）；记录全部 kwargs。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.scripts.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeAsyncStream(item)


class FakeOpenAI:
    def __init__(self, scripts):
        self.chat = SimpleNamespace(completions=FakeCompletions(scripts))
        self.close_count = 0

    async def close(self):
        self.close_count += 1


# ---- 分片构造器 ----


def ns_chunk(content=None, *, tool_calls=None, finish=None, role=None, reasoning=None):
    """伪 ChatCompletionChunk（单 choice）。reasoning 模拟豆包思考字段。"""
    delta = SimpleNamespace(
        role=role, content=content, tool_calls=tool_calls, reasoning_content=reasoning
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def ns_tc(index, id=None, name=None, arguments=None):
    """伪 ChoiceDeltaToolCall；function=None 模拟 id-only 分片。"""
    fn = (
        None
        if (name is None and arguments is None)
        else SimpleNamespace(name=name, arguments=arguments)
    )
    return SimpleNamespace(index=index, id=id, type="function", function=fn)


def ns_usage_chunk():
    """include_usage 尾块：choices 为空。"""
    return SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=42))


def make_adapter(fake: FakeOpenAI, **cfg) -> ArkLLMClient:
    """注入 FakeOpenAI 的适配器（model 必配：请求参数来自 settings）。"""
    return ArkLLMClient(client=fake, settings=Settings(llm_model="doubao-test", **cfg))


async def drive(adapter, messages=None, tools=None) -> list[LLMChunk]:
    """排空一次 stream()，返回块序列。"""
    return [
        c
        async for c in adapter.stream(
            messages or [{"role": "user", "content": "你好"}], tools
        )
    ]


MSG = [{"role": "user", "content": "你好"}]
TOOLS = [{"type": "function", "function": {"name": "timetable_query", "parameters": {}}}]


# ---- AC-IMPL05-1 纯文本流逐块透传 ----


async def test_text_stream_passthrough():
    fake = FakeOpenAI(
        [[ns_chunk("你"), ns_chunk("好"), ns_chunk("呀", finish="stop")]]
    )
    chunks = await drive(make_adapter(fake), MSG)
    assert [c.delta for c in chunks] == ["你", "好", "呀", None]  # 终块 delta 为 None
    assert "".join(c.delta for c in chunks if c.delta) == "你好呀"
    assert all(c.tool_calls is None for c in chunks)  # 全程无 tool_calls
    assert chunks[-1].finish_reason == "stop"


# ---- AC-IMPL05-2 reasoning_content 剥离 ----


async def test_reasoning_stripped():
    fake = FakeOpenAI(
        [
            [
                ns_chunk("答", reasoning="先思考"),
                ns_chunk(reasoning="继续思考，不产出"),  # 仅 reasoning 的分片
                ns_chunk("案", finish="stop"),
            ]
        ]
    )
    chunks = await drive(make_adapter(fake), MSG)
    assert "".join(c.delta for c in chunks if c.delta) == "答案"  # 思考不透传
    assert chunks[-1].finish_reason == "stop"


# ---- AC-IMPL05-3 空/None 分片跳过 ----


async def test_empty_chunks_skipped():
    fake = FakeOpenAI(
        [[ns_chunk(role="assistant"), ns_chunk(None), ns_chunk("", finish="stop")]]
    )
    chunks = await drive(make_adapter(fake), MSG)
    assert [c.delta for c in chunks] == [None]  # 不产出空 delta 块，仅终块
    assert chunks[0].finish_reason == "stop"  # 终块仍发出（编排层可拿到 final）


# ---- AC-IMPL05-4 单工具调用分片聚合 ----


async def test_single_tool_call_aggregation():
    fake = FakeOpenAI(
        [
            [
                ns_chunk(role="assistant"),
                ns_chunk(tool_calls=[ns_tc(0, id="call_1", name="timetable_query", arguments="")]),
                ns_chunk(tool_calls=[ns_tc(0, arguments='{"start_date": "2026-09-28",')]),
                ns_chunk(tool_calls=[ns_tc(0, arguments='"end_date": "2026-09-30"}')]),
                ns_chunk(finish="tool_calls"),
            ]
        ]
    )
    chunks = await drive(make_adapter(fake), MSG, TOOLS)
    assert len(chunks) == 1  # 无文本，仅一个聚合终块
    final = chunks[0]
    assert final.delta is None
    assert final.finish_reason == "tool_calls"
    assert len(final.tool_calls) == 1
    call = final.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "timetable_query"
    assert (
        call.arguments == '{"start_date": "2026-09-28","end_date": "2026-09-30"}'
    )  # 分片拼接正确


# ---- AC-IMPL05-5 多 index 并发交错聚合 ----


async def test_multi_index_interleaved():
    fake = FakeOpenAI(
        [
            [
                ns_chunk(tool_calls=[ns_tc(0, id="call_a", name="timetable_query", arguments='{"a"')]),
                ns_chunk(tool_calls=[ns_tc(1, id="call_b", name="grade_query", arguments='{"b"')]),
                ns_chunk(tool_calls=[ns_tc(1, arguments=': 2}')]),
                ns_chunk(tool_calls=[ns_tc(0, arguments=': 1}')]),
                ns_chunk(finish="tool_calls"),
            ]
        ]
    )
    chunks = await drive(make_adapter(fake), MSG, TOOLS)
    calls = chunks[-1].tool_calls
    assert [c.id for c in calls] == ["call_a", "call_b"]  # 按 index 升序，不串桶
    assert [c.arguments for c in calls] == ['{"a": 1}', '{"b": 2}']


# ---- AC-IMPL05-6 首分片无 id 的兜底 ----


async def test_missing_and_late_id():
    # id 晚到：第二个分片才出现 → 终块取晚到值
    fake = FakeOpenAI(
        [
            [
                ns_chunk(tool_calls=[ns_tc(0, name="timetable_query", arguments="{}")]),
                ns_chunk(tool_calls=[ns_tc(0, id="call_late")]),
                ns_chunk(finish="tool_calls"),
            ]
        ]
    )
    chunks = await drive(make_adapter(fake), MSG, TOOLS)
    assert chunks[-1].tool_calls[0].id == "call_late"

    # 全程无 id → 合成 call_0（非空，保配对协议）
    fake2 = FakeOpenAI(
        [[ns_chunk(tool_calls=[ns_tc(0, name="grade_query", arguments="{}")], finish="tool_calls")]]
    )
    chunks2 = await drive(make_adapter(fake2), MSG, TOOLS)
    assert chunks2[-1].tool_calls[0].id == "call_0"


# ---- AC-IMPL05-7 arguments 空串归一 ----


async def test_empty_arguments_normalized():
    fake = FakeOpenAI(
        [
            [
                ns_chunk(tool_calls=[ns_tc(0, id="call_1", name="library_loans")]),
                ns_chunk(finish="tool_calls"),
            ]
        ]
    )
    chunks = await drive(make_adapter(fake), MSG, TOOLS)
    assert chunks[-1].tool_calls[0].arguments == "{}"  # 空参归一为合法 JSON


# ---- AC-IMPL05-8 空 choices 块容错 ----


async def test_usage_tail_chunk_tolerated():
    fake = FakeOpenAI(
        [
            [
                ns_chunk("好", finish="stop"),
                ns_usage_chunk(),  # include_usage 尾块：choices 为空
            ]
        ]
    )
    chunks = await drive(make_adapter(fake), MSG)
    assert "".join(c.delta for c in chunks if c.delta) == "好"
    assert chunks[-1].finish_reason == "stop"  # 不提前终止、不影响结果


# ---- AC-IMPL05-9 流中途异常包装 ----


async def test_midstream_exception_wrapped():
    fake = FakeOpenAI([[ns_chunk("首"), RuntimeError("boom")]])  # 第 2 项即失败注入
    chunks = []
    with pytest.raises(LLMStreamError) as ei:
        async for c in make_adapter(fake).stream(MSG):
            chunks.append(c)
    assert chunks and chunks[0].delta == "首"  # 已产出的块不回收
    assert isinstance(ei.value.__cause__, RuntimeError)  # 因果链保留
    assert "RuntimeError" in str(ei.value)  # 消息含原始类名


# ---- AC-IMPL05-10 create 阶段 SDK 异常包装 ----


async def test_create_exception_wrapped():
    fake = FakeOpenAI([[RuntimeError("connect refused")]])  # 首项直接是异常
    chunks = []
    with pytest.raises(LLMStreamError):
        async for c in make_adapter(fake).stream(MSG):
            chunks.append(c)
    assert chunks == []  # 无任何块产出


# ---- AC-IMPL05-11 未配置懒抛 ----


async def test_not_configured_lazy_raise():
    # 全空：无注入 client，首次迭代抛 LLMNotConfiguredError（懒抛）
    adapter = ArkLLMClient(
        settings=Settings(llm_api_base=None, llm_api_key=None, llm_model=None)
    )
    assert adapter.is_configured is False
    with pytest.raises(LLMNotConfiguredError):
        await drive(adapter, MSG)

    # 仅缺 model：注入了 client 也必须抛（model 是请求参数）
    adapter2 = ArkLLMClient(
        client=FakeOpenAI([]),
        settings=Settings(llm_api_base="https://x", llm_api_key="k", llm_model=None),
    )
    assert adapter2.is_configured is False
    with pytest.raises(LLMNotConfiguredError):
        await drive(adapter2, MSG)


# ---- AC-IMPL05-12 请求参数与客户端构造正确性 ----


async def test_request_kwargs():
    fake = FakeOpenAI([[ns_chunk("好", finish="stop")]])
    # 显式 None 压掉本地 .env 的可能配置，测"代码默认"
    adapter = make_adapter(fake, llm_thinking_enabled=None)
    await drive(adapter, MSG, TOOLS)
    kwargs = fake.chat.completions.calls[0]
    assert kwargs["model"] == "doubao-test"
    assert kwargs["stream"] is True
    assert "extra_body" not in kwargs  # 默认不发厂商私有参数（任何端点安全）
    assert kwargs["tools"] == TOOLS
    assert "stream_options" not in kwargs  # include_usage 默认关

    # 显式 False → 发 disabled（保持 v1.0 的方舟关思考语义）
    fake2 = FakeOpenAI([[ns_chunk("好", finish="stop")]])
    await drive(make_adapter(fake2, llm_thinking_enabled=False), MSG)
    assert (
        fake2.chat.completions.calls[0]["extra_body"]
        == {"thinking": {"type": "disabled"}}
    )

    # tools=None → 不带该键（空表对 Ark 行为未验证）
    fake2 = FakeOpenAI([[ns_chunk("好", finish="stop")]])
    await drive(make_adapter(fake2), MSG, None)
    assert "tools" not in fake2.chat.completions.calls[0]


async def test_client_constructor_params(monkeypatch):
    """懒创建路径的 SDK 构造参数（monkeypatch 掉 AsyncOpenAI 记录入参）。"""
    import app.llm.adapter as adapter_mod

    recorded: dict = {}

    class RecorderOpenAI:
        def __init__(self, **kw):
            recorded.update(kw)
            self.chat = SimpleNamespace(
                completions=FakeCompletions([[ns_chunk("好", finish="stop")]])
            )

        async def close(self):
            pass

    monkeypatch.setattr(adapter_mod, "AsyncOpenAI", RecorderOpenAI)
    adapter = ArkLLMClient(
        settings=Settings(
            llm_api_base="https://ark.test/api/v3",
            llm_api_key="k-test",
            llm_model="m-test",
        )
    )
    await drive(adapter, MSG)
    assert recorded["base_url"] == "https://ark.test/api/v3"
    assert recorded["api_key"] == "k-test"
    assert recorded["max_retries"] == 0  # SDK 默认 2 会被显式清零
    assert float(recorded["timeout"].read) == 15.0  # settings 默认超时
    assert float(recorded["timeout"].connect) == 5.0  # connect 单独收紧


# ---- AC-IMPL05-13 aclose 生命周期 ----


async def test_aclose_idempotent():
    fake = FakeOpenAI([])
    adapter = make_adapter(fake)
    await adapter.aclose()
    assert fake.close_count == 1
    assert adapter._client is None
    await adapter.aclose()  # 幂等，二次不抛不再关
    assert fake.close_count == 1


# ---- AC-IMPL05-14 E2E：与 Orchestrator 协议兼容（主干）----


async def test_e2e_with_orchestrator(db):
    fake = FakeOpenAI(
        [
            # 第 1 次 stream：决定调用课表工具（arguments 分片到达）
            [
                ns_chunk(role="assistant"),
                ns_chunk(
                    tool_calls=[
                        ns_tc(0, id="call_1", name="timetable_query", arguments="")
                    ]
                ),
                ns_chunk(tool_calls=[ns_tc(0, arguments='{"start_date": "2026-09-28",')]),
                ns_chunk(tool_calls=[ns_tc(0, arguments='"end_date": "2026-09-30"}')]),
                ns_chunk(finish="tool_calls"),
            ],
            # 第 2 次 stream：基于工具结果流式作答
            [ns_chunk("明天有 "), ns_chunk("1 节课。", finish="stop")],
        ]
    )
    orch, _, store, _, _ = make_orch(
        db, [], llm=make_adapter(fake), llm_model="doubao-test"
    )
    events = await collect(orch.run(STUDENT, "我明天有什么课"))

    # 事件序列与 FakeLLM 直连时完全同形（协议兼容的直接证据）
    assert types_of(events) == [
        EventType.THINKING,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.TOKEN,
        EventType.TOKEN,
        EventType.DONE,
    ]
    done = events[-1]
    assert done.data["tool_iterations"] == 1
    assert token_text(events) == "明天有 1 节课。"
    assert events[1].data["tool"] == "timetable_query"

    # 落库消息配对完整：user / assistant(tool_calls) / tool / assistant
    history = await store.load_history(done.data["session_id"])
    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]

    # 第 2 次 LLM 请求的上下文包含工具回填消息（配对协议未断裂）
    second_msgs = fake.chat.completions.calls[1]["messages"]
    assert any(m.get("role") == "tool" for m in second_msgs)


# ---- AC-IMPL05-15 thinking / usage 开关 ----


async def test_thinking_and_usage_switches():
    fake = FakeOpenAI(
        [[ns_chunk("好", finish="stop"), ns_usage_chunk()]]
    )
    adapter = make_adapter(
        fake, llm_thinking_enabled=True, llm_stream_include_usage=True
    )
    chunks = await drive(adapter, MSG)
    kwargs = fake.chat.completions.calls[0]
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kwargs["stream_options"] == {"include_usage": True}
    assert chunks[-1].finish_reason == "stop"  # usage 尾块被容忍


# ---- AC-IMPL05-16 E2E：首请求失败映射 llm_unavailable ----


async def test_e2e_first_request_failure(db):
    fake = FakeOpenAI([[RuntimeError("ark down")]])
    orch, _, _, _, _ = make_orch(
        db, [], llm=make_adapter(fake), llm_model="doubao-test"
    )
    events = await collect(orch.run(STUDENT, "我明天有什么课"))
    errors = [ev for ev in events if ev.type == EventType.ERROR]
    assert len(errors) == 1
    assert errors[0].data["code"] == "llm_unavailable"  # 适配器异常贯通 F6 路径
    assert not any(ev.type == EventType.DONE for ev in events)  # 无 done 终态
