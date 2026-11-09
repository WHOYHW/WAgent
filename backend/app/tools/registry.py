"""工具注册表：登记阶段一 6 个工具（§3.2.1）+ kb_search 门控注册（文档 18 §五 D5）。

门控裁决（v1.5）：``build_registry`` 按 ``services.settings.kb_enabled`` 注册
``kb_search``——False 时工具根本不进注册表（LLM 工具列表不出现），合并即零行为
变化；``default_registry`` 以 ``Settings(kb_enabled=True)`` 显式构建，定位是
**全量词表**（Schema 导出 + 黄金集 validate_cases 受控词表），与运行时门控
注册表是两个用途，词表自动跟随工具改名防静默腐烂。
"""

from __future__ import annotations

from ..config import Settings
from .base import Services, ToolRegistry
from .grade import GradeTool
from .handoff import HandoffCreateTool
from .kb import KnowledgeSearchTool
from .library import LibraryLoansTool, LibraryRenewTool
from .reminder import ReminderSubscribeTool
from .timetable import TimetableTool

_ALL_TOOLS = (
    TimetableTool(),
    GradeTool(),
    LibraryLoansTool(),
    LibraryRenewTool(),
    ReminderSubscribeTool(),
    HandoffCreateTool(),
)

_GATED_TOOLS = (KnowledgeSearchTool(),)  # kb_enabled 门控（文档 18 D5）


def build_registry(services: Services | None = None) -> ToolRegistry:
    registry = ToolRegistry(services)
    for tool in _ALL_TOOLS:
        registry.register(tool)
    if registry.services.settings.kb_enabled:
        for tool in _GATED_TOOLS:
            registry.register(tool)
    return registry


# 全局默认注册表（无共享缓存/会话依赖）：全量词表，kb 门控强制开启，
# 仅用于 Schema 导出与黄金集受控词表，不用于运行时调度。
default_registry = build_registry(Services(settings=Settings(kb_enabled=True)))
