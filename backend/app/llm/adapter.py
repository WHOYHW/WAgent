"""LLM 适配器（M2 子任务 3）：OpenAI 兼容流式响应 → ``LLMClient`` Protocol。

接火山方舟（豆包系列，``https://ark.cn-beijing.volces.com/api/v3``），
协议与实现契约见 ``docs/项目文档/05-LLM适配器设计.md``。本模块只做
"厂商流式分片 → ``LLMChunk`` 块序列"的翻译，不包含任何编排语义——
重试/兜底/降级全部由编排层（F3/F6）统一决定（设计原则 P2 依赖倒置）。

设计要点：
- **delta 透传**：``choices[0].delta.content`` 逐块 yield；``None``/空串跳过
  （心跳包 / role-only 首块）。
- **tool_calls 分片聚合**：OpenAI 流式协议中工具调用按 ``index`` 分片到达
  （首片带 id/name，后续片只有 arguments 增量），本模块按 index 分桶拼接，
  流结束 yield **恰好一个**聚合末块——履行冻结协议"tool_calls 只出现在
  最后一个块"的约定。
- **reasoning 双保险**：豆包 seed 系列为思考模型，默认经 ``extra_body``
  服务端关 thinking（计费且推高首字延迟）；传输层对
  ``delta.reasoning_content`` 有意不读取不透传（思考过程对用户不可见，R3），
  留作参数失效/换模型时的纵深防御。
- **不做自动重试**：SDK ``max_retries=0``——已 yield 的 token 无法回滚，
  重试会造成重复输出；失败统一包装 :class:`LLMStreamError` 抛出，由编排层
  F6（首请求失败 → ``llm_unavailable``）或 F3（摘要失败 → 降级）兜底。
- **懒校验**：构造零副作用；配置缺失在首次 ``stream()`` 时抛
  :class:`LLMNotConfiguredError`（镜像 ``adapters.base`` 的
  ``SourceNotConfiguredError`` 语义）——LLM 未配置时应用仍可启动，
  红线拦截（F2）等 LLM 零参与路径不受影响（P5）。

异常映射链（设计文档 §F5）：
``LLMStreamError`` → 编排层首请求失败时置 ``LLMStreamFailure`` →
``error(llm_unavailable)`` 终态。编排层与子任务 4 不 import ``openai``
的任何异常类型（P2 闭环）。
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

from openai import AsyncOpenAI, Timeout

from ..config import Settings
from ..config import settings as default_settings
from ..logging_util import get_logger
from ..orchestrator import LLMChunk, ToolCallRequest

logger = get_logger("llm")


class LLMNotConfiguredError(Exception):
    """LLM 未配置（``llm_api_base`` / ``llm_api_key`` / ``llm_model`` 缺失）。

    懒抛：首次 ``stream()`` 时才校验。镜像 ``adapters.base.SourceNotConfiguredError``
    的语义——未配置是部署问题而非代码缺陷，应用启动不因此失败。
    """


class LLMStreamError(Exception):
    """LLM 流式请求失败（SDK 异常的统一包装，``__cause__`` 保留原始异常）。

    消息含原始异常类名，便于排障；不包含请求内容与凭证（R3 纪律）。
    """


class _ToolCallAcc:
    """单个工具调用的分片聚合器（按 ``tool_calls[].index`` 分桶）。"""

    __slots__ = ("id", "name", "arguments")

    def __init__(self) -> None:
        self.id: str | None = None
        self.name: str = ""
        self.arguments: str = ""


class ArkLLMClient:
    """火山方舟 LLM 适配器：结构化匹配 ``orchestrator.LLMClient``（不继承）。

    生命周期：应用级单例（连接池复用），应用 shutdown 时 ``aclose()``；
    编排层仍是请求级实例，二者经 Protocol 解耦。构造接受可选 ``client``
    注入（单测用 ``FakeOpenAI``，不触网络）。
    """

    def __init__(
        self,
        *,
        client: AsyncOpenAI | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self.settings = settings or default_settings

    @property
    def is_configured(self) -> bool:
        """三项配置齐全（供子任务 4 在 startup 自检打 warning）。"""
        s = self.settings
        return bool(s.llm_api_base and s.llm_api_key and s.llm_model)

    # ---- 唯一公开方法（Protocol 实现）----

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        client = await self._get_client()
        model = self._require_model()

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            # 豆包思考模型开关（方舟非标准参数，经 extra_body 透传）
            "extra_body": {
                "thinking": {
                    "type": "enabled" if self.settings.llm_thinking_enabled else "disabled"
                }
            },
        }
        if tools:
            kwargs["tools"] = tools  # 空表/None 都不带（方舟对 [] 的行为未验证）
        if self.settings.llm_stream_include_usage:
            kwargs["stream_options"] = {"include_usage": True}

        acc: dict[int, _ToolCallAcc] = {}  # index → 聚合器（dict 保插入序）
        finish: str | None = None
        n_chunks = 0
        started = time.monotonic()
        try:
            stream = await client.chat.completions.create(**kwargs)
            async with stream:  # 任意退出路径（含消费者弃流）都关底层流
                async for chunk in stream:
                    if not chunk.choices:  # usage 尾块 / keep-alive，跳过
                        continue
                    choice = chunk.choices[0]  # n=1，取首个
                    finish = choice.finish_reason or finish
                    delta = choice.delta
                    content = getattr(delta, "content", None)
                    # delta.reasoning_content 有意不读取（思考过程对用户不可见）
                    if content:
                        yield LLMChunk(delta=content)
                        n_chunks += 1
                    for tc in delta.tool_calls or ():
                        index = tc.index if tc.index is not None else 0
                        a = acc.setdefault(index, _ToolCallAcc())
                        if tc.id:
                            a.id = tc.id  # 晚到的 id 覆盖（部分端点非首片携带）
                        fn = tc.function
                        if fn is not None:  # id-only 分片没有 function
                            if fn.name:
                                a.name += fn.name  # name 罕见分片，拼接安全
                            if fn.arguments:
                                a.arguments += fn.arguments
        except Exception as exc:
            raise LLMStreamError(
                f"ark_stream_failed: {exc.__class__.__name__}: {exc}"
            ) from exc

        # ---- 终块：恰好一个，聚合态 ----
        if acc:
            calls = [
                ToolCallRequest(
                    id=a.id or f"call_{idx}",  # id 缺失时合成，保配对协议不断裂
                    name=a.name,
                    arguments=a.arguments or "{}",  # 空参归一，免编排层白耗一轮解析失败回填
                )
                for idx, a in sorted(acc.items())  # 按 index 排序，输出确定
            ]
            final_reason = finish or "tool_calls"
            yield LLMChunk(tool_calls=calls, finish_reason=final_reason)
        else:
            final_reason = finish or "stop"
            yield LLMChunk(finish_reason=final_reason)

        # R3 纪律：只记元数据（模型/块数/结束原因/耗时），不记内容与参数
        logger.info(
            "llm_stream_done",
            model=model,
            chunks=n_chunks,
            tool_calls=len(acc),
            finish_reason=final_reason,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def aclose(self) -> None:
        """关闭懒创建的底层客户端（应用 shutdown 时调用；幂等）。"""
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---- 私有 ----

    async def _get_client(self) -> AsyncOpenAI:
        """懒创建 SDK 客户端；未配置抛 :class:`LLMNotConfiguredError`。"""
        if self._client is None:
            s = self.settings
            if not (s.llm_api_base and s.llm_api_key):
                raise LLMNotConfiguredError("llm_api_base / llm_api_key 未配置")
            self._client = AsyncOpenAI(
                base_url=s.llm_api_base,
                api_key=s.llm_api_key,
                # read 超时是"相邻字节间隔"上限而非整流时长（思考模型持续吐
                # 分片就不会触发）；connect 单独收紧到 5s，尽早暴露网络故障
                timeout=Timeout(s.orchestrator_llm_timeout_seconds, connect=5.0),
                max_retries=0,  # 已 yield 的 token 无法回滚，重试语义归编排层
            )
        return self._client

    def _require_model(self) -> str:
        """model 是请求参数而非客户端参数：注入 client 的测试也必须配置。"""
        if not self.settings.llm_model:
            raise LLMNotConfiguredError("llm_model 未配置")
        return self.settings.llm_model
