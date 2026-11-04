"""编排核心包（M2，2026-11-04 由单文件 orchestrator.py 按职责拆分）。

公共 API 不变——``from app.orchestrator import Orchestrator`` 等既有导入
（app/main.py、app/llm/adapter.py、tests/、eval/、demo）全部经本包重导出
继续可用，``app/llm/adapter.py`` 的 import 一字未动。

模块地图：
- events.py            六类事件协议 + LLMClient Protocol（03 §2.3/§2.4，已冻结）
- loop.py              对话循环主体（run() 单一入口 + Agent Loop + 写门禁）
- summarize.py         F3 历史压缩（生成摘要 → 落库 → 审计，失败降级）
- guardrail_assets.py  确定性防线资产（红线话术/SLA 映射/写门禁词表/熔断文案）

设计原则（03 §2.1）见 loop.py 模块注释。
"""

from .events import Event, EventType, LLMChunk, LLMClient, ToolCallRequest
from .loop import (
    DEFAULT_SYSTEM_PROMPT,
    LLMStreamFailure,
    Orchestrator,
)

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "Event",
    "EventType",
    "LLMChunk",
    "LLMClient",
    "LLMStreamFailure",
    "Orchestrator",
    "ToolCallRequest",
]
