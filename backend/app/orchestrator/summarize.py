"""F3 会话压缩：调 LLM 生成摘要并落库，失败降级（P0-1 尾部窗口语义）。

压缩触发与探测窗口在 loop.py 的 run() 里；本模块只负责"生成摘要 →
store.compress 落库 → 审计"这一段。降级正确性无损：最新消息在探测
窗口内，压缩失败时调用方以 probe 为上下文继续对话。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..conversation import SessionStore
from ..logging_util import get_logger, write_audit
from ..schemas.conversation import Message, MessageIn
from .events import LLMClient

logger = get_logger("orchestrator.summarize")

SUMMARY_SYSTEM_PROMPT = (
    "你是会话压缩器。请把以下对话历史压缩为不超过 200 字的中文摘要，"
    "只保留用户意图与关键结论；不得包含成绩数值与任何身份证明文（R3）。"
    "直接输出摘要正文，不要任何解释。"
)


async def compress_or_degrade(
    *,
    store: SessionStore,
    llm: LLMClient,
    settings: Settings,
    student_id: str,
    session,
    probe: list[Message],
    trace_id: str,
    to_openai: Callable[[MessageIn], dict[str, Any]],
) -> bool:
    """生成摘要并 ``store.compress`` 落库；失败降级返回 False。

    ``to_openai`` 由调用方（loop.py）注入消息序列化函数——压缩上下文与
    主循环的 OpenAI 消息格式必须同源，不允许两套实现漂移。
    """
    kept_tail = max(4, settings.session_compress_tail)
    to_summarize = probe[:-kept_tail] if len(probe) > kept_tail else []
    if not to_summarize:
        return False

    summary_messages: list[dict] = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        *(to_openai(m) for m in to_summarize),
    ]
    text = ""
    try:
        # 非流式场景：忽略 delta 逐块拼接，不产出 token（摘要对用户不可见）
        async for chunk in llm.stream(summary_messages, tools=None):
            if chunk.delta:
                text += chunk.delta
    except Exception:
        logger.exception("summary_llm_failed")
        return False

    summary = text.strip()[: settings.orchestrator_summary_max_chars]
    if not summary:
        logger.warning("summary_empty_skipped")
        return False

    comp = await store.compress(session.session_id, summary)
    try:
        await write_audit(
            store.db,
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
