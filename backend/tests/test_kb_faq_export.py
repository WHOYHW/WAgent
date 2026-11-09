"""K6 faq_export 回流线单测（文档 11 §4.1 M/N 落地，零网络）。

覆盖：归一化确定性、聚类三路（精确合并 / Jaccard 相似合并 / 短句防过聚 /
不相似不聚）、候选装配纯函数（排序排名/变体去重/红线并集/归属首见非空）、
CSV/Markdown 渲染（审核留白列）、run_export 只读铁证（零写库零 commit）+
status/since/min_count 过滤、CLI 端到端（临时 SQLite）。
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio
from sqlalchemy import func, select

from app.db import make_engine
from app.kb.faq_export import (
    FAQ_MIN_NORM_CHARS,
    TicketSlice,
    build_candidates,
    cluster_indices,
    main,
    normalize_question,
    render_csv,
    render_markdown,
    run_export,
)
from app.orm import Base, HandoffTicket


def _slice(i: int, q: str, *, rules: list[str] | None = None,
           owner: str | None = None, at: datetime | None = None) -> TicketSlice:
    return TicketSlice(
        id=i, question=q, hit_rules=rules or [], suggested_owner=owner,
        created_at=at or datetime(2026, 11, 9, 4, i, tzinfo=UTC),
    )


# ---- 归一化与聚类（纯函数）----


def test_normalize_folds_width_digits_punct():
    assert normalize_question("Ｑ：绩点 3.2，能毕业吗？") == "q绩点能毕业吗"
    assert normalize_question("  成绩\t查询!! ") == "成绩查询"


def test_cluster_exact_merge_despite_noise():
    idx = cluster_indices([
        normalize_question("重修怎么报名？"),
        normalize_question("重修怎么报名！！"),  # 标点噪声 → 归一化相同
        normalize_question("图书馆几点开门"),
    ])
    assert idx == [[0, 1], [2]]


def test_cluster_jaccard_similar_merge():
    idx = cluster_indices([
        normalize_question("校园卡丢了怎么挂失补办"),
        normalize_question("校园卡丢了怎么挂失补办啊"),  # 近重复 → 高重叠
        normalize_question("食堂周末营业吗"),
    ])
    assert idx == [[0, 1], [2]]


def test_cluster_short_questions_only_exact_merge():
    short_a = normalize_question("咋办")
    short_b = normalize_question("咋弄")  # 2-gram 重叠但长度 < 下限
    idx = cluster_indices([short_a, short_a, short_b])
    assert idx == [[0, 1], [2]]
    assert len(short_b) < FAQ_MIN_NORM_CHARS


def test_cluster_dissimilar_not_merged():
    idx = cluster_indices([
        normalize_question("我的校园卡丢失了怎么挂失补办"),
        normalize_question("下学期选课的时间安排什么时候公布"),
    ])
    assert idx == [[0], [1]]


def test_cluster_deterministic_same_input_same_output():
    qs = ["重修报名流程", "重修报名的流程", "借书期限", "退课申请"]
    norms = [normalize_question(q) for q in qs]
    assert cluster_indices(norms) == cluster_indices(norms)


# ---- 候选装配（纯函数）----


def test_build_candidates_ranking_and_fields():
    tickets = [
        _slice(3, "校园卡丢了怎么挂失补办", rules=["R1"], owner="信息中心"),
        _slice(1, "校园卡丢了怎么挂失补办！"),              # 同簇（归一化相同）
        _slice(7, "校园卡丢了怎么挂失补办啊", rules=["R1", "R2"]),  # 同簇（相似）
        _slice(9, "图书馆几点关门", at=datetime(2026, 11, 9, 8, tzinfo=UTC)),
    ]
    out = build_candidates(tickets)
    assert [c.count for c in out] == [3, 1]  # 频次降序
    assert out[0].rank == 1 and out[1].rank == 2
    assert out[0].sample_question == "校园卡丢了怎么挂失补办"  # 首见原文
    assert out[0].variants == ["校园卡丢了怎么挂失补办", "校园卡丢了怎么挂失补办！",
                               "校园卡丢了怎么挂失补办啊"]
    assert out[0].hit_rules == ["R1", "R2"]  # 并集排序去重
    assert out[0].suggested_owner == "信息中心"  # 首见非空
    assert out[0].ticket_ids == [1, 3, 7]  # 升序
    assert out[1].suggested_owner is None


def test_build_candidates_empty():
    assert build_candidates([]) == []


# ---- 渲染（纯函数）----


def test_render_csv_review_columns_blank():
    out = build_candidates([_slice(1, "重修怎么报名"), _slice(2, "重修怎么报名")])
    rows = list(csv.reader(io.StringIO(render_csv(out))))
    assert rows[0][:9] == ["序号", "频次", "样例问题", "变体数", "红线命中",
                           "建议归属", "首见(+8)", "最近(+8)", "工单ID"]
    assert rows[0][9:] == ["审核-是否采纳", "审核-FAQ问题", "审核-FAQ答案", "审核-入库版本"]
    assert rows[1][0] == "1" and rows[1][1] == "2" and all(v == "" for v in rows[1][9:])


def test_render_markdown_contains_workflow():
    out = build_candidates([_slice(1, "挂失校园卡")])
    md = render_markdown(out, generated_at=datetime(2026, 11, 9, tzinfo=UTC),
                         ticket_count=1, filters_desc="status=全部")
    assert "人工审核流程" in md and "kb_seed" in md and "app.kb.ingest" in md
    assert "挂失校园卡" in md
    empty = render_markdown([], generated_at=datetime(2026, 11, 9, tzinfo=UTC),
                            ticket_count=0, filters_desc="-")
    assert "无候选" in empty


# ---- run_export：只读铁证 + 过滤（临时 SQLite，零网络）----


async def _seed_tickets(engine, tickets: list[dict]):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        for t in tickets:
            session.add(HandoffTicket(
                student_id="20230001", session_id="s1", user_question=t["q"],
                intent_history=[], hit_rules=t.get("rules", []),
                retrieved_facts=[], agent_response_log="", priority="P2",
                status=t.get("status", "closed"),
                suggested_owner=t.get("owner"),
                created_at=t.get("at"),
            ))
        await session.commit()


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_run_export_readonly_and_outputs(db, tmp_path):
    await _seed_tickets(db, [
        {"q": "重修怎么报名"}, {"q": "重修怎么报名！", "status": "open"},
        {"q": "食堂几点开门"},
    ])
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(db, expire_on_commit=False)
    async with factory() as session:
        before = (await session.execute(select(func.count(HandoffTicket.id)))).scalar()
        result = await run_export(session, out_dir=tmp_path / "out")
        after = (await session.execute(select(func.count(HandoffTicket.id)))).scalar()
        assert len(session.new) == 0 and len(session.dirty) == 0  # 零写库铁证
    assert before == after == 3
    assert result.ticket_count == 3 and result.candidate_count == 2
    assert Path(result.csv_path).exists() and Path(result.md_path).exists()
    assert Path(result.csv_path).parent == tmp_path / "out"


async def test_run_export_filters(db, tmp_path):
    await _seed_tickets(db, [
        {"q": "重修怎么报名", "status": "open"},
        {"q": "重修怎么报名！", "status": "closed"},  # status=open 时被过滤
        {"q": "宿舍报修", "at": datetime(2026, 11, 1, tzinfo=UTC)},
        {"q": "宿舍报修电话", "at": datetime(2026, 11, 8, tzinfo=UTC)},
    ])
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(db, expire_on_commit=False)
    async with factory() as session:
        r1 = await run_export(session, out_dir=tmp_path, status="open",
                              since="2026-11-05", min_count=2)
        # open + since 过滤后只剩重修簇（count=1 < min_count=2）→ 零候选
        assert r1.ticket_count == 1 and r1.candidate_count == 0
        r2 = await run_export(session, out_dir=tmp_path, min_count=2)
        # 无过滤：重修簇 count=2、宿舍簇 count=2 均达标
        assert r2.candidate_count == 2
        assert r2.ticket_count == 4


# ---- CLI 端到端 ----


def test_cli_end_to_end(tmp_path):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    dbfile = tmp_path / "cli.db"
    engine = make_engine(f"sqlite+aiosqlite:///{dbfile}")

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(HandoffTicket(
                student_id="20230001", session_id="s1", user_question="缓考怎么申请",
                intent_history=[], hit_rules=[], retrieved_facts=[],
                agent_response_log="", priority="P2", status="closed",
            ))
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())
    out_dir = tmp_path / "cand"
    code = main(["--db", f"sqlite+aiosqlite:///{dbfile}", "--out", str(out_dir)])
    assert code == 0
    files = list(out_dir.glob("faq_candidates_*.csv"))
    assert len(files) == 1 and list(out_dir.glob("faq_candidates_*.md"))
