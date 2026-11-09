"""Embedding 适配器单元测试（文档 18 v1.4 · K3 真机档）。

全程 ``FakeOpenAI`` 桩注入（镜像 test_llm_adapter 的 DI 模式），不触网络；
SDK 响应形态用 ``SimpleNamespace`` 树伪造——测的是适配器的批量/重排/校验
逻辑而非 pydantic。真端点由 ``demo_embedder --live`` 冒烟覆盖（测试分层：
单测协议形状 / 联调真端点）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.kb import (
    FAKE_EMBED_DIM,
    ArkEmbeddingClient,
    EmbeddingCallError,
    EmbeddingNotConfiguredError,
    FakeEmbedder,
)

# ---- FakeOpenAI 桩区（镜像 test_llm_adapter）----


class FakeEmbeddingsAPI:
    """scripts：每次 create() 弹出的向量列表（或首抛异常）；记录全部 kwargs。

    script 项为 SimpleNamespace 时原样返回（自定义响应形态：乱序 index 等）。
    """

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.scripts.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(
            data=[SimpleNamespace(index=i, embedding=v) for i, v in enumerate(item)]
        )


class FakeOpenAI:
    def __init__(self, scripts):
        self.embeddings = FakeEmbeddingsAPI(scripts)
        self.close_count = 0

    async def close(self):
        self.close_count += 1


# ---- 构造器 ----


def vec(dim: int = 4, seed: int = 0) -> list[float]:
    """确定性伪向量（不同 seed 值不同，同 seed 值相同）。"""
    return [float((seed + i) % 7 + 1) for i in range(dim)]


def make_adapter(fake: FakeOpenAI, **cfg) -> ArkEmbeddingClient:
    return ArkEmbeddingClient(
        client=fake,
        settings=Settings(
            llm_api_base="https://ark.test/api/v3",
            llm_api_key="k-test",
            kb_embedding_model="doubao-embed-test",
            **cfg,
        ),
    )


# ---- 1 纯透传：单批一次请求、原序一一对应 ----


async def test_passthrough_single_batch():
    fake = FakeOpenAI([[vec(seed=0), vec(seed=1), vec(seed=2)]])
    texts = ["转专业条件", "图书馆开放时间", "学生证补办"]
    out = await make_adapter(fake).embed_texts(texts)
    assert out == [vec(seed=0), vec(seed=1), vec(seed=2)]  # 原序一一对应
    call = fake.embeddings.calls[0]
    assert call["model"] == "doubao-embed-test"  # 用 kb_embedding_model 而非 llm_model
    assert call["input"] == texts  # 单批一次请求全量文本


# ---- 2 批量化：切批多次请求、跨批拼接保序 ----


async def test_batching_splits_and_preserves_order():
    fake = FakeOpenAI([[vec(seed=0), vec(seed=1)], [vec(seed=2), vec(seed=3)], [vec(seed=4)]])
    texts = [f"测试文本{i}" for i in range(5)]
    out = await make_adapter(fake, kb_embedding_batch_size=2).embed_texts(texts)
    assert out == [vec(seed=i) for i in range(5)]  # 跨批拼接保序
    assert len(fake.embeddings.calls) == 3  # 5 条 / 每批 2 → 3 次请求
    assert fake.embeddings.calls[0]["input"] == texts[:2]
    assert fake.embeddings.calls[1]["input"] == texts[2:4]
    assert fake.embeddings.calls[2]["input"] == texts[4:]


# ---- 3 空入参零网络 ----


async def test_empty_input_no_network():
    fake = FakeOpenAI([])
    out = await make_adapter(fake).embed_texts([])
    assert out == []
    assert fake.embeddings.calls == []  # 不发任何请求


# ---- 4 未配置懒抛（启动不失败）----


async def test_not_configured_lazy_raise():
    # 构造不抛（无注入 client 也允许建实例）——校验懒到首次调用
    adapter = ArkEmbeddingClient(
        settings=Settings(llm_api_base=None, llm_api_key=None, kb_embedding_model="")
    )
    assert adapter.is_configured is False
    with pytest.raises(EmbeddingNotConfiguredError):
        await adapter.embed_texts(["x"])

    # 仅缺 model：注入了 client 也必须抛（model 是请求参数）
    adapter2 = ArkEmbeddingClient(
        client=FakeOpenAI([]),
        settings=Settings(llm_api_base="https://x", llm_api_key="k", kb_embedding_model=""),
    )
    assert adapter2.is_configured is False
    with pytest.raises(EmbeddingNotConfiguredError):
        await adapter2.embed_texts(["x"])


# ---- 5 响应 index 重排 + 连续性校验 ----


async def test_response_reordered_by_index():
    resp = SimpleNamespace(
        data=[
            SimpleNamespace(index=2, embedding=vec(seed=2)),
            SimpleNamespace(index=0, embedding=vec(seed=0)),
            SimpleNamespace(index=1, embedding=vec(seed=1)),
        ]
    )
    fake = FakeOpenAI([resp])
    out = await make_adapter(fake).embed_texts(["a", "b", "c"])
    assert out == [vec(seed=0), vec(seed=1), vec(seed=2)]  # 按 index 重排


async def test_index_gap_raises():
    # 条数对但 index 不连续（0,0,2）→ 防错位静默入库
    resp = SimpleNamespace(
        data=[
            SimpleNamespace(index=0, embedding=vec(seed=0)),
            SimpleNamespace(index=0, embedding=vec(seed=1)),
            SimpleNamespace(index=2, embedding=vec(seed=2)),
        ]
    )
    fake = FakeOpenAI([resp])
    with pytest.raises(EmbeddingCallError, match="index 不连续"):
        await make_adapter(fake).embed_texts(["a", "b", "c"])


# ---- 6 条数不一致 ----


async def test_count_mismatch_raises():
    fake = FakeOpenAI([[vec(seed=0)]])  # 请求 2 条返回 1 条
    with pytest.raises(EmbeddingCallError, match="返回条数"):
        await make_adapter(fake).embed_texts(["a", "b"])


# ---- 7 维度一致性校验 ----


async def test_dim_mismatch_configured():
    fake = FakeOpenAI([[vec(dim=3, seed=0), vec(dim=3, seed=1)]])
    with pytest.raises(EmbeddingCallError, match="kb_embedding_dim=4"):
        await make_adapter(fake, kb_embedding_dim=4).embed_texts(["a", "b"])


async def test_dim_inconsistent_within_batch():
    fake = FakeOpenAI([[vec(dim=4, seed=0), vec(dim=3, seed=1)]])
    with pytest.raises(EmbeddingCallError, match="批内维度不一致"):
        await make_adapter(fake).embed_texts(["a", "b"])  # 未配置维度也拦批内不一致


async def test_dim_match_configured_passes():
    fake = FakeOpenAI([[vec(dim=4, seed=0), vec(dim=4, seed=1)]])
    out = await make_adapter(fake, kb_embedding_dim=4).embed_texts(["a", "b"])
    assert out == [vec(seed=0), vec(seed=1)]


# ---- 8 SDK 异常统一包装（含批中失败）----


async def test_create_exception_wrapped():
    fake = FakeOpenAI([RuntimeError("connect refused")])
    with pytest.raises(EmbeddingCallError) as ei:
        await make_adapter(fake).embed_texts(["a"])
    assert isinstance(ei.value.__cause__, RuntimeError)  # 因果链保留
    assert "RuntimeError" in str(ei.value)  # 消息含原始类名


async def test_midbatch_failure_wrapped():
    fake = FakeOpenAI([[vec(seed=0), vec(seed=1)], RuntimeError("batch 2 down")])
    with pytest.raises(EmbeddingCallError):
        await make_adapter(fake, kb_embedding_batch_size=2).embed_texts(["a", "b", "c"])


# ---- 9 懒创建路径的 SDK 构造参数（连接池复用）----


async def test_client_constructor_params(monkeypatch):
    import app.kb.embedder as embedder_mod

    recorded: dict = {}
    created: list = []

    class RecorderOpenAI:
        def __init__(self, **kw):
            recorded.update(kw)
            created.append(self)
            self.embeddings = FakeEmbeddingsAPI([[vec(seed=0)], [vec(seed=1)]])

        async def close(self):
            pass

    monkeypatch.setattr(embedder_mod, "AsyncOpenAI", RecorderOpenAI)
    client = ArkEmbeddingClient(
        settings=Settings(
            llm_api_base="https://ark.test/api/v3",
            llm_api_key="k-test",
            kb_embedding_model="m-test",
        )
    )
    await client.embed_texts(["x"])
    await client.embed_texts(["y"])  # 第二次调用复用同一客户端
    assert recorded["base_url"] == "https://ark.test/api/v3"
    assert recorded["api_key"] == "k-test"
    assert recorded["max_retries"] == 0  # SDK 默认重试被显式清零（重试归调用方）
    assert float(recorded["timeout"].read) == 15.0  # settings 默认超时口径
    assert float(recorded["timeout"].connect) == 5.0  # connect 单独收紧
    assert len(created) == 1  # 连接池复用，不重复建客户端


# ---- 10 aclose 幂等 ----


async def test_aclose_idempotent():
    fake = FakeOpenAI([])
    adapter = make_adapter(fake)
    await adapter.aclose()
    assert fake.close_count == 1
    assert adapter._client is None
    await adapter.aclose()  # 幂等，二次不抛不再关
    assert fake.close_count == 1


# ---- 11 Protocol 兼容：FakeEmbedder 仍满足同一接口（回归锚）----


async def test_fake_embedder_contract():
    out = await FakeEmbedder().embed_texts(["转专业条件", ""])
    assert len(out) == 2
    assert all(len(v) == FAKE_EMBED_DIM for v in out)
    assert all(v == 0.0 for v in out[1])  # 空文本 → 零向量
