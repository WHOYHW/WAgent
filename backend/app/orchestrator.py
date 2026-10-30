"""编排核心（M2）：对话循环 = 会话准备 → 红线门 → 压缩 → Agent Loop → 收尾。

事件流协议与 ``LLMClient`` Protocol 见设计文档
``docs/项目文档/03-编排核心Orchestrator设计.md``（v1.1）§2.3/§2.4——
两个协议已冻结，子任务 3（LLM 适配器）与子任务 4（SSE 端点）据此并行开发。

设计原则（设计文档 §2.1）：
- P1 单一入口：``run()`` 是编排层唯一合法入口，所有对话过同一套
  红线/预算/审计/所有者校验；
- P2 依赖倒置：LLM 以 Protocol 声明，单测注入 ``FakeLLM``，不碰网络；
- P4 确定性优先：红线、熔断、压缩触发、写确认放行全是确定性代码路径；
- P6 参数不可信：LLM 生成的工具参数中的身份/授权/幂等字段一律剥离，
  值由服务端注入。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Protocol

from .config import Settings
from .conversation import SessionExpired, SessionNotFound, SessionStore
from .guardrail import GuardrailResult, check_redlines
from .logging_util import get_logger, get_trace_id, set_trace_id, write_audit
from .schemas import HandoffCreateParams, HandoffPriority, MessageIn
from .schemas.conversation import Message
from .security import make_idempotency_key
from .tools import ToolContext, ToolRegistry
from .tools.base import _mask_args

logger = get_logger("orchestrator")


# ---------------------------------------------------------------------------
# 协议区（可被子任务 3/4 import）
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """六类事件（设计文档 §2.3）。"""

    THINKING = "thinking"        # 阶段信号（前端"正在…"状态）
    TOOL_CALL = "tool_call"      # LLM 决定调用某工具
    TOOL_RESULT = "tool_result"  # 工具执行结果摘要（含溯源 meta）
    TOKEN = "token"              # LLM 文本增量（真流式透传）
    DONE = "done"                # 终态：本轮对话完成
    ERROR = "error"              # 终态：编排层自身故障


@dataclass
class Event:
    """事件信封。``session_id`` 必回传（新会话/重建时前端需替换）。"""

    type: EventType
    data: dict[str, Any]
    session_id: str | None = None
    trace_id: str | None = None


@dataclass
class ToolCallRequest:
    """LLM 请求的一次工具调用（OpenAI tool_calls 元素的直译）。"""

    id: str
    name: str
    arguments: str  # JSON 字符串，原样保留，由编排层解析


@dataclass
class LLMChunk:
    """流式块。约定：delta 与 tool_calls 不同时出现；
    tool_calls（聚合完成态）只出现在流的最后一个块。"""

    delta: str | None = None
    tool_calls: list[ToolCallRequest] | None = None
    finish_reason: str | None = None  # "stop" | "tool_calls" | "length"


class LLMClient(Protocol):
    """子任务 3 的实现契约：流式增量封装在适配器内，编排层只见块序列。"""

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]: ...


# ---------------------------------------------------------------------------
# 常量区
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_PROMPT = """你是湖南理工大学的校园助手，为学生提供课表、成绩、图书馆借阅查询与提醒订阅服务。

回答规则：
1. 只依据工具返回的数据回答，不编造任何事实；未查询到的信息就说不知道。
2. 工具结果标注 is_stale=true 或 degrade_status=stale_cache 时，必须在回答中说明"该数据来自缓存（同步于 <synced_at>），可能不是最新"。
3. 工具返回 unavailable 时，明确告知该服务暂时不可用，不要猜测结果。
4. 不回答"能否毕业、学籍处分、评优资格"等终局性问题——请学生咨询二级学院教务办。
5. 不查询、不推断他人信息；涉及隐私的请求一律拒绝。
6. 需要数据时直接调用工具，不要向用户复述参数或询问你已能推断的信息。
7. 身份信息（student_id）由系统自动注入，你不需要也无法填写。
8. 续借是唯一需要用户确认的操作：必须先查询在借清单，向用户完整展示将续借的图书条码与到期日变化，明确请用户回复"确认续借"；收到确认后再次调用续借工具，两次调用的图书条码必须完全一致。确认状态由系统核验，不要试图代替用户确认。"""

# 星期中文映射（05 §6.3 遗留项①：当前日期注入的换算辅助）
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 规则 → 优先级 / 归属映射（11 §4.2 SLA 表，设计文档 F2）
REDLINE_PRIORITY: dict[str, HandoffPriority] = {
    "R5_生命优先": HandoffPriority.P0,
    "R3_敏感信息": HandoffPriority.P1,
    "R4_不代表达诉求": HandoffPriority.P1,
    "R1_终局性结论": HandoffPriority.P2,
}
REDLINE_OWNER: dict[str, str] = {
    "R5_生命优先": "学生工作部/心理中心",
    "R3_敏感信息": "现代教育技术中心",
    "R4_不代表达诉求": "二级学院教务办",
    "R1_终局性结论": "二级学院教务办",
}

R5_LIFELINE_SCRIPT = (
    "同学你好，谢谢你愿意把这些说出来，你现在的安全是最重要的事。"
    "学校心理健康中心随时可以为你提供支持（大学生活动中心 3 楼），"
    "24 小时心理援助热线：0730-8888888，也可以直接联系你的辅导员。"
    "已为你转接人工老师，请一定留意回复，我们不会让你一个人面对。"
)

BLOCKED_REPLY_TEMPLATES: dict[str, str] = {
    "R1_终局性结论": (
        "毕业资格、学籍等终局性判断需要由二级学院教务办老师依据正式规则答复，"
        "我不能代替老师下结论。已为你创建人工工单，老师会尽快联系你，请留意回复。"
    ),
    "R3_敏感信息": (
        "这个问题涉及个人敏感信息，为保护同学隐私我无法直接处理。"
        "已为你转接相关老师核实处理，请留意回复。"
    ),
    "R4_不代表达诉求": (
        "代你提交正式申请需要本人操作或人工老师协助完成，我不能代替你表达诉求。"
        "已为你创建人工工单，老师会联系你一起完成，请留意回复。"
    ),
}

# 写工具的服务端注入字段（F4b）；读工具仅剥离 student_id
WRITE_GUARDED_FIELDS = ("student_id", "confirmed", "idempotency_key")

# 阶段一唯一写白名单成员（F4b 门禁适用范围）
RENEW_TOOL_NAME = "library_renew"

# 续借确认关键词集（确定性匹配，P4：授权判定不允许概率性组件）
RENEW_CONFIRM_KEYWORDS = ("确认续借", "确定续借", "继续续借", "确认", "确定")

HARD_FUSE_SCRIPT = (
    "查询步骤较多暂时未能完成，建议把问题拆分得更具体一些，"
    "或者直接转人工老师协助处理。给你带来不便非常抱歉。"
)

# 不暴露给 LLM 的工具（F4b）：handoff_create 由编排层确定性路径调用（F2），
# 不经 LLM——否则模型可伪造 hit_rules/priority 生成 P0 危机工单
LLM_EXCLUDED_TOOLS = ("handoff_create",)


class LLMStreamFailure(Exception):
    """主循环 LLM 流式请求失败（F6：首请求失败 → llm_unavailable 终态）。"""


def _split_script(text: str, size: int = 40) -> list[str]:
    """固定话术按 ~40 字分片为 token 事件。"""
    return [text[i : i + size] for i in range(0, len(text), size)]


def _call_to_dict(call: ToolCallRequest) -> dict[str, Any]:
    """ToolCallRequest → OpenAI tool_calls 元素（落库/回填消息用）。"""
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


def _find_last_renew_call(
    history: list[Message], scan: int = 10
) -> dict[str, Any] | None:
    """扫描历史尾部，找最近一次落库的 ``library_renew`` 调用参数（预览轮证据）。

    返回解析后的 arguments dict；无证据返回 None。解析失败继续向前找。
    """
    for m in reversed(history[-scan:]):
        if m.role != "assistant" or not m.tool_calls:
            continue
        for tc in reversed(m.tool_calls):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") == RENEW_TOOL_NAME:
                try:
                    parsed = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class Orchestrator:
    """M2 编排核心。请求级实例（registry.services 内含请求级 db 会话），
    由子任务 4 的 FastAPI 依赖注入构造。"""

    def __init__(
        self,
        store: SessionStore,
        registry: ToolRegistry,
        llm: LLMClient,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.llm = llm
        self.system_prompt = system_prompt
        self.settings = settings or store.settings

    # ---- 唯一公开入口 ----

    async def run(
        self,
        student_id: str,
        user_input: str,
        session_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """对话主入口。``student_id`` 由 API 层 JWT 注入传入；
        ``session_id`` 为空/失效/所有者不匹配时自动新建并在 done 事件回传。"""
        trace_id = set_trace_id()
        try:
            session = await self._ensure_session(student_id, session_id)
            yield self._emit(EventType.THINKING, {"phase": "session_ready"}, session)

            # ---- F2 红线门（LLM 零参与的确定性路径）----
            gr = check_redlines(user_input)
            if gr.hit:
                async for ev in self._redline_block(
                    student_id, session, user_input, gr, trace_id
                ):
                    yield ev
                return

            # ---- F3 历史与压缩（显式探测窗口，防"永不触发"）----
            await self.store.append(
                session.session_id,
                MessageIn(role="user", content=user_input, trace_id=trace_id),
            )
            probe = await self.store.load_history(
                session.session_id,
                limit=self.settings.orchestrator_history_fetch_limit,
            )
            history = probe
            compressed = False
            if len(probe) > self.settings.session_history_limit:
                yield self._emit(EventType.THINKING, {"phase": "compressing"}, session)
                compressed = await self._compress_or_degrade(
                    student_id, session, probe, trace_id
                )
                if compressed:
                    # 压缩成功后重载（此时活跃 = 摘要 + 最近 6 条 ≈ 7 条）
                    history = await self.store.load_history(session.session_id)
                # 失败降级：以 probe 为上下文继续（最近 60 条，必含本轮 user 消息）

            # ---- F4/F5 Agent Loop（含熔断状态机）----
            messages = self._build_llm_messages(history)
            async for ev in self._agent_loop(
                student_id, session, messages, user_input, history,
                trace_id, compressed,
            ):
                yield ev
        except SessionExpired:
            # 双保险：ensure 之后的竞态过期 → 提示重试
            yield self._emit(
                EventType.ERROR,
                {"code": "storage_error", "message": "会话已过期，请重新发起"},
                None,
            )
        except LLMStreamFailure:
            # F6 llm_unavailable：主循环首请求即失败（本轮 user 消息已落库）
            logger.exception("llm_first_request_failed")
            yield self._emit(
                EventType.ERROR,
                {"code": "llm_unavailable", "message": "AI 服务暂时不可用，请稍后重试"},
                None,
            )
        except Exception:
            logger.exception("orchestrator_failed")
            yield self._emit(
                EventType.ERROR,
                {"code": "internal", "message": "服务暂时不可用，请稍后重试"},
                None,
            )

    # ---- F1 会话准备 + 所有者校验（P0-2）----

    async def _ensure_session(self, student_id: str, session_id: str | None):
        """session_id 为空/不存在/过期/所有者不匹配 → 静默新建。

        不匹配时与"不存在"走完全相同路径（不泄露会话存在性，抵抗枚举探测），
        但留 ``owner_mismatch`` 审计。
        """
        if session_id:
            conv = None
            try:
                conv = await self.store.get(session_id)
            except (SessionNotFound, SessionExpired):
                conv = None  # 落到下方新建
            if conv is not None:
                if conv.student_id != student_id:
                    # 越权尝试是安全事件，必须留痕；审计失败不阻断。
                    try:
                        await write_audit(
                            self.store.db,
                            action="owner_mismatch",
                            student_id=student_id,
                            session_id=session_id,
                            args_digest={"requested": session_id},
                            result="rejected",
                        )
                    except Exception:
                        logger.exception("owner_mismatch_audit_failed")
                else:
                    return conv  # 唯一合法的"续用"路径
        return await self.store.create(student_id)

    # ---- F2 红线拦截 + 转人工工单 ----

    async def _redline_block(
        self,
        student_id: str,
        session,
        user_input: str,
        gr: GuardrailResult,
        trace_id: str,
    ) -> AsyncIterator[Event]:
        """命中红线：固定话术 + handoff 工单 + 审计，全部确定性代码。

        动作顺序（设计文档 F2）：① user + 拦截话术先落库——被拦截的对话
        仍是完整会话，工单失败也不丢危机输入；② 工单创建（失败不阻断
        话术输出，仅影响 ticket 回传）；③ 审计；④ token + done。
        """
        # 混合命中时路由取最高危规则：guardrail 按字典序收集，rules[0]
        # 恒为低危（R1）；生命优先必须按 R5 路由，否则危机工单被降级。
        rule = "R5_生命优先" if gr.life_priority else gr.rules[0]
        if gr.life_priority:
            template = R5_LIFELINE_SCRIPT
        else:
            template = BLOCKED_REPLY_TEMPLATES.get(rule, BLOCKED_REPLY_TEMPLATES["R1_终局性结论"])

        msgs = await self.store.append_many(
            session.session_id,
            [
                MessageIn(role="user", content=user_input, trace_id=trace_id),
                MessageIn(role="assistant", content=template, trace_id=trace_id),
            ],
        )

        ctx = self._make_ctx(student_id, session)
        ticket = None
        ticket_error: str | None = None
        try:
            ticket = await self.registry.dispatch(
                "handoff_create",
                ctx,
                HandoffCreateParams(
                    student_id=student_id,  # 服务端以 ctx 覆盖（R1.2）
                    session_id=session.session_id,
                    user_question=user_input,
                    hit_rules=gr.rules,
                    agent_response_log=template,
                    suggested_owner=REDLINE_OWNER.get(rule),
                    priority=REDLINE_PRIORITY[rule],
                ),
            )
        except Exception as exc:
            # P5：每个分支都有用户可见出口——工单失败不阻断话术输出
            ticket_error = str(exc) or exc.__class__.__name__
            logger.exception("handoff_dispatch_failed")

        yield self._emit(
            EventType.TOOL_CALL,
            {
                "tool": "handoff_create",
                "call_id": f"handoff_{trace_id}",
                "args_digest": {"hit_rules": gr.rules, "priority": REDLINE_PRIORITY[rule].value},
                "seq": 0,
            },
            session,
        )
        result_data: dict[str, Any] = {
            "tool": "handoff_create",
            "call_id": f"handoff_{trace_id}",
            "ok": ticket is not None,
        }
        if ticket is not None:
            result_data["meta"] = ticket.meta.model_dump(mode="json")
            result_data["result_digest"] = {"ticket_id": ticket.id}
        else:
            result_data["error"] = ticket_error
        yield self._emit(EventType.TOOL_RESULT, result_data, session)

        try:
            await write_audit(
                self.store.db,
                action="redline_block",
                student_id=student_id,
                session_id=session.session_id,
                args_digest={"hit_rules": gr.rules},
                result="blocked",
            )
        except Exception:
            logger.exception("redline_audit_failed")

        for piece in _split_script(template):
            yield self._emit(EventType.TOKEN, {"delta": piece}, session)
        yield self._emit(
            EventType.DONE,
            {
                "session_id": session.session_id,
                "assistant_message_id": msgs[1].id,
                "compressed": False,
                "tool_iterations": 0,
                "budget_exhausted": False,
                "blocked": True,
                "handoff_ticket_id": ticket.id if ticket is not None else None,
            },
            session,
        )

    # ---- F3 会话压缩 ----

    SUMMARY_SYSTEM_PROMPT = (
        "你是会话压缩器。请把以下对话历史压缩为不超过 200 字的中文摘要，"
        "只保留用户意图与关键结论；不得包含成绩数值与任何身份证明文（R3）。"
        "直接输出摘要正文，不要任何解释。"
    )

    async def _compress_or_degrade(
        self, student_id: str, session, probe: list[Message], trace_id: str
    ) -> bool:
        """调 LLM 生成摘要并 ``store.compress`` 落库；失败降级返回 False。

        降级正确性无损：最新消息在探测窗口内（P0-1 尾部窗口语义），
        调用方以 probe 为上下文继续对话。
        """
        kept_tail = max(4, self.settings.session_compress_tail)
        to_summarize = probe[:-kept_tail] if len(probe) > kept_tail else []
        if not to_summarize:
            return False

        summary_messages: list[dict] = [
            {"role": "system", "content": self.SUMMARY_SYSTEM_PROMPT},
            *(self._to_openai(m) for m in to_summarize),
        ]
        text = ""
        try:
            # 非流式场景：忽略 delta 逐块拼接，不 yield token（摘要对用户不可见）
            async for chunk in self.llm.stream(summary_messages, tools=None):
                if chunk.delta:
                    text += chunk.delta
        except Exception:
            logger.exception("summary_llm_failed")
            return False

        summary = text.strip()[: self.settings.orchestrator_summary_max_chars]
        if not summary:
            logger.warning("summary_empty_skipped")
            return False

        comp = await self.store.compress(session.session_id, summary)
        try:
            await write_audit(
                self.store.db,
                action="session_compress",
                student_id=student_id,
                session_id=session.session_id,
                args_digest={
                    "summarized_count": comp.summarized_count,
                    "kept_tail": comp.kept_tail,
                },
                result="ok",
            )
        except Exception:
            logger.exception("session_compress_audit_failed")
        return True

    # ---- F4 工具路由主循环 ----

    async def _agent_loop(
        self,
        student_id: str,
        session,
        messages: list[dict],
        user_input: str,
        history: list[Message],
        trace_id: str,
        compressed: bool,
    ) -> AsyncIterator[Event]:
        """Agent Loop + 熔断状态机（F5）：fused 置位后永不清零。"""
        ctx = self._make_ctx(student_id, session)
        used = 0
        fused = False
        while True:
            if fused:
                # ---- 收尾请求：至多一次，不带 tools；幻觉 tool_calls 一律忽略 ----
                yield self._emit(
                    EventType.THINKING, {"phase": "budget_exhausted"}, session
                )
                text = ""
                try:
                    async for chunk in self.llm.stream(messages, tools=None):
                        if chunk.delta:
                            text += chunk.delta
                            yield self._emit(
                                EventType.TOKEN, {"delta": chunk.delta}, session
                            )
                except Exception:
                    # ③ 硬兜底：固定文案分片为 token 事件
                    logger.exception("fuse_finalize_failed")
                    text = HARD_FUSE_SCRIPT
                    for piece in _split_script(text):
                        yield self._emit(EventType.TOKEN, {"delta": piece}, session)
                final_msg = await self._finalize(session, text, trace_id)
                yield self._emit(
                    EventType.DONE,
                    {
                        "session_id": session.session_id,
                        "assistant_message_id": final_msg.id,
                        "compressed": compressed,
                        "tool_iterations": used,
                        "budget_exhausted": True,
                        "blocked": False,
                    },
                    session,
                )
                return

            # 非 fused 期间始终携带工具表；超限由批级原子拒绝统一判定
            # （拒绝即熔断：used==budget 时任何新批必然 6+1>6 → 拒绝 → fused）
            tools = self._llm_tools()

            final: LLMChunk | None = None
            text = ""
            try:
                async for chunk in self.llm.stream(messages, tools=tools):
                    if chunk.delta:
                        text += chunk.delta
                        yield self._emit(EventType.TOKEN, {"delta": chunk.delta}, session)
                    final = chunk
            except Exception as exc:
                if used == 0:
                    # F6 llm_unavailable：主循环首请求即失败且非 FUSED 收尾
                    raise LLMStreamFailure("llm_first_request_failed") from exc
                raise
            if final is not None and final.tool_calls:
                batch = final.tool_calls
                if used + len(batch) > self.settings.orchestrator_tool_budget:
                    # ① 批级原子拒绝 → 拒绝即置位 fused（消除"拒绝后仍带工具表重试"）
                    new_msgs = await self._reject_batch(session, batch, trace_id)
                    messages = messages + [self._to_openai(m) for m in new_msgs]
                    try:
                        await write_audit(
                            self.store.db,
                            action="budget_exhausted",
                            student_id=student_id,
                            session_id=session.session_id,
                            args_digest={"used": used, "rejected": len(batch)},
                            result="fused",
                        )
                    except Exception:
                        logger.exception("budget_audit_failed")
                    fused = True
                    continue  # 下一轮进收尾分支（不再带 tools）

                assistant_in = MessageIn(
                    role="assistant",
                    content=text,
                    tool_calls=[_call_to_dict(c) for c in batch],
                    trace_id=trace_id,
                )
                tool_msgs: list[MessageIn] = []
                for call in batch:
                    ev_call, ev_result, result_msg = await self._dispatch_one(
                        ctx, call, session, user_input, history,
                        used + len(tool_msgs), trace_id,
                    )
                    yield ev_call
                    yield ev_result
                    tool_msgs.append(result_msg)
                # 中间消息即时落库（断线安全 + F4b 跨轮确认查证有据可依）
                await self.store.append_many(
                    session.session_id, [assistant_in, *tool_msgs]
                )
                messages = messages + [
                    self._to_openai(assistant_in),
                    *(self._to_openai(m) for m in tool_msgs),
                ]
                used += len(batch)
                continue
            # 纯文本 → 正常收尾
            final_msg = await self._finalize(session, text, trace_id)
            yield self._emit(
                EventType.DONE,
                {
                    "session_id": session.session_id,
                    "assistant_message_id": final_msg.id,
                    "compressed": compressed,
                    "tool_iterations": used,
                    "budget_exhausted": False,
                    "blocked": False,
                },
                session,
            )
            return

    async def _reject_batch(
        self, session, batch: list[ToolCallRequest], trace_id: str
    ) -> list[MessageIn]:
        """被拒批的协议配对消息（难点 3）：每个 tool_call 一条"未执行"回填。"""
        assistant_in = MessageIn(
            role="assistant",
            content="",
            tool_calls=[_call_to_dict(c) for c in batch],
            trace_id=trace_id,
        )
        reason = f"未执行：已达单轮工具调用上限（{self.settings.orchestrator_tool_budget} 次）"
        tool_msgs = [
            MessageIn(role="tool", content=reason, tool_call_id=c.id, trace_id=trace_id)
            for c in batch
        ]
        await self.store.append_many(session.session_id, [assistant_in, *tool_msgs])
        return [assistant_in, *tool_msgs]

    async def _dispatch_one(
        self,
        ctx: ToolContext,
        call: ToolCallRequest,
        session,
        user_input: str,
        history: list[Message],
        seq: int,
        trace_id: str,
    ) -> tuple[Event, Event, MessageIn]:
        """单次工具调用：解析 → 参数净化 → dispatch → 事件对 + 回填消息。

        任何失败（JSON 解析 / 校验 / 工具异常）都不中断循环，
        回填 ``{role: tool, content: "调用失败：<原因>"}`` 让 LLM 转述（F4-6）。
        """
        call_event = self._emit(
            EventType.TOOL_CALL,
            {"tool": call.name, "call_id": call.id, "seq": seq},
            session,
        )
        try:
            args = json.loads(call.arguments)
            if not isinstance(args, dict):
                raise ValueError("arguments 必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            # 失败路径同样携带 args_digest（协议 §2.3）；不可解析时只放
            # 标记不放原文——原文可能含模型伪造的他人身份字段（P6）
            call_event.data["args_digest"] = {"parse": "failed"}
            return (
                call_event,
                self._tool_failure_event(call, session, f"arguments 解析失败：{exc}"),
                MessageIn(
                    role="tool",
                    content=f"调用失败：arguments 解析失败：{exc}",
                    tool_call_id=call.id,
                    trace_id=trace_id,
                ),
            )

        # P6 参数净化：身份字段一律剥离后由服务端注入（R1.2 闭环）
        args.pop("student_id", None)
        args["student_id"] = ctx.student_id

        # F4b 写门禁：写工具的授权/幂等字段由服务端决定，模型自填无效
        if call.name == RENEW_TOOL_NAME:
            barcodes = args.get("barcodes") or []
            args.pop("confirmed", None)
            args.pop("idempotency_key", None)
            args["confirmed"] = self._renew_gate(user_input, history, barcodes)
            args["idempotency_key"] = make_idempotency_key(
                ctx.session_id or "", barcodes, date.today().isoformat()
            )
            if args["confirmed"]:
                logger.info(
                    "renew_gate_passed",
                    barcodes=len(barcodes),
                )
            else:
                logger.info("renew_gate_blocked", barcodes=len(barcodes))

        try:
            result = await self.registry.dispatch(call.name, ctx, args)
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            call_event.data["args_digest"] = _mask_args(args)
            return (
                call_event,
                self._tool_failure_event(call, session, reason),
                MessageIn(
                    role="tool",
                    content=f"调用失败：{reason}",
                    tool_call_id=call.id,
                    trace_id=trace_id,
                ),
            )

        dumped = result.model_dump(mode="json")
        result_event = self._emit(
            EventType.TOOL_RESULT,
            {
                "tool": call.name,
                "call_id": call.id,
                "ok": True,
                "meta": dumped.get("meta"),
                "result_digest": _result_digest(dumped),
            },
            session,
        )
        # 事件流只发摘要级结果，明细由 LLM 最终回答转述
        call_event.data["args_digest"] = _mask_args(args)
        return (
            call_event,
            result_event,
            MessageIn(
                role="tool",
                content=json.dumps(dumped, ensure_ascii=False),
                tool_call_id=call.id,
                trace_id=trace_id,
            ),
        )

    def _tool_failure_event(self, call: ToolCallRequest, session, reason: str) -> Event:
        return self._emit(
            EventType.TOOL_RESULT,
            {
                "tool": call.name,
                "call_id": call.id,
                "ok": False,
                "error": reason,
            },
            session,
        )

    # ---- F4b 写操作确认门禁（纯函数 + 历史查证，P4 确定性）----

    def _renew_gate(
        self, user_input: str, history: list[Message], barcodes: list[str]
    ) -> bool:
        """三重校验：预览痕迹 + 用户确认词 + 条码绑定。任一不满足 → False。

        ① 历史痕迹：会话尾部存在落库的 ``library_renew`` tool_calls（预览轮证据）；
        ② 用户显式确认：本轮 user_input 命中确认关键词；
        ③ 操作内容绑定：本轮 barcodes 与预览轮完全一致（防"确认 A 执行 B"）。
        """
        preview_args = _find_last_renew_call(history)
        if preview_args is None:
            return False  # ① 无预览轮证据（首轮就执行 = 无中生有）
        if not any(k in user_input for k in RENEW_CONFIRM_KEYWORDS):
            return False  # ② 用户未显式确认
        return set(preview_args.get("barcodes") or []) == set(barcodes)  # ③

    # ---- F6 收尾 ----

    async def _finalize(self, session, text: str, trace_id: str) -> Message:
        """最终 assistant 消息落库。宁缺半截：text 为空也落一条占位。"""
        return await self.store.append(
            session.session_id,
            MessageIn(role="assistant", content=text, trace_id=trace_id),
        )

    # ---- 辅助 ----

    def _llm_tools(self) -> list[dict]:
        """暴露给 LLM 的工具表：剔除编排层确定性路径专用的工具（F4b）。"""
        return [
            t
            for t in self.registry.openai_tools()
            if t["function"]["name"] not in LLM_EXCLUDED_TOOLS
        ]

    def _make_ctx(self, student_id: str, session) -> ToolContext:
        return ToolContext(
            student_id=student_id,
            services=self.registry.services,
            session_id=session.session_id,
            channel=session.channel,
        )

    def _emit(self, type_: EventType, data: dict[str, Any], session) -> Event:
        return Event(
            type=type_,
            data=data,
            session_id=session.session_id if session is not None else None,
            trace_id=get_trace_id(),
        )

    def _to_openai(self, m: MessageIn) -> dict[str, Any]:
        """MessageIn → OpenAI Chat Completions 消息元素。"""
        if m.role == "assistant" and m.tool_calls:
            return {
                "role": "assistant",
                "content": m.content or "",
                "tool_calls": m.tool_calls,
            }
        if m.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": m.tool_call_id,
                "content": m.content,
            }
        return {"role": m.role, "content": m.content}

    def _build_llm_messages(self, history: list[Message]) -> list[dict]:
        """上下文组装：system prompt 永远第一，摘要紧随其后，其余按 seq 原序。

        system prompt 尾部每轮动态拼入当前日期（05 §6.3 遗留项①）：模型
        无时钟感，缺日期时"明天"会落到训练截止附近——相对日期类查询
        全错是正确性问题而非幻觉，必须由编排层在组装时注入。
        """
        today = date.today()
        date_line = (
            f"当前日期：{today.isoformat()}（{_WEEKDAY_CN[today.weekday()]}）。"
            "用户提到的相对日期（今天/明天/下周等）一律以此为准换算。"
        )
        messages: list[dict] = [
            {"role": "system", "content": f"{self.system_prompt}\n\n{date_line}"}
        ]
        for m in history:
            if m.is_summary:
                messages.append({"role": "system", "content": m.content})
        for m in history:
            if not m.is_summary:
                messages.append(self._to_openai(m))
        return messages


def _result_digest(dumped: dict[str, Any]) -> dict[str, Any]:
    """tool_result 事件只放摘要级结果（如 {count: 5}），不搬运大 payload。"""
    digest: dict[str, Any] = {}
    for key in ("items", "results"):
        if isinstance(dumped.get(key), list):
            digest["count"] = len(dumped[key])
    return digest
