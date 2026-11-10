"""MCP 服务暴露层（文档 19）：registry 只读工具桥接 + JWT 传输层身份注入。

分层契约（M3 补 lint-imports 条目）：app.mcp 位于 app.tools 之下（桥接
registry 工具内核与 kb 检索契约），不感知编排/LLM 层。
"""

from .bridge import build_mcp_server
from .identity import StudentIdentityMiddleware, current_student
from .server import mcp_lifespan, mount_mcp

__all__ = [
    "StudentIdentityMiddleware",
    "build_mcp_server",
    "current_student",
    "mcp_lifespan",
    "mount_mcp",
]
