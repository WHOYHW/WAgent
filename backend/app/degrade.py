"""C4 降级语义规范（§3.4）：五级状态码 + 文案模板 + 重试策略。

- ``DegradeStatus`` 从契约层（schemas.meta）重导出，保持单一事实来源。
- 文案模板集中在 ``templates/degrade.yaml``，按 ``degrade_status`` + ``tool`` 索引，
  禁止在工具内硬编码文案（R4.2）；按文件 mtime 热更新，改 yaml 即时生效（AC4.2）。
- 重试策略（R4.3）统一在 ``adapters.base`` 实现：读源系统指数退避 2 次；写操作 0 次。
- 无静默失败原则（R4.4）：工具调用必须有明确 ``degrade_status``，经 ``build_tool_meta``
  写入每个工具出参的 ``meta.degrade_status``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schemas.meta import DegradeStatus, ToolMeta

__all__ = ["DegradeStatus", "render_degrade_text", "build_tool_meta", "compute_is_stale"]

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "degrade.yaml"


def _load_templates() -> dict[str, Any]:
    """按 mtime 缓存模板：文件未变直接命中，修改后自动重载（AC4.2 热更新）。"""
    try:
        mtime = _TEMPLATE_PATH.stat().st_mtime
    except OSError:
        return {"degrade_templates": {}}
    cached = _template_cache.get("data")
    if cached is not None and _template_cache.get("mtime") == mtime:
        return cached
    with open(_TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {"degrade_templates": {}}
    _template_cache["mtime"] = mtime
    _template_cache["data"] = raw
    return raw


_template_cache: dict[str, Any] = {}


def _resolve_template(status: DegradeStatus, tool: str) -> str:
    templates = _load_templates().get("degrade_templates", {})
    by_status = templates.get(status.value, {})
    if isinstance(by_status, str):
        return by_status
    return by_status.get(tool, by_status.get("_default", ""))


def render_degrade_text(status: DegradeStatus, tool: str, **variables: Any) -> str:
    """从模板库渲染用户可见降级文案（R4.2，改 yaml 不发版）。

    ``variables`` 用于填充占位符，如 ``synced_at``、``reason``、``source_name``。
    未命中的状态返回空串（表示无需额外提示）。
    """
    template = _resolve_template(status, tool)
    if not template:
        return ""
    # 兼容 Python str.format 占位符；缺省变量时保留占位符不抛异常。
    try:
        return template.format(**variables)
    except (KeyError, IndexError, ValueError):
        return template


def build_tool_meta(
    source: Any,
    synced_at: Any,
    degrade_status: DegradeStatus,
    ttl_seconds: int,
    *,
    now: Any = None,
) -> ToolMeta:
    """根据适配器返回构造 ToolMeta，并计算 is_stale（C5 §3.5.2 R5.1）。

    该函数是「适配器输出 → 工具出参 meta」的唯一边界，is_stale 判定规则在此固化：
    ``is_stale = True`` 当且仅当 ``degrade_status == stale_cache``，或
    ``source == cache 且 now - synced_at > ttl``。
    """
    from datetime import datetime, timezone

    from .schemas.meta import ToolSource

    now = now or datetime.now(timezone.utc)
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)

    is_stale = compute_is_stale(
        source=source,
        synced_at=synced_at,
        ttl_seconds=ttl_seconds,
        degrade_status=degrade_status,
        now=now,
    )

    stale_reason: str | None = None
    if degrade_status == DegradeStatus.STALE_CACHE:
        stale_reason = "source_timeout"
    elif source == ToolSource.CACHE and is_stale:
        stale_reason = "cache_expired"

    return ToolMeta(
        source=ToolSource(source),
        synced_at=synced_at,
        degrade_status=degrade_status,
        is_stale=is_stale,
        stale_reason=stale_reason,
    )


def compute_is_stale(
    *,
    source: Any,
    synced_at: Any,
    ttl_seconds: int,
    degrade_status: DegradeStatus,
    now: Any = None,
) -> bool:
    """is_stale 判定规则（§3.5.2 R5.1），纯函数便于单测。"""
    from datetime import datetime, timezone

    from .schemas.meta import ToolSource

    if degrade_status == DegradeStatus.STALE_CACHE:
        return True
    now = now or datetime.now(timezone.utc)
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    if source == ToolSource.CACHE and (now - synced_at).total_seconds() > ttl_seconds:
        return True
    return False
