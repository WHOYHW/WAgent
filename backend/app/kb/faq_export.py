
"""handoff → FAQ 沉淀回流线（文档 11 §4.1 节点 M/N 落地，K6）：

工单 user_question 聚类 → 只读导出候选 FAQ（CSV + Markdown）→ **人工审核兜底**
→ 审核通过的问题/答案手工补进 ``kb_seed/*.yaml``（strategy: qa）→
``python -m app.kb.ingest --seed ...`` 入库进知识库。

    python -m app.kb.faq_export                              # 全量工单 → faq_candidates/
    python -m app.kb.faq_export --status closed              # 只导已办结工单
    python -m app.kb.faq_export --since 2026-11-01 --min-count 2
    python -m app.kb.faq_export --db <url> --out <dir>       # 指定库/输出目录

三条铁律（对齐文档 11「人兜底」与 18 纯函数纪律）：
- **只读**：全程 SELECT，零写库零 commit——沉淀动作由人完成后走 ingest，脚本
  不做自动入库（LLM 零参与，聚类为确定性纯函数）；
- **可审核**：CSV 预留「审核-采纳/FAQ 问题/FAQ 答案/入库版本」空列，人工逐簇
  裁决；导出物含学生原话，``faq_candidates/`` 不入 git（.gitignore）；
- **可复算**：normalize → 2-gram Jaccard 并查集聚类全部纯函数，同输入必同输出。

分层约束：本模块属 app.kb 层（forbidden 契约：不 import app.orchestrator/app.llm）；
读 handoff_ticket 仅 ORM。不设 D5 门控——导出是内部运营报表，不提供任何面向
学生的运行时能力（与 ingest 的「能力上线是运营决策」语义不同源）。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import re
import sys
import time
import unicodedata
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import make_engine
from ..logging_util import get_logger
from ..orm import HandoffTicket

logger = get_logger("kb")

#: 展示时区（对齐 reminder.evaluator：固定 +8，免 tzdata 依赖）
CAMPUS_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

#: 2-gram Jaccard 相似度合并阈值（≥ 视为同一问题变体）
FAQ_SIMILAR_THRESHOLD = 0.6

#: 归一化长度下限——短于该值的问题只做精确归并（不参与相似合并，防短句过聚）
FAQ_MIN_NORM_CHARS = 4

#: CSV 审核留白列（人兜底落在产物上：人工逐簇裁决后回填）
_REVIEW_COLUMNS = ("审核-是否采纳", "审核-FAQ问题", "审核-FAQ答案", "审核-入库版本")


# ---- 第 1 步：归一化与聚类（纯函数，LLM 零参与）----


def normalize_question(text: str) -> str:
    """问题归一化：NFKC 折叠全角/半角 → 小写 → 剔除数字噪声（学号/日期/编号）
    → 剔除标点与空白（``\\w`` 含 CJK，中文原样保留）。"""
    s = unicodedata.normalize("NFKC", text).lower()
    s = re.sub(r"\d+", "", s)
    return re.sub(r"[^\w]+", "", s)


def _bigrams(norm: str) -> set[str]:
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def cluster_indices(norms: list[str]) -> list[list[int]]:
    """确定性聚类：归一化相同必同簇；长度达标者 2-gram Jaccard ≥ 阈值并查集
    合并。输入顺序即遍历顺序（调用方按 created_at 排序传入），同输入必同输出。
    返回簇列表，簇内为原列表下标，簇间按首见顺序。"""
    parent = list(range(len(norms)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grams = [_bigrams(n) for n in norms]
    for j in range(len(norms)):
        for i in range(j):
            if norms[i] == norms[j]:
                parent[find(i)] = find(j)
                continue
            if len(norms[i]) < FAQ_MIN_NORM_CHARS or len(norms[j]) < FAQ_MIN_NORM_CHARS:
                continue  # 短句只做精确归并
            if _jaccard(grams[i], grams[j]) >= FAQ_SIMILAR_THRESHOLD:
                parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for idx in range(len(norms)):
        groups.setdefault(find(idx), []).append(idx)
    return list(groups.values())


# ---- 第 2 步：候选装配（纯函数，零 IO——对齐 reminder candidates 纪律）----


class TicketSlice(BaseModel):
    """工单投影（只取聚类所需字段，student_id 永不出本模块——R3 脱敏纪律）。"""

    id: int
    question: str
    hit_rules: list[str] = Field(default_factory=list)
    suggested_owner: str | None = None
    created_at: datetime


class FaqCandidate(BaseModel):
    """候选 FAQ 簇（人工审核单元）：count 为采纳优先级的第一信号。"""

    rank: int = Field(description="按频次降序排名（1 起）")
    count: int = Field(description="簇内工单数（同类问题人次）")
    sample_question: str = Field(description="首见原文（人工审核样例）")
    variants: list[str] = Field(description="簇内原文去重（保留首见顺序）")
    hit_rules: list[str] = Field(description="簇内命中的红线规则并集（排序去重）")
    suggested_owner: str | None = Field(description="首见非空的建议归属部门")
    ticket_ids: list[int] = Field(description="簇内工单 ID（升序，供回查工单系统）")
    first_seen: datetime
    last_seen: datetime


def build_candidates(tickets: list[TicketSlice]) -> list[FaqCandidate]:
    """工单投影 → 候选簇列表（纯函数：聚类 → 装配 → 排序 → 排名）。

    排序：频次降序 → 最近提问降序 → 样例文本升序（全确定性平局裁决）。
    """
    norms = [normalize_question(t.question) for t in tickets]
    candidates: list[FaqCandidate] = []
    for members in cluster_indices(norms):
        rows = [tickets[i] for i in members]
        variants: list[str] = []
        for r in rows:
            if r.question not in variants:
                variants.append(r.question)
        candidates.append(FaqCandidate(
            rank=0,
            count=len(rows),
            sample_question=variants[0],
            variants=variants,
            hit_rules=sorted({rule for r in rows for rule in r.hit_rules}),
            suggested_owner=next((r.suggested_owner for r in rows if r.suggested_owner), None),
            ticket_ids=sorted(r.id for r in rows),
            first_seen=min(_aware(r.created_at) for r in rows),
            last_seen=max(_aware(r.created_at) for r in rows),
        ))
    candidates.sort(key=lambda c: (-c.count, -c.last_seen.timestamp(), c.sample_question))
    for i, c in enumerate(candidates, start=1):
        c.rank = i
    return candidates


def _aware(dt: datetime) -> datetime:
    """SQLite 可能存 naive（按 UTC 解释）；统一折成 aware 便于比较。"""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _display(dt: datetime) -> str:
    return _aware(dt).astimezone(CAMPUS_TZ).strftime("%Y-%m-%d %H:%M")


# ---- 第 3 步：渲染（纯函数）----


def render_csv(candidates: list[FaqCandidate]) -> str:
    """审核用 CSV（utf-8-sig 落盘，Excel 直开；审核四列留白人兜底）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(("序号", "频次", "样例问题", "变体数", "红线命中", "建议归属",
                     "首见(+8)", "最近(+8)", "工单ID", *_REVIEW_COLUMNS))
    for c in candidates:
        writer.writerow((c.rank, c.count, c.sample_question, len(c.variants),
                         ";".join(c.hit_rules) or "-", c.suggested_owner or "-",
                         _display(c.first_seen), _display(c.last_seen),
                         " ".join(str(i) for i in c.ticket_ids),
                         "", "", "", ""))
    return buf.getvalue()


def render_markdown(candidates: list[FaqCandidate], *, generated_at: datetime,
                    ticket_count: int, filters_desc: str) -> str:
    lines = [
        "# 候选 FAQ 导出（handoff → FAQ 回流线，文档 11 §4.1 M/N 落地）",
        "",
        f"- 生成时间：{_display(generated_at)}（UTC+8）｜ 参与聚类工单：{ticket_count} 张｜ 筛选：{filters_desc}",
        f"- 聚类口径：归一化精确合并 + 2-gram Jaccard ≥ {FAQ_SIMILAR_THRESHOLD}"
        f"（短句 < {FAQ_MIN_NORM_CHARS} 只精确合并）；LLM 零参与，同输入必同输出。",
        "",
        "**人工审核流程（本脚本只导出，不自动入库）**：",
        "",
        "1. 逐簇审核下表与 CSV（``faq_candidates/``）：判断是否值得沉淀为 FAQ；",
        "2. 在 CSV 审核列回填：是否采纳 / 规范化问题 / 参考答案 / 目标语料版本；",
        "3. 采纳条目手工写入 ``kb_seed/*.yaml``（strategy: qa，问题行即锚）；",
        "4. ``python -m app.kb.ingest --seed kb_seed/<你的种子>.yaml --version <新版本号>`` 入库，",
        "   下一次检索即可命中（kb_search 引用《文档名》口径见提示词 v2）。",
        "",
        "| 序号 | 频次 | 样例问题 | 变体数 | 红线命中 | 建议归属 | 最近提问 | 工单ID |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in candidates:
        lines.append(
            f"| {c.rank} | {c.count} | {c.sample_question} | {len(c.variants)} "
            f"| {';'.join(c.hit_rules) or '-'} | {c.suggested_owner or '-'} "
            f"| {_display(c.last_seen)} | {' '.join(map(str, c.ticket_ids))} |"
        )
    if not candidates:
        lines.append("| - | 0 | （无候选：工单量不足或全部被过滤） | - | - | - | - | - |")
    lines.append("")
    lines.append("> 导出物含学生原话，属内部审核材料：不入 git、不外发；审核完成的答案建议同步核对来源（源系统/官网），与「权威性可追溯」约定一致。")
    return "\n".join(lines)


# ---- 第 4 步：导出执行（唯一 IO 边界：一次 SELECT + 两个文件写盘）----


class ExportResult(BaseModel):
    """导出结果报告（CLI 输出与单测断言依据）。"""

    csv_path: str
    md_path: str
    ticket_count: int
    candidate_count: int


async def run_export(session: AsyncSession, *, out_dir: Path,
                     status: str | None = None, since: str | None = None,
                     min_count: int = 1) -> ExportResult:
    """只读导出：SELECT 工单 → 纯函数聚类 → CSV + Markdown 落盘。

    零写库：全程 SELECT（无 add/commit）；``since`` 为 ISO 日期（含当日，
    UTC+8 口径）；``status`` ∈ open/handling/closed；``min_count`` 过滤低频噪声簇。
    """
    stmt = select(HandoffTicket).order_by(HandoffTicket.created_at, HandoffTicket.id)
    if status:
        stmt = stmt.where(HandoffTicket.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    if since:
        floor = datetime.fromisoformat(since).replace(tzinfo=CAMPUS_TZ)
        rows = [r for r in rows if _aware(r.created_at) >= floor]

    slices = [TicketSlice(
        id=r.id, question=r.user_question, hit_rules=list(r.hit_rules),
        suggested_owner=r.suggested_owner, created_at=r.created_at,
    ) for r in rows]
    candidates = [c for c in build_candidates(slices) if c.count >= min_count]

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"faq_candidates_{stamp}.csv"
    md_path = out_dir / f"faq_candidates_{stamp}.md"
    filters_desc = (f"status={status or '全部'} since={since or '不限'} min_count={min_count}")
    csv_path.write_text(render_csv(candidates), encoding="utf-8-sig")
    md_path.write_text(render_markdown(
        candidates, generated_at=datetime.now(UTC),
        ticket_count=len(rows), filters_desc=filters_desc,
    ), encoding="utf-8")
    logger.info("faq_export_done", tickets=len(rows), clusters=len(candidates),
                csv=csv_path.name)
    return ExportResult(csv_path=str(csv_path), md_path=str(md_path),
                        ticket_count=len(rows), candidate_count=len(candidates))


# ---- CLI ----


async def _amain(args: argparse.Namespace) -> int:
    engine = make_engine(args.db)  # 缺省 settings.db_url
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await run_export(
                session, out_dir=Path(args.out),
                status=args.status, since=args.since, min_count=args.min_count,
            )
        print(f"[faq-export] tickets={result.ticket_count} "
              f"clusters={result.candidate_count}（只读导出，未写库）")
        print(f"  CSV：{result.csv_path}")
        print(f"  MD ：{result.md_path}")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="handoff → FAQ 候选只读导出（文档 11 §4.1 回流线，K6）")
    parser.add_argument("--db", default=None, help="数据库连接串（缺省 settings.db_url）")
    parser.add_argument("--out", default="faq_candidates", help="输出目录（缺省 ./faq_candidates）")
    parser.add_argument("--status", choices=["open", "handling", "closed"], default=None,
                        help="只导指定状态工单（缺省全部）")
    parser.add_argument("--since", default=None, help="只导该 ISO 日期（UTC+8）之后的工单")
    parser.add_argument("--min-count", type=int, default=1,
                        help="只导频次 ≥ N 的簇（缺省 1，即全量）")
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    started = time.perf_counter()
    code = main()
    print(f"[faq-export] done in {time.perf_counter() - started:.1f}s")
    sys.exit(code)
