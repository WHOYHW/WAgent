"""K1 契约单测（文档 18 §三/§五，零 IO 零网络）。

切块三策略的规则与不变量测试在 test_kb_chunking.py；本文件覆盖：
1. make_doc_id 确定性（增量刷新按 URL 定位的锚）；
2. Pydantic 契约：参数校验拒空 query、出参 mode/meta 完整（禁止裸 dict 落点）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.kb import (
    KbDocMeta,
    KbHit,
    KbSearchParams,
    KbSearchResult,
    RetrievalMode,
    make_doc_id,
)
from app.schemas.meta import DegradeStatus, ToolMeta, ToolSource

# ---- make_doc_id：确定性 ----


def test_make_doc_id_deterministic_and_distinct():
    url = "https://jwc.hnist.cn/info/1041/12345.htm"
    assert make_doc_id(url) == make_doc_id(url)
    assert make_doc_id(url) != make_doc_id(url.replace("12345", "12346"))
    assert len(make_doc_id(url)) == 16


# ---- 契约：校验与组装 ----


def test_kb_search_params_rejects_blank_query():
    with pytest.raises(ValidationError):
        KbSearchParams(query="")
    with pytest.raises(ValidationError):
        KbSearchParams(query="x" * 257)


def test_kb_doc_meta_roundtrip():
    meta = KbDocMeta(
        doc_id=make_doc_id("https://www.hnist.cn/index/notice.htm"),
        title="通知公告",
        source_url="https://www.hnist.cn/index/notice.htm",
        column="校级/通知公告",
        fetched_at="2026-11-04T08:00:00+08:00",
        corpus_version="2026-11-04.1",
    )
    assert meta.corpus_version == "2026-11-04.1"


def test_kb_search_result_assembly():
    hit = KbHit(
        doc_id="a" * 16, title="转专业实施办法", content="……",
        source_url="https://jwc.hnist.cn/gzzd/xjgl.htm", score=0.031,
    )
    result = KbSearchResult(
        items=[hit],
        mode=RetrievalMode.FTS_ONLY,
        meta=ToolMeta(
            source=ToolSource.INTERNAL,
            synced_at="2026-11-04T08:00:00+08:00",
            degrade_status=DegradeStatus.LIVE_OK,
        ),
    )
    # v1.5 裁决①：出参键为 items（既有工具惯例，_result_digest 认该键）
    assert result.items[0].score == pytest.approx(0.031)
    assert result.mode is RetrievalMode.FTS_ONLY


def test_kb_search_params_top_k_optional():
    params = KbSearchParams(query="转专业条件")
    assert params.top_k is None  # 缺省由系统决定（工具层钳制到 kb_top_k）
    params = KbSearchParams(query="转专业条件", top_k=2)
    assert params.top_k == 2
    with pytest.raises(ValidationError):
        KbSearchParams(query="转专业条件", top_k=0)
