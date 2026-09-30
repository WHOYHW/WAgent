"""编排核心单元测试（任务设计 W2.6 · 验收 AC-IMPL03-1 ~ 15）。

三分支主干（任务卡必测）：T1 压缩触发 / T2 迭代上限熔断 / T3 红线拦截；
附加场景按 §6.2 AC 表逐条编号。LLM 一律 ``FakeLLM`` 桩（P2 依赖倒置），
不触网络；工具走真 ``build_registry`` + conftest 桩适配器。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import Settings
from app.conversation import SessionStore
from app.orm import AuditLog, HandoffTicket, Message
from app.orchestrator import (
    Event,
    EventType,
    LLMChunk,
    Orchestrator,
    ToolCallRequest,
)
from app.security import mask_student_id
from app.schemas.conversation import MessageIn
from app.tools import build_registry
from tests.conftest import make_services

STUDENT = "20230001"


# ---- FakeLLM（§6.1 脚本化桩）----


class FakeLLM:
    """script 是「每次 stream() 弹出的 chunk 序列」的列表；
    记录每次收到的 messages / tools 供断言。"""

    def __init__(self, script: list[list[LLMChunk]]) -> None:
        """script：每次 stream() 弹出的 chunk 序列组成的列表。"""
        self.script = list(script)
        self.calls: list[dict] = []

    async def stream(self, messages, tools=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        for chunk in self.script.pop(0):
            if isinstance(chunk, Exception):
                raise chunk  # 脚本化的失败注入（T1c 等）
            yield chunk


# ---- 夹具与辅助 ----


def make_orch(db, script, *, edu=None, lib=None, llm=None, **cfg):
    settings = Settings(**cfg) if cfg else Settings()
    store = SessionStore(db, settings)
    services = make_services(db, edu=edu, lib=lib)
    registry = build_registry(services)
    client = llm or FakeLLM(script)
    orch = Orchestrator(store, registry, client, settings=settings)
    return orch, client, store, registry, services


async def collect(agen) -> list[Event]:
    return [ev async for ev in agen]


def types_of(events: list[Event]) -> list[EventType]:
    return [ev.type for ev in events]


def find_event(events: list[Event], t: EventType) -> Event:
    return next(ev for ev in events if ev.type == t)


def token_text(events: list[Event]) -> str:
    return "".join(ev.data["delta"] for ev in events if ev.type == EventType.TOKEN)


async def audit_rows(db, action: str) -> list[AuditLog]:
    return list(
        (await db.execute(select(AuditLog).where(AuditLog.action == action)))
        .scalars()
        .all()
    )


async def count_messages(db, session_id: str) -> int:
    rows = (await db.execute(select(Message).where(Message.session_id == session_id))).scalars().all()
    return len(rows)


def text_script(text: str) -> list[LLMChunk]:
    """单次 stream() 的纯文本 chunk 序列。"""
    return [LLMChunk(delta=text, finish_reason="stop")]


def tool_call_script(name: str, args: dict, call_id: str = "call_1") -> list[LLMChunk]:
    """单次 stream() 的工具调用 chunk 序列（末块聚合 tool_calls）。"""
    return [
        LLMChunk(
            tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=json.dumps(args))],
            finish_reason="tool_calls",
        )
    ]


TIMETABLE_ARGS = {
    "student_id": STUDENT,  # 模型"伪造"身份（T7 断言被剥离）
    "start_date": "2026-09-28",
    "end_date": "2026-09-30",
}


# ---- T4 / AC-IMPL03-10 正常路径：1 次工具调用的事件序列 ----


async def test_normal_one_tool_call_event_sequence(db):
    orch, llm, store, _, _ = make_orch(db, [
        tool_call_script("timetable_query", TIMETABLE_ARGS),
        text_script("明天有 2 节课：高等数学与大学物理。"),
    ])
    events = await collect(orch.run(STUDENT, "我明天有什么课"))

    assert types_of(events) == [
        EventType.THINKING, EventType.TOOL_CALL, EventType.TOOL_RESULT,
        EventType.TOKEN, EventType.DONE,
    ]
    assert events[0].data["phase"] == "session_ready"
    assert events[1].data["tool"] == "timetable_query"
    assert events[1].data["call_id"] == "call_1"
    assert events[2].data["ok"] is True
    assert events[2].data["meta"]["source"] == "edu_system"
    assert token_text(events) == "明天有 2 节课：高等数学与大学物理。"

    done = events[-1]
    assert done.type == EventType.DONE
    assert done.data["tool_iterations"] == 1
    assert done.data["budget_exhausted"] is False
    assert done.data["blocked"] is False
    assert done.data["compressed"] is False
    assert done.data["session_id"]
    assert done.data["assistant_message_id"]

    # LLM 协议：首轮带 tools，次轮收到配对的 assistant(tool_calls) + tool 消息
    assert llm.calls[0]["tools"] is not None
    assert llm.calls[0]["messages"][0]["role"] == "system"
    assert llm.calls[0]["messages"][-1]["role"] == "user"
    second = llm.calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_1"

    # 中间消息即时落库（assistant + tool）+ 最终 assistant 消息
    sid = done.data["session_id"]
    msgs = await store.load_history(sid, limit=10)
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant"]


# ---- T3a / AC-IMPL03-6 红线拦截：LLM 零调用 ----


async def test_redline_r1_blocks_before_llm(db):
    orch, llm, store, _, _ = make_orch(db, [])
    events = await collect(orch.run(STUDENT, "我的毕业资格够吗"))

    assert llm.calls == []  # LLM 从未被调用
    assert types_of(events)[0] == EventType.THINKING
    assert types_of(events)[1] == EventType.TOOL_CALL
    assert events[1].data["tool"] == "handoff_create"

    ticket = (await db.execute(select(HandoffTicket))).scalar_one()
    assert ticket.status == "open"
    assert "R1_终局性结论" in ticket.hit_rules
    assert ticket.priority == "P2"

    done = find_event(events, EventType.DONE)
    assert done.data["blocked"] is True
    assert done.data["handoff_ticket_id"] == ticket.id
    assert token_text(events)  # 拦截话术以 token 事件流给用户

    assert await audit_rows(db, "redline_block")
    # 被拦截的对话仍是完整会话：user + 拦截话术已落库
    msgs = await store.load_history(ticket.session_id, limit=10)
    assert [m.role for m in msgs] == ["user", "assistant"]


# ---- T3b / AC-IMPL03-7 R5 生命优先通道 ----


async def test_redline_r5_lifeline_channel(db):
    orch, llm, _, _, _ = make_orch(db, [])
    events = await collect(orch.run(STUDENT, "我最近很难受，不想活了"))

    assert llm.calls == []
    ticket = (await db.execute(select(HandoffTicket))).scalar_one()
    assert ticket.priority == "P0"
    assert "心理" in (ticket.suggested_owner or "")
    text = token_text(events)
    assert "热线" in text  # 话术包含心理援助资源
    done = find_event(events, EventType.DONE)
    assert done.data["blocked"] is True


# ---- T6 / AC-IMPL03-11 会话过期自动重建 ----


async def test_expired_session_rebuilds(db):
    orch, llm, store, _, _ = make_orch(db, [text_script("好的，已为你新建会话。")])
    sess = await store.create(student_id=STUDENT)
    # 把 expires_at 设到过去（相对真实时钟）
    from app.orm import ConversationSession

    row = (await db.execute(
        select(ConversationSession).where(
            ConversationSession.session_id == sess.session_id)
    )).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db.commit()

    events = await collect(orch.run(STUDENT, "你好", session_id=sess.session_id))
    done = find_event(events, EventType.DONE)
    assert done.data["session_id"] != sess.session_id  # 新会话


# ---- T1 / AC-IMPL03-1 压缩触发 ----


async def seed_messages(store, sid: str, n: int) -> None:
    """预插 n 条历史（user/assistant 交替）。"""
    for i in range(n):
        await store.append(
            sid,
            MessageIn(role="user" if i % 2 == 0 else "assistant", content=f"m{i}"),
        )


async def test_compression_triggers_and_rebuilds_context(db):
    """T1：预插 25 条 + 本轮 user = 26 条活跃 > 20 → 摘要 → compress → 上下文重组。"""
    summary_text = "学生此前询问了课表、教室与图书馆续借等校园事务。"
    orch, llm, store, _, _ = make_orch(db, [
        [LLMChunk(delta=summary_text, finish_reason="stop")],   # 摘要请求
        text_script("基于摘要与最近历史作答。"),                    # 对话请求
    ])
    sess = await store.create(student_id=STUDENT)
    await seed_messages(store, sess.session_id, 25)

    events = await collect(orch.run(STUDENT, "本轮问题", session_id=sess.session_id))

    # ① 摘要请求：不带 tools，覆盖 probe[:-6]（20 条被压缩消息）
    assert llm.calls[0]["tools"] is None
    summary_msgs = llm.calls[0]["messages"]
    assert summary_msgs[0]["role"] == "system"  # 摘要 prompt
    assert len(summary_msgs) - 1 == 20
    assert summary_msgs[1]["content"] == "m0"
    assert summary_msgs[-1]["content"] == "m19"

    # ② 会话出现 is_summary=True 的 system 行；③ 软删 20 条
    from sqlalchemy import func

    summary_rows = (await db.execute(
        select(Message).where(
            Message.session_id == sess.session_id, Message.is_summary.is_(True)
        )
    )).scalars().all()
    assert len(summary_rows) == 1
    assert summary_rows[0].content == summary_text
    soft_deleted = (await db.execute(
        select(func.count()).select_from(Message).where(
            Message.session_id == sess.session_id, Message.deleted_at.is_not(None)
        )
    )).scalar_one()
    assert soft_deleted == 20  # 26 活跃 − 6 保留

    # ④ 首次对话请求：[system prompt, 摘要, …尾部 6 条（含最新 user）]
    conv_msgs = llm.calls[1]["messages"]
    assert conv_msgs[0]["role"] == "system" and "校园助手" in conv_msgs[0]["content"]
    assert conv_msgs[1] == {"role": "system", "content": summary_text}
    assert len(conv_msgs) == 8  # prompt + 摘要 + 6 条尾部
    assert conv_msgs[-1]["role"] == "user" and conv_msgs[-1]["content"] == "本轮问题"

    # ⑤ done.compressed=True；压缩审计
    done = find_event(events, EventType.DONE)
    assert done.data["compressed"] is True
    assert await audit_rows(db, "session_compress")
    assert any(
        ev.type == EventType.THINKING and ev.data.get("phase") == "compressing"
        for ev in events
    )


# ---- T1b / AC-IMPL03-2 摘要 ≤ 200 字 ----


async def test_summary_truncated_to_max_chars(db):
    orch, llm, store, _, _ = make_orch(db, [
        [LLMChunk(delta="长" * 500, finish_reason="stop")],
        text_script("好的。"),
    ])
    sess = await store.create(student_id=STUDENT)
    await seed_messages(store, sess.session_id, 25)
    events = await collect(orch.run(STUDENT, "本轮问题", session_id=sess.session_id))

    assert find_event(events, EventType.DONE).data["compressed"] is True
    summary_row = (await db.execute(
        select(Message).where(
            Message.session_id == sess.session_id, Message.is_summary.is_(True)
        )
    )).scalar_one()
    assert len(summary_row.content) == 200  # 硬截断


# ---- T1c / AC-IMPL03-3 压缩失败降级不阻断，上下文含最新消息 ----


async def test_summary_failure_degrades_gracefully(db):
    orch, llm, store, _, _ = make_orch(db, [
        [RuntimeError("llm down")],          # 摘要调用抛异常
        text_script("降级后正常作答。"),
    ])
    sess = await store.create(student_id=STUDENT)
    await seed_messages(store, sess.session_id, 25)
    events = await collect(orch.run(STUDENT, "本轮问题", session_id=sess.session_id))

    # 对话照常完成（done 而非 error），compressed=False
    done = find_event(events, EventType.DONE)
    assert done.data["compressed"] is False
    assert find_event(events, EventType.DONE).data["blocked"] is False

    # 降级上下文 = probe：LLM 收到的最后一条是本轮 user 消息
    conv_msgs = llm.calls[1]["messages"]
    assert conv_msgs[-1]["role"] == "user" and conv_msgs[-1]["content"] == "本轮问题"
    # 未产生 is_summary 行
    rows = (await db.execute(
        select(Message).where(
            Message.session_id == sess.session_id, Message.is_summary.is_(True)
        )
    )).scalars().all()
    assert rows == []


# ---- T1d / AC-IMPL03-3b 触发边界：恰好 20 条不触发 ----


async def test_boundary_exactly_20_active_no_compression(db):
    orch, llm, store, _, _ = make_orch(db, [text_script("恰好边界。")])
    sess = await store.create(student_id=STUDENT)
    await seed_messages(store, sess.session_id, 19)  # + 本轮 user = 20

    events = await collect(orch.run(STUDENT, "本轮问题", session_id=sess.session_id))

    assert len(llm.calls) == 1                       # 无摘要调用
    assert llm.calls[0]["tools"] is not None         # 直接进对话
    assert llm.calls[0]["messages"][-1]["content"] == "本轮问题"
    done = find_event(events, EventType.DONE)
    assert done.data["compressed"] is False
    rows = (await db.execute(
        select(Message).where(
            Message.session_id == sess.session_id, Message.is_summary.is_(True)
        )
    )).scalars().all()
    assert rows == []


# ---- T2 / AC-IMPL03-4 迭代上限熔断（拒绝即 fused）----


class LooperLLM:
    """tools 非 None 时永远返回一批 tool_calls（每次新 id）；
    tools is None（收尾请求）时返回固定文本。"""

    def __init__(self, batch_size: int = 3) -> None:
        self.batch_size = batch_size
        self.calls: list[dict] = []
        self._n = 0

    async def stream(self, messages, tools=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        self._n += 1
        if tools is None:
            yield LLMChunk(delta="已尽力查询，以下是基于已有结果的汇总。", finish_reason="stop")
            return
        batch = [
            ToolCallRequest(
                id=f"call_{self._n}_{i}",
                name="timetable_query",
                arguments=json.dumps(TIMETABLE_ARGS),
            )
            for i in range(self.batch_size)
        ]
        yield LLMChunk(tool_calls=batch, finish_reason="tool_calls")


async def test_budget_fuse_rejects_and_terminates(db):
    """T2：预算 6；每批 3 → 两批执行后第三批被拒，拒绝即熔断，收尾不带 tools。"""
    llm = LooperLLM(batch_size=3)
    store = SessionStore(db, Settings())
    registry = build_registry(make_services(db))
    orch = Orchestrator(store, registry, llm, settings=store.settings)

    events = await collect(orch.run(STUDENT, "帮我查这学期全部安排"))

    # ① dispatch 审计行 = 6（用审计计数：课表工具命中缓存时桩适配器计数不可靠）
    tool_audits = await audit_rows(db, "tool_call")
    assert len(tool_audits) == 6
    # ② 事件流 tool_call 事件 = 6（被拒批不产生事件——调用从未执行）
    assert sum(1 for ev in events if ev.type == EventType.TOOL_CALL) == 6
    # ③ 收尾请求 tools is None；⑥ LLM 调用次数有界 = 4（2 执行 + 1 拒绝 + 1 收尾）
    assert llm.calls[-1]["tools"] is None
    assert len(llm.calls) == 4
    # ④ 有 token 事件且 done 收尾（非裸 error）
    assert any(ev.type == EventType.TOKEN for ev in events)
    assert events[-1].type == EventType.DONE
    assert not any(ev.type == EventType.ERROR for ev in events)
    # ⑤ done 统计字段
    done = events[-1]
    assert done.data["tool_iterations"] == 6
    assert done.data["budget_exhausted"] is True
    # 熔断审计留痕
    assert await audit_rows(db, "budget_exhausted")
    # thinking(budget_exhausted) 阶段信号
    assert any(
        ev.type == EventType.THINKING and ev.data.get("phase") == "budget_exhausted"
        for ev in events
    )


async def test_rejected_batch_pairing_and_no_tool_retry(db):
    """T2b / AC-IMPL03-5：被拒批每个 tool_call 有配对"未执行"消息；拒绝后请求不带 tools。"""
    llm = LooperLLM(batch_size=3)
    store = SessionStore(db, Settings())
    registry = build_registry(make_services(db))
    orch = Orchestrator(store, registry, llm, settings=store.settings)

    events = await collect(orch.run(STUDENT, "查安排"))
    sid = events[-1].data["session_id"]

    # 拒绝轮（第 3 次）带 tools，其后（第 4 次收尾）tools is None
    assert llm.calls[2]["tools"] is not None
    assert llm.calls[3]["tools"] is None

    # 落库消息：最后一个 assistant(tool_calls) 的每个 call id 都有配对"未执行" tool 消息
    msgs = await store.load_history(sid, limit=60)
    last_asst = next(m for m in reversed(msgs) if m.role == "assistant" and m.tool_calls)
    call_ids = [tc["id"] for tc in last_asst.tool_calls]
    tool_msgs = [m for m in msgs if m.role == "tool" and m.tool_call_id in call_ids]
    assert len(tool_msgs) == 3
    assert all(m.content.startswith("未执行") for m in tool_msgs)
    # 协议配对完整：每个 id 恰好一条
    assert sorted(m.tool_call_id for m in tool_msgs) == sorted(call_ids)


# ---- T5 / AC-IMPL03-9 工具失败不中断（失败回填转述）----


async def test_tool_failure_feeds_back_to_llm(db):
    from tests.conftest import StubEduAdapter

    edu = StubEduAdapter()
    edu.fail = TimeoutError("edu timeout")
    orch, llm, store, _, _ = make_orch(
        db,
        [
            tool_call_script("timetable_query", TIMETABLE_ARGS),
            text_script("抱歉，课表服务暂时不可用，请稍后再试。"),
        ],
        edu=edu,
    )
    events = await collect(orch.run(STUDENT, "我明天有什么课"))

    tool_result = find_event(events, EventType.TOOL_RESULT)
    assert tool_result.data["ok"] is False
    assert tool_result.data["error"]

    # LLM 收到失败 tool 消息并产出含"暂时不可用"语义的回答
    second = llm.calls[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert "调用失败" in second[-1]["content"]
    assert "暂时不可用" in token_text(events)
    assert events[-1].type == EventType.DONE  # 对话未中断


# ---- T7 / AC-IMPL03-8 student_id 防越权（模型伪造被剥离）----


async def test_forged_student_id_stripped(db):
    forged = dict(TIMETABLE_ARGS, student_id="99999999")
    orch, llm, _, _, _ = make_orch(db, [
        tool_call_script("timetable_query", forged),
        text_script("已查询。"),
    ])
    events = await collect(orch.run(STUDENT, "我明天有什么课"))

    # dispatch 实际入参为 ctx 身份（审计 args_digest 佐证：真实学号脱敏值）
    rows = await audit_rows(db, "tool_call")
    assert rows and rows[0].args_digest["student_id"] == mask_student_id(STUDENT)
    assert rows[0].args_digest["student_id"] != mask_student_id("99999999")
    assert events[-1].type == EventType.DONE


# ---- T8 / AC-IMPL03-14 写门禁：confirmed / idempotency_key 服务端可信化 ----


RENEW_ARGS = {
    "student_id": STUDENT,            # 模型伪造字段（一律被剥离）
    "barcodes": ["BAR001"],
    "confirmed": True,
    "idempotency_key": "forged-key",
}


async def renew_turn(db, user_input: str, sid: str | None, barcodes, **cfg):
    from tests.conftest import StubLibAdapter

    lib = StubLibAdapter()
    orch, llm, store, _, _ = make_orch(
        db,
        [
            tool_call_script(
                "library_renew",
                dict(RENEW_ARGS, barcodes=barcodes),
            ),
            text_script("好的，已处理续借。"),
        ],
        lib=lib,
        **cfg,
    )
    events = await collect(orch.run(STUDENT, user_input, session_id=sid))
    return orch, llm, store, lib, events


async def test_write_gate_forged_confirmed_stripped(db):
    """T8a：LLM 自填 confirmed=true → 剥离为 False，工具不执行，回填 PreconditionError。"""
    orch, llm, store, lib, events = await renew_turn(db, "帮我把这本书续借", None, ["BAR001"])

    assert lib.renew_calls == 0
    tool_audits = [r for r in await audit_rows(db, "tool_call") if r.tool_name == "library_renew"]
    assert tool_audits[0].args_digest["confirmed"] is False  # 服务端注入值
    # LLM 收到 PreconditionError 回填（两轮确认协议的预览轮）
    second = llm.calls[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert "需先展示新到期日" in second[-1]["content"]
    assert events[-1].type == EventType.DONE


async def test_write_gate_confirm_round_trips(db):
    """T8b：预览轮落库后，用户回复"确认续借" + 相同条码 → confirmed=True 放行。"""
    from app.security import make_idempotency_key

    # 预览轮（confirmed 被门禁置 False → PreconditionError 回填 → 落库）
    _, llm1, store, _, events1 = await renew_turn(db, "帮我把这本书续借", None, ["BAR001"])
    sid = events1[-1].data["session_id"]
    assert events1[-1].type == EventType.DONE

    # 执行轮：用户显式确认 + 相同 barcodes
    _, llm2, _, lib2, events2 = await renew_turn(db, "确认续借", sid, ["BAR001"])

    assert lib2.renew_calls == 1
    tool_audits = [r for r in await audit_rows(db, "tool_call") if r.tool_name == "library_renew"]
    executed = [r for r in tool_audits if r.args_digest.get("confirmed") is True]
    assert executed, "执行轮应有 confirmed=True 的审计行"
    assert lib2.renew_args[0] == STUDENT
    assert lib2.renew_args[1] == ["BAR001"]
    assert lib2.renew_args[2] == make_idempotency_key(
        sid, ["BAR001"], date.today().isoformat()
    )
    assert events2[-1].type == EventType.DONE


async def test_write_gate_rejects_without_confirmation(db):
    """T8c：用户未确认（"算了不用了"）→ confirmed=False，不执行。"""
    _, llm1, store, _, events1 = await renew_turn(db, "帮我把这本书续借", None, ["BAR001"])
    sid = events1[-1].data["session_id"]
    _, _, _, lib2, events2 = await renew_turn(db, "算了不用了", sid, ["BAR001"])

    assert lib2.renew_calls == 0
    tool_audits = [r for r in await audit_rows(db, "tool_call") if r.tool_name == "library_renew"]
    assert all(r.args_digest.get("confirmed") is False for r in tool_audits)
    assert events2[-1].type == EventType.DONE


async def test_write_gate_rejects_barcode_swap(db):
    """T8d：执行轮 LLM 偷换条码 → 与预览轮不一致 → confirmed=False，不执行。"""
    _, llm1, store, _, events1 = await renew_turn(db, "帮我把这本书续借", None, ["BAR001"])
    sid = events1[-1].data["session_id"]
    _, _, _, lib2, events2 = await renew_turn(db, "确认续借", sid, ["BAR999"])

    assert lib2.renew_calls == 0
    tool_audits = [r for r in await audit_rows(db, "tool_call") if r.tool_name == "library_renew"]
    assert all(r.args_digest.get("confirmed") is False for r in tool_audits)
    assert events2[-1].type == EventType.DONE


# ---- T9 / AC-IMPL03-13 会话所有者校验：他人 session_id 不可读写 ----


async def test_owner_mismatch_silently_rebuilds(db):
    # 学生 A 跑一轮，得到 sid
    orch_a, llm_a, store, _, _ = make_orch(db, [text_script("A 的回答。")])
    events_a = await collect(orch_a.run(STUDENT, "A 的问题"))
    sid = find_event(events_a, EventType.DONE).data["session_id"]
    a_messages_before = await count_messages(db, sid)

    # 学生 B 携带 A 的 sid 再调 run()
    orch_b, llm_b, _, _, _ = make_orch(db, [text_script("B 的回答。")])
    events_b = await collect(orch_b.run("20230099", "B 的问题", session_id=sid))
    done = find_event(events_b, EventType.DONE)
    assert done.data["session_id"] != sid  # 静默新建

    # B 的 LLM 上下文不含 A 的任何历史（仅 system + B 的 user）
    assert [m["role"] for m in llm_b.calls[0]["messages"]] == ["system", "user"]
    assert llm_b.calls[0]["messages"][-1]["content"] == "B 的问题"

    # 越权尝试已留痕；A 会话消息表无新增
    assert await audit_rows(db, "owner_mismatch")
    assert await count_messages(db, sid) == a_messages_before


# ---- 终评修复（Final review：C1 / I1 / I2 / I3 / I4）----


async def test_mixed_redline_r5_takes_precedence(db):
    """C1 / 混合命中（R1+R5）：工单按 R5 最高优先级路由，话术走生命通道。

    guardrail 按字典序 R1→R3→R4→R5 收集命中，``rules[0]`` 恒为低危规则；
    生命优先时路由必须取 R5，否则危机工单被降级为 P2/教务办。
    """
    orch, llm, _, _, _ = make_orch(db, [])
    events = await collect(orch.run(STUDENT, "我挂科了，不想活了"))

    ticket = (await db.execute(select(HandoffTicket))).scalar_one()
    assert set(ticket.hit_rules) == {"R1_终局性结论", "R5_生命优先"}  # 命中全量留痕
    assert ticket.priority == "P0"  # R5 路由，非 rules[0]=R1 的 P2
    assert "心理" in (ticket.suggested_owner or "")
    assert "热线" in token_text(events)  # 话术走生命通道


async def test_handoff_create_not_exposed_to_llm(db):
    """I2 / handoff_create 由编排层确定性路径调用（F2），不进 LLM 工具表。"""
    orch, llm, _, _, _ = make_orch(db, [text_script("好的。")])
    await collect(orch.run(STUDENT, "你好"))

    names = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert "handoff_create" not in names
    assert "timetable_query" in names  # 其余工具正常暴露


async def test_redline_persists_when_handoff_fails(db, monkeypatch):
    """I1 / 落库先于工单：工单创建失败不阻断话术输出，对话仍完整落库。"""
    orch, llm, store, registry, _ = make_orch(db, [])

    async def boom(*args, **kwargs):
        raise RuntimeError("handoff db down")

    monkeypatch.setattr(registry, "dispatch", boom)
    events = await collect(orch.run(STUDENT, "我的毕业资格够吗"))

    done = events[-1]
    assert done.type == EventType.DONE  # 不因工单失败变成 error 终态
    assert done.data["blocked"] is True
    assert done.data.get("handoff_ticket_id") is None  # 无工单回传
    assert token_text(events)  # 拦截话术仍送达用户
    msgs = await store.load_history(done.data["session_id"], limit=10)
    assert [m.role for m in msgs] == ["user", "assistant"]


async def test_llm_first_request_failure_emits_llm_unavailable(db):
    """I3a / F6：主循环首请求失败 → error(llm_unavailable) 终态，无 done。"""
    orch, llm, store, _, _ = make_orch(db, [[RuntimeError("llm down")]])
    events = await collect(orch.run(STUDENT, "你好"))

    assert events[-1].type == EventType.ERROR
    assert events[-1].data["code"] == "llm_unavailable"
    assert not any(ev.type == EventType.DONE for ev in events)


async def test_bad_json_arguments_fed_back(db):
    """I3b+I4 / 参数 JSON 解析失败：配对回填 + 事件带 args_digest，循环不中断。"""
    orch, llm, store, _, _ = make_orch(db, [
        [LLMChunk(
            tool_calls=[ToolCallRequest(
                id="call_bad", name="timetable_query", arguments="不是JSON",
            )],
            finish_reason="tool_calls",
        )],
        text_script("抱歉，刚才的查询参数有误，请再描述一次。"),
    ])
    events = await collect(orch.run(STUDENT, "我明天有什么课"))

    call_ev = find_event(events, EventType.TOOL_CALL)
    assert call_ev.data["args_digest"]  # 失败路径同样携带脱敏摘要
    result_ev = find_event(events, EventType.TOOL_RESULT)
    assert result_ev.data["ok"] is False
    second = llm.calls[1]["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_bad"
    assert "解析失败" in second[-1]["content"]
    done = events[-1]
    assert done.type == EventType.DONE
    assert done.data["tool_iterations"] == 1


async def test_unknown_tool_fed_back(db):
    """I4 / 未知工具：ToolError 回填 + 配对消息 + args_digest，对话不中断。"""
    orch, llm, store, _, _ = make_orch(db, [
        tool_call_script("nonexistent_tool", {"foo": "bar"}),
        text_script("抱歉，该功能暂不支持。"),
    ])
    events = await collect(orch.run(STUDENT, "帮我查一个东西"))

    result_ev = find_event(events, EventType.TOOL_RESULT)
    assert result_ev.data["ok"] is False
    assert "未知工具" in result_ev.data["error"]
    second = llm.calls[1]["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_1"
    assert "未知工具" in second[-1]["content"]
    assert find_event(events, EventType.TOOL_CALL).data["args_digest"]
    assert events[-1].type == EventType.DONE
