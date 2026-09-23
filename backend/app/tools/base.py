"""工具层基类与编排契约（C2）。

- :class:`ToolSpec`：Function Calling Schema（name/description/parameters/strict）
  + 前置/后置条件 + 版本号，可经 ``to_openai_function()`` 直接作为 LLM 的 tools 入参。
- :class:`Tool`：单一职责的异步执行体，出入参强类型。
- :class:`ToolRegistry`：注册与调度，含统一脱敏审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import EduAdapter, LibAdapter
from ..cache import TTLCache
from ..config import Settings, settings as default_settings
from ..logging_util import get_logger, write_audit
from ..security import mask_student_id

logger = get_logger("tools")


class ToolError(Exception):
    """工具层异常基类。"""


class PreconditionError(ToolError):
    """前置条件违反（→ 参数错误/422）。"""


class PostconditionError(ToolError):
    """后置条件未满足（内部一致性错误）。"""


@dataclass(frozen=True)
class ToolSpec:
    """Function Calling Schema + 前置/后置条件（§3.2.2 R2.1/R2.4）。"""

    name: str
    description: str
    params_model: type[BaseModel]
    result_model: type[BaseModel]
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    strict: bool = False
    write: bool = False
    boundary: str = "AI"  # AI / AI+H
    schema_version: str = "1.0"

    @property
    def parameters(self) -> dict:
        """JSON Schema 由 Pydantic 导出（R1.1，禁止手写）。"""
        return self.params_model.model_json_schema()

    def to_openai_function(self) -> dict:
        """OpenAI Function Calling 的 tools 元素（R2.1）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
                "strict": self.strict,
            },
        }

    def to_dict(self) -> dict:
        """完整 Schema（含前置/后置条件与版本），供文档化/评测使用。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": self.strict,
            "schema_version": self.schema_version,
            "write": self.write,
            "boundary": self.boundary,
            "preconditions": list(self.preconditions),
            "postconditions": list(self.postconditions),
        }


@dataclass
class Services:
    """工具运行所需的依赖束（低耦合：工具只依赖接口，不依赖构造）。"""

    cache: TTLCache = field(default_factory=TTLCache)
    settings: Settings = field(default_factory=lambda: default_settings)
    edu: EduAdapter | None = None
    lib: LibAdapter | None = None
    db: AsyncSession | None = None
    # 续借幂等与频控（会话级，生产应迁移至 Redis/DB 唯一约束）
    idempotency: dict[str, BaseModel] = field(default_factory=dict)
    renew_session_count: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.edu is None:
            self.edu = EduAdapter(self.cache, settings=self.settings)
        if self.lib is None:
            self.lib = LibAdapter(self.cache, settings=self.settings)


@dataclass
class ToolContext:
    """工具调用上下文。``student_id`` 由会话身份层注入（禁止作为用户输入）。"""

    student_id: str
    services: Services
    session_id: str | None = None
    channel: str | None = None


def _mask_args(args: dict[str, Any]) -> dict[str, Any]:
    """参数脱敏摘要（不含成绩/身份证明文，R3.4）。"""
    digest: dict[str, Any] = {}
    for key, value in args.items():
        if key == "student_id" and value:
            digest[key] = mask_student_id(str(value))
        elif key == "barcodes":
            digest[key] = f"<{len(value)} items>" if isinstance(value, list) else value
        elif key in ("idempotency_key",):
            digest[key] = f"<hash:{str(value)[:8]}…>"
        else:
            digest[key] = value
    return digest


class Tool:
    """工具基类。子类声明 :attr:`spec` 并实现 :meth:`execute`。"""

    spec: ClassVar[ToolSpec]

    def validate(self, ctx: ToolContext, params: BaseModel) -> None:
        """前置条件校验（默认通过）。违反时抛 :class:`PreconditionError`。"""

    async def execute(self, ctx: ToolContext, params: BaseModel) -> BaseModel:
        raise NotImplementedError

    async def call(self, ctx: ToolContext, params: BaseModel) -> BaseModel:
        """校验前置条件后执行。"""
        self.validate(ctx, params)
        return await self.execute(ctx, params)


class ToolRegistry:
    def __init__(self, services: Services | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.services = services or Services()

    def register(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool

    def spec_names(self) -> list[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"未知工具：{name}") from exc

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def openai_tools(self) -> list[dict]:
        """导出全部工具为 OpenAI Function Calling tools 参数（AC2.1）。"""
        return [t.spec.to_openai_function() for t in self._tools.values()]

    async def dispatch(
        self,
        name: str,
        ctx: ToolContext,
        params: BaseModel | dict[str, Any],
    ) -> BaseModel:
        """统一调度：类型校验 → 前置条件 → 执行 → 脱敏审计。"""
        tool = self.get(name)
        if isinstance(params, dict):
            params = tool.spec.params_model.model_validate(params)
        start = datetime.now(timezone.utc)
        result_value: str = "ok"
        try:
            result = await tool.call(ctx, params)
        except ToolError:
            result_value = "error"
            raise
        except Exception:
            result_value = "error"
            logger.exception("tool_dispatch_failed", tool=name)
            raise
        finally:
            latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            # mode="json"：date/datetime/enum 序列化为 JSON 兼容类型（审计列是 JSON）。
            args_digest = _mask_args(params.model_dump(mode="json"))
            if ctx.services.db is not None:
                try:
                    await write_audit(
                        ctx.services.db,
                        action="tool_call",
                        student_id=ctx.student_id,
                        session_id=ctx.session_id,
                        tool_name=name,
                        args_digest=args_digest,
                        result=result_value,
                        latency_ms=latency,
                        channel=ctx.channel,
                    )
                except Exception:  # 审计失败不阻断主流程
                    logger.exception("audit_write_failed", tool=name)
        return result
