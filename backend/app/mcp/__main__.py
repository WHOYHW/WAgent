"""stdio 演示入口（dev-only，文档 19 §二 D1/D3）：``python -m app.mcp``。

- 门控：``mcp_enabled=False`` 拒绝启动（D4 同源：能力上线是运营决策）。
- 身份：stdio 无传输层，取 ``MCP_DEMO_STUDENT_ID`` 环境变量（未配置拒绝
  启动）；生产禁用本入口。
- 装配：与 HTTP 档共享同一 ``build_mcp_server``，工具行为零分叉；审计
  channel="mcp-stdio" 可区分演示通道。
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    from ..config import settings
    from ..conversation import SessionStore
    from ..db import async_session_factory
    from ..llm.adapter import ArkLLMClient
    from ..orchestrator import Orchestrator
    from ..tools import Services, build_registry
    from .bridge import build_mcp_server
    from .identity import current_student

    if not settings.mcp_enabled:
        print(
            "mcp_enabled=False：MCP 能力未开启（文档 19 D4），stdio 入口拒绝启动",
            file=sys.stderr,
        )
        raise SystemExit(2)
    demo_student_id = os.environ.get("MCP_DEMO_STUDENT_ID")
    if not demo_student_id:
        print(
            "MCP_DEMO_STUDENT_ID 未配置：stdio 档无传输层身份，拒绝启动"
            "（文档 19 D1；生产禁用 stdio 入口）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    # set 必须先于 mcp.run()（anyio.run 的任务继承调用方 context），之后所有
    # 工具调用的 dispatch 都能从 contextvar 取到演示身份。
    current_student.set(demo_student_id)

    llm = ArkLLMClient()  # stdio 演示进程级单例（镜像 main.py _llm_client 语义）
    services = Services(settings=settings)

    def _orchestrator_factory(db):
        # 镜像 main.py get_orchestrator：每调用独立 db 会话 → Services → registry
        return Orchestrator(SessionStore(db, settings), build_registry(services), llm)

    mcp = build_mcp_server(
        settings=settings,
        registry=build_registry(services),
        session_factory=async_session_factory,
        # 演示档不做共享：每调用新建 Services/adapter（无跨调用 TTL 缓存、
        # registry 每 chat 重建）——dev-only 演示通道可接受，HTTP 档才是
        # 进程级共享（main.py 单例）。
        services_factory=lambda db: Services(settings=settings, db=db),
        orchestrator_factory=_orchestrator_factory,
        channel="mcp-stdio",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
