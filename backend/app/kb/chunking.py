"""切块三策略纯函数（文档 18 §三 v1.2，零 IO 零风险）。

页面形态决定策略（策略路由是 K3 ingest 的职责，本层不自动探测）：
- ``by_clause``   校规章程主策略：正则切「第X条」，中文数字任意进位；
- ``by_heading``  办事指南：markdown 标题定节；
- ``by_qa``       FAQ 页：「问：/Q:」行开启问答对；
- 超长段（单条/单节/单对 > max_chars）与无锚全文 → fixed 滑窗回退
  （窗口 max_chars、步长 max_chars - overlap，重叠只复制不删除）。

不变量（AC-18a）：① 任意块 ``len(text) ≤ max_chars``；② 不丢正文——原文
每个非空行必出现在块并集中；③ 块序 = 原文序，``section_idx`` 自 0 连续。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

#: 块长上限与重叠的默认值（与 config.kb_chunk_max_chars=500 对齐；overlap 固定 50 不进配置）
CHUNK_DEFAULT_MAX_CHARS = 500
CHUNK_DEFAULT_OVERLAP = 50

#: 条款锚：中文数字任意进位（一/十二/一百零一/三百二十），不匹配阿拉伯数字混写
_CLAUSE_RE = re.compile(r"第[零一二三四五六七八九十百千万]+条")

#: markdown 标题行（1~6 级），锚为标题文本（剥离 # 前缀）
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)

#: FAQ 问答行：「问：」「问:」「Q:」「Q：」（Q 大小写不敏感），锚为整行
_QA_LINE_RE = re.compile(r"^\s*(?:问[:：]|q[:：])\s*", re.IGNORECASE)

#: anchor 落库列宽（KbChunk.anchor String(256)），超长锚截断（正文不受影响）
_ANCHOR_MAX = 256


class KbChunkDraft(BaseModel):
    """切块器出参契约（K3 装配 KbChunk 行的直接来源，禁止裸 dict）。"""

    section_idx: int = Field(ge=0, description="页内序号，自 0 连续递增")
    anchor: str | None = Field(
        default=None, max_length=_ANCHOR_MAX,
        description="切块锚：条款号/markdown 标题/FAQ 问题行；前言与滑窗尾片为 None",
    )
    text: str = Field(min_length=1, description="块原文（非空行逐字保留，不丢正文）")


def _fixed_window(
    text: str, max_chars: int, overlap: int
) -> list[str]:
    """fixed 滑窗：窗口 ``max_chars``、步长 ``max_chars - overlap``，推进保证无死循环。"""
    if overlap >= max_chars:
        raise ValueError(f"不变量：overlap({overlap}) < max_chars({max_chars})")
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    step = max_chars - overlap
    pieces: list[str] = []
    start = 0
    while True:
        pieces.append(text[start : start + max_chars])
        if start + max_chars >= len(text):
            break
        start += step
    return pieces


def _assemble(
    sections: list[tuple[str | None, str]], max_chars: int, overlap: int
) -> list[KbChunkDraft]:
    """(anchor, section_text) 序列 → KbChunkDraft 列表。

    空白节跳过；超长节走滑窗（片共享该节 anchor）；锚超宽截断（列宽 256）。
    """
    drafts: list[KbChunkDraft] = []
    for anchor, section in sections:
        anchor = anchor[:_ANCHOR_MAX] if anchor else None
        pieces = _fixed_window(section, max_chars, overlap)
        for piece in pieces:
            drafts.append(KbChunkDraft(section_idx=len(drafts), anchor=anchor, text=piece))
    return drafts


def by_clause(
    text: str, *, max_chars: int = CHUNK_DEFAULT_MAX_CHARS, overlap: int = CHUNK_DEFAULT_OVERLAP
) -> list[KbChunkDraft]:
    """校规章程切块：以「第X条」为边界定节，前言独立成块（anchor=None）。

    无任何条款锚时整篇走滑窗（anchor=None）——回退是显式语义而非静默失败。
    """
    matches = list(_CLAUSE_RE.finditer(text))
    if not matches:
        return _assemble([(None, text)], max_chars, overlap)
    sections: list[tuple[str | None, str]] = []
    preamble = text[: matches[0].start()]
    if preamble.strip():
        sections.append((None, preamble))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(0), text[m.start() : end]))
    return _assemble(sections, max_chars, overlap)


def by_heading(
    text: str, *, max_chars: int = CHUNK_DEFAULT_MAX_CHARS, overlap: int = CHUNK_DEFAULT_OVERLAP
) -> list[KbChunkDraft]:
    """办事指南切块：markdown 标题（1~6 级）定节，节含标题行与正文，anchor=标题文本。

    无标题行时整篇走滑窗。
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return _assemble([(None, text)], max_chars, overlap)
    sections: list[tuple[str | None, str]] = []
    preamble = text[: matches[0].start()]
    if preamble.strip():
        sections.append((None, preamble))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(1), text[m.start() : end]))
    return _assemble(sections, max_chars, overlap)


def by_qa(
    text: str, *, max_chars: int = CHUNK_DEFAULT_MAX_CHARS, overlap: int = CHUNK_DEFAULT_OVERLAP
) -> list[KbChunkDraft]:
    """FAQ 切块：「问：/Q:」行开启新问答对（问题+答案同块），anchor=问题行。

    首个问答行前的前言独立成块；无问答行时整篇走滑窗。
    """
    lines = text.split("\n")
    starts: list[int] = [i for i, line in enumerate(lines) if _QA_LINE_RE.match(line)]
    if not starts:
        return _assemble([(None, text)], max_chars, overlap)
    sections: list[tuple[str | None, str]] = []
    preamble_lines = lines[: starts[0]]
    preamble = "\n".join(preamble_lines)
    if preamble.strip():
        sections.append((None, preamble))
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(lines)
        section = "\n".join(lines[start:end])
        sections.append((lines[start].strip(), section))
    return _assemble(sections, max_chars, overlap)


__all__ = [
    "CHUNK_DEFAULT_MAX_CHARS",
    "CHUNK_DEFAULT_OVERLAP",
    "KbChunkDraft",
    "by_clause",
    "by_heading",
    "by_qa",
]
