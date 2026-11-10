"""MCP 身份层（文档 19 §二 D1）：JWT 传输层注入 + contextvar。

- :data:`current_student`：当前请求的 student_id（``str | None``），工具
  wrapper 从这里取身份——工具参数层永远不出现 ``student_id``（约定③在
  MCP 语境的落地）。
- :class:`StudentIdentityMiddleware`：纯 ASGI 中间件，仅用于包裹 MCP 子
  应用（挂载于 /mcp）——解 ``Authorization: Bearer <JWT>``（复用
  ``security.decode_token`` 既有校验）→ set contextvar；无/坏令牌直接 401，
  请求不进 MCP 机制（tools/list 与 tools/call 都要求有效身份）。

用 contextvar 而不是 MCP SDK 的 Context 对象取身份，是刻意与 SDK 内部
API 解耦——SDK 版本升级不动这块（文档 19 §三）。
"""

from __future__ import annotations

import json
from contextvars import ContextVar

import jwt
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from ..security import decode_token

# 当前请求身份。HTTP 档由中间件按请求 set/reset（finally 保证复位）；
# stdio 演示入口在启动前显式 set（MCP_DEMO_STUDENT_ID）。
current_student: ContextVar[str | None] = ContextVar("current_student", default=None)


class StudentIdentityMiddleware:
    """主 app 级 ASGI 中间件：仅 /mcp 前缀强制 Bearer JWT 身份，其余路径纯透传。

    - /mcp 前缀：解 ``Authorization: Bearer <JWT>``（复用
      ``security.decode_token`` 既有校验）→ set contextvar；无/坏令牌直接
      401，请求不进 MCP 机制（tools/list 与 tools/call 都要求有效身份）。
    - 其余路径（含 REST /agent/*）：身份由各自的 Depends 提供，本中间件
      不干预——"middleware 挂在 /mcp 路径前缀上"（文档 19 §二 D1）。
    """

    def __init__(self, app: ASGIApp, mcp_prefix: str = "/mcp") -> None:
        self.app = app
        self._prefix = mcp_prefix.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path != self._prefix and not path.startswith(self._prefix + "/"):
            await self.app(scope, receive, send)
            return
        if path == self._prefix:
            # Starlette Mount 的匹配要求前缀后至少一个字符（"/mcp/{path:path}"），
            # 精确 /mcp 不命中 → redirect_slashes 307。此处归一为 /mcp/ 消除重定向
            # 依赖（官方客户端 follow_redirects=True 会跟随，第三方客户端未必）。
            scope = {**scope, "path": self._prefix + "/"}
        student_id: str | None = None
        detail = "缺少会话令牌"
        auth = Headers(scope=scope).get("authorization")
        if auth and auth[:7].lower() == "bearer ":
            try:
                payload = decode_token(auth[7:].strip())
                student_id = payload.get("sub")
                if not student_id:
                    student_id = None
                    detail = "令牌缺少主体身份"
            except jwt.InvalidTokenError:
                detail = "会话令牌无效或已过期"
        if student_id is None:
            # 无/坏令牌直接 401，不进 MCP 机制（错误文案与 REST 面 get_current_student 同语义）
            await _send_401(send, detail)
            return

        token = current_student.set(student_id)
        try:
            await self.app(scope, receive, send)
        finally:
            current_student.reset(token)


async def _send_401(send: Send, detail: str) -> None:
    """纯 ASGI 401 响应（错误结构对齐 FastAPI HTTPException 的 {"detail": ...}）。"""
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
