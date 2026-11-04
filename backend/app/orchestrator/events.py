"""事件流协议与 LLM 客户端契约（设计文档 03 §2.3/§2.4，已冻结）。

六类事件是编排层对外的唯一输出语言（SSE 端点逐帧透传）；
``LLMClient`` Protocol 是子任务 3 的实现契约——编排层只见块序列，
流式增量封装在适配器内。两个协议变更必须走设计文档修订。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


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
