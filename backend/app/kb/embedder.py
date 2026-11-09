"""embedding 客户端：协议 + 确定性桩 + 方舟真机客户端（文档 18 §四 v1.4，K3 真机档）。

- ``EmbeddingClient``：冻结协议（duck typing，对齐 LLMClient 惯例，无 ABC 继承），
  单方法 ``embed_texts``——查询向量化复用同一接口（``embed_texts([query])[0]``），
  不设 ``embed_query`` 双入口（重用单一接口避免协议膨胀，LLM 适配器已验证的纪律）；
- ``FakeEmbedder``：确定性假实现（测试/黄金集专用，零网络零依赖）；
- ``ArkEmbeddingClient``：方舟真机客户端（AsyncOpenAI ``/embeddings`` 端点），
  ingest 批量向量化与检索查询向量化共用同一实现（v1.4，K3 真机档）。

FakeEmbedder 规约（AC-18b 可断言性的前提）：字符 sha256 散列桶——文本每个
非空白字符散列到固定 64 维桶计数 +1，L2 归一化。禁用内置 ``hash()``：
PYTHONHASHSEED 随机化会破坏确定性。共享字符越多余弦越高 →"转专业条件"
与转专业条款块的相似度可预期，检索结果可逐条断言。

ArkEmbeddingClient 镜像 ArkLLMClient 的四条纪律（文档 18 v1.4 ③④⑤）：
构造零副作用懒校验（未配置是部署问题，启动不失败）、max_retries=0
（重试归调用方）、日志只记元数据不记文本与凭证、应用级单例 + 幂等 aclose()。
异常类本地定义镜像 ``LLMNotConfiguredError`` 语义而非复用——分层契约
app.kb 位于 app.llm 之下，禁止反向 import。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Protocol

from openai import AsyncOpenAI, Timeout

from ..config import Settings
from ..config import settings as default_settings
from ..logging_util import get_logger

#: FakeEmbedder 输出维度（测试语料字符集远小于 64，桶冲突可控）
FAKE_EMBED_DIM = 64

#: 散列前剥离空白：语义相似度不应被空格/换行差异污染
_WHITESPACE_RE = re.compile(r"\s+")

logger = get_logger("kb")


class EmbeddingClient(Protocol):
    """embedding 客户端冻结协议。输入文本列表，输出等长 float 向量列表。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    """确定性假 embedder：字符散列桶 + L2 归一化（零网络，测试专用）。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * FAKE_EMBED_DIM
        for ch in _WHITESPACE_RE.sub("", text):
            digest = hashlib.sha256(ch.encode("utf-8")).digest()
            vec[int.from_bytes(digest[:4], "big") % FAKE_EMBED_DIM] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0.0:  # 空文本 → 零向量（检索侧余弦计算有零向量守卫）
            return vec
        return [v / norm for v in vec]


class EmbeddingNotConfiguredError(Exception):
    """embedding 未配置（``llm_api_base`` / ``llm_api_key`` / ``kb_embedding_model`` 缺失）。

    懒抛：首次 ``embed_texts()`` 时才校验。镜像 ``LLMNotConfiguredError``
    的语义——未配置是部署问题而非代码缺陷，应用启动不因此失败。
    本地定义而非复用 app.llm 的异常类（分层契约：app.kb 不 import app.llm）。
    """


class EmbeddingCallError(Exception):
    """embedding 调用失败（SDK 异常的统一包装，``__cause__`` 保留原始异常）。

    消息含原始异常类名便于排障；不包含请求文本与凭证（R3 纪律）。
    """


class ArkEmbeddingClient:
    """方舟 embedding 适配器：结构化匹配 ``EmbeddingClient``（不继承）。

    生命周期：应用级单例（连接池复用），应用 shutdown 时 ``aclose()``；
    构造接受可选 ``client`` 注入（单测 FakeOpenAI，不触网络）。
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
        """三项配置齐全（装配层自检打 warning 用，与 ArkLLMClient.is_configured 同惯例）。"""
        s = self.settings
        return bool(s.llm_api_base and s.llm_api_key and s.kb_embedding_model)

    # ---- 唯一公开方法（Protocol 实现）----

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：按 ``kb_embedding_batch_size`` 切批请求，结果跨批按请求序拼接。"""
        if not texts:  # 空入参零网络直接返回（ingest 空页切块可能为空）
            return []
        client = await self._get_client()
        model = self._require_model()
        batch_size = max(1, self.settings.kb_embedding_batch_size)

        out: list[list[float]] = []
        n_batches = 0
        started = time.monotonic()
        try:
            for lo in range(0, len(texts), batch_size):
                batch = texts[lo : lo + batch_size]
                resp = await client.embeddings.create(model=model, input=batch)
                out.extend(self._parse_response(resp, len(batch)))
                n_batches += 1
        except EmbeddingCallError:
            raise  # 解析/校验类失败已是规范形态，直接上抛
        except Exception as exc:
            raise EmbeddingCallError(
                f"ark_embedding_failed: {exc.__class__.__name__}: {exc}"
            ) from exc

        # R3 纪律：只记元数据（模型/文本数/批数/维度/耗时），不记文本与凭证
        logger.info(
            "embedding_done",
            model=model,
            texts=len(texts),
            batches=n_batches,
            dim=len(out[0]) if out else 0,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return out

    async def aclose(self) -> None:
        """关闭懒创建的底层客户端（应用 shutdown 时调用；幂等）。"""
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---- 私有 ----

    async def _get_client(self) -> AsyncOpenAI:
        """懒创建 SDK 客户端；未配置抛 :class:`EmbeddingNotConfiguredError`。"""
        if self._client is None:
            s = self.settings
            if not (s.llm_api_base and s.llm_api_key):
                raise EmbeddingNotConfiguredError("llm_api_base / llm_api_key 未配置")
            self._client = AsyncOpenAI(
                base_url=s.llm_api_base,
                api_key=s.llm_api_key,
                # embedding 批量是短请求，read 超时复用编排层 LLM 超时口径即可
                timeout=Timeout(s.orchestrator_llm_timeout_seconds, connect=5.0),
                max_retries=0,  # 重试语义归调用方（ingest 编排层），适配器禁自动重试
            )
        return self._client

    def _require_model(self) -> str:
        """model 是请求参数而非客户端参数：注入 client 的测试也必须配置。"""
        if not self.settings.kb_embedding_model:
            raise EmbeddingNotConfiguredError("kb_embedding_model 未配置")
        return self.settings.kb_embedding_model

    def _parse_response(self, resp: Any, expected_count: int) -> list[list[float]]:
        """响应解析：index 重排 + 连续性校验 + 维度一致性校验（防脏向量入库）。"""
        data = list(resp.data)
        if len(data) != expected_count:
            raise EmbeddingCallError(
                f"ark_embedding_failed: 返回条数 {len(data)} != 请求数 {expected_count}"
            )
        rows = sorted(data, key=lambda item: item.index)
        expect_dim = self.settings.kb_embedding_dim
        vecs: list[list[float]] = []
        resp_dim: int | None = None
        for i, row in enumerate(rows):
            if row.index != i:
                raise EmbeddingCallError(
                    f"ark_embedding_failed: index 不连续（位置 {i} 得到 index {row.index}）"
                )
            vec = list(row.embedding)
            if resp_dim is None:
                resp_dim = len(vec)
            elif len(vec) != resp_dim:
                raise EmbeddingCallError(
                    f"ark_embedding_failed: 批内维度不一致 {len(vec)} != {resp_dim}"
                )
            if expect_dim is not None and len(vec) != expect_dim:
                raise EmbeddingCallError(
                    f"ark_embedding_failed: 维度 {len(vec)} != 配置 kb_embedding_dim={expect_dim}"
                )
            vecs.append(vec)
        return vecs


__all__ = [
    "FAKE_EMBED_DIM",
    "ArkEmbeddingClient",
    "EmbeddingCallError",
    "EmbeddingClient",
    "EmbeddingNotConfiguredError",
    "FakeEmbedder",
]
