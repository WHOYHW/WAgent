"""工具注册表：登记阶段一 6 个工具（§3.2.1）。"""

from __future__ import annotations

from .base import Services, ToolRegistry
from .grade import GradeTool
from .handoff import HandoffCreateTool
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


def build_registry(services: Services | None = None) -> ToolRegistry:
    registry = ToolRegistry(services)
    for tool in _ALL_TOOLS:
        registry.register(tool)
    return registry


# 全局默认注册表（无共享缓存/会话依赖，仅用于导出 Schema）。
default_registry = build_registry()
