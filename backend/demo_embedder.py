"""Embedding 适配器 Demo（文档 18 v1.4 · K3 真机档 · 任务卡 AC-18c 冒烟入口）。

    python -m demo_embedder           # 离线模式（默认，FakeEmbedder 桩，不触网）
    python -m demo_embedder --live    # 联调模式：真 ArkEmbeddingClient

live 冒烟判据（AC-18c，任务卡编号；与文档 18 §七 AC-18c 门控编号不同源）：
10 条真实文本往返成功（返回向量数 = 10 且逐条非空），维度与配置一致——
配置 KB_EMBEDDING_DIM 时强校验相等，未配置则报告实际维度供 .env 回填。
消费 backend/.env 的 LLM_API_BASE / LLM_API_KEY / KB_EMBEDDING_MODEL
（D4：复用 llm 既有凭证，零新增凭证面）。
"""

from __future__ import annotations

import asyncio
import sys
import time

from app.config import settings
from app.kb import ArkEmbeddingClient, EmbeddingCallError, FakeEmbedder

#: 10 条真实校园 FAQ 文本（冒烟语料：覆盖规章/办事指南/高频问题三类说法）
SMOKE_TEXTS = [
    "转专业需要满足什么条件？",
    "图书馆的开放时间是几点到几点？",
    "学生证丢失后如何补办？",
    "本学期期末考试安排什么时候公布？",
    "奖学金的评定标准有哪些？",
    "如何办理休学与复学手续？",
    "校园卡挂失和补办的流程是什么？",
    "宿舍网络故障应该找哪个部门报修？",
    "选修课的学分要求和绩点计算规则是什么？",
    "毕业审核需要达到哪些学分条件？",
]


def _report(ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {detail}")


async def _run_offline() -> None:
    vectors = await FakeEmbedder().embed_texts(SMOKE_TEXTS)
    _report(len(vectors) == len(SMOKE_TEXTS), f"向量数 {len(vectors)} == {len(SMOKE_TEXTS)}")
    _report(all(any(v) for v in vectors), "逐条非零向量")
    _report(len({len(v) for v in vectors}) == 1, f"维度一致（{len(vectors[0])}）")


async def _run_live() -> None:
    client = ArkEmbeddingClient()
    if not client.is_configured:
        print(
            "embedding 未配置（backend/.env 缺 LLM_API_BASE / LLM_API_KEY），退出。\n"
            "未配置是部署问题而非代码缺陷：构造与启动不失败（懒校验语义）。"
        )
        return

    print(f"=== AC-18c 冒烟（live）：{len(SMOKE_TEXTS)} 条真实文本，模型 {settings.kb_embedding_model} ===")
    exit_code = 0
    try:
        t0 = time.perf_counter()
        vectors = await client.embed_texts(SMOKE_TEXTS)
        elapsed = time.perf_counter() - t0

        _report(
            len(vectors) == len(SMOKE_TEXTS),
            f"往返成功：返回 {len(vectors)} 条 == 请求数",
        )
        _report(all(any(v) for v in vectors), "逐条非零向量")
        dims = {len(v) for v in vectors}
        _report(len(dims) == 1, f"全批维度一致：{dims}")
        actual_dim = next(iter(dims), 0)
        if settings.kb_embedding_dim is not None:
            _report(
                actual_dim == settings.kb_embedding_dim,
                f"维度与配置一致：实际 {actual_dim} == kb_embedding_dim={settings.kb_embedding_dim}",
            )
        else:
            print(f"  [INFO] kb_embedding_dim 未配置，实际维度 {actual_dim}（建议回填 .env）")
        print(f"  总耗时 {elapsed:.2f}s")
    except EmbeddingCallError as exc:
        exit_code = 1
        print(f"  [FAIL] 冒烟失败：{exc}")
    finally:
        await client.aclose()  # 释放 SDK 连接池

    sys.exit(exit_code)


async def main() -> None:
    if "--live" in sys.argv:
        await _run_live()
    else:
        await _run_offline()


if __name__ == "__main__":
    asyncio.run(main())
