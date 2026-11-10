"""MCP HTTP 挂载与生命周期（文档 19 §二 D3，M2）。

最大的坑（python-sdk#1367）：Starlette ``app.mount()`` 不执行子应用
lifespan——MCP Streamable HTTP 的会话管理器若没在父 lifespan 里手动启动，
所有 /mcp 请求静默失败。正确接线：:func:`mount_mcp` 挂载 + main.py 在其
lifespan 内 enter/exit :func:`mcp_lifespan`（yield 前 enter、后 exit）。

第二个坑（实测发现）：Starlette Mount 的匹配要求前缀后至少一个字符
（``"/mcp/{path:path}"``），客户端配的端点 URL 通常是 ``.../mcp``（无尾
斜杠）——不处理则 redirect_slashes 307（官方客户端跟随，第三方未必）。
归一逻辑在 :class:`StudentIdentityMiddleware` 内完成（主 app 级中间件、
仅 /mcp 前缀生效），详见 identity.py。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..orchestrator import Orchestrator
from ..tools import Services, ToolRegistry
from .bridge import build_mcp_server
from .identity import StudentIdentityMiddleware


def mount_mcp(
    app: FastAPI,
    settings: Settings,
    registry: ToolRegistry,
    session_factory: async_sessionmaker[AsyncSession],
    services_factory: Callable[[AsyncSession], Services],
    orchestrator_factory: Callable[[AsyncSession], Orchestrator],
) -> FastMCP:
    """构建 MCP server 并挂载到 ``/mcp`` 前缀（身份中间件仅覆盖该前缀）。

    ``mcp_enabled=False`` 时 main.py 根本不调用本函数——挂载与中间件都
    不发生，/mcp 保持 404（D4）。返回 FastMCP 实例：main.py 须在 lifespan
    内 enter :func:`mcp_lifespan`。
    """
    mcp = build_mcp_server(
        settings,
        registry,
        session_factory,
        services_factory,
        orchestrator_factory,
    )
    # 子应用内路由定在 "/"：外层 Mount("/mcp") 剥前缀后剩余 "/" 恰好对上，
    # 端点 URL = /mcp（无尾斜杠由身份中间件归一，identity.py）
    # ⚠️ 这是 SDK 内部属性改写（非公开 API，mcp>=1.20 实测行为）：升级 mcp
    # 依赖版本时先重跑 tests/test_mcp_server.py 的 L3 HTTP 档作护栏。
    mcp.settings.streamable_http_path = "/"
    app.add_middleware(StudentIdentityMiddleware)  # 主 app 级，仅 /mcp 前缀强制身份
    app.mount("/mcp", mcp.streamable_http_app())  # session manager 在此懒创建（仅此一次）
    return mcp


@asynccontextmanager
async def mcp_lifespan(mcp: FastMCP):
    """MCP 会话管理器生命周期——必须在父 app lifespan 中手动 enter/exit。

    ``app.mount()`` 不执行子应用 lifespan（python-sdk#1367）：不启动会话
    管理器则所有 /mcp 请求静默失败。
    """
    async with mcp.session_manager.run():
        yield
