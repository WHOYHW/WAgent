"""LLM 适配器包（编排层子任务 3）。

实现契约见 ``docs/项目文档/05-LLM适配器设计.md`` 与
``app/orchestrator.py`` 协议区（已冻结的 ``LLMClient`` Protocol）。
"""

from .adapter import ArkLLMClient, LLMNotConfiguredError, LLMStreamError

__all__ = ["ArkLLMClient", "LLMNotConfiguredError", "LLMStreamError"]
