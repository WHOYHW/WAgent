"""K5 ingest 入库管线单测（文档 18 §三 v1.6，AC-18e，零网络）。

fixture 档：本地 HTML 夹具 + FakeEmbedder + 临时 SQLite 库，覆盖——
博达正文提取（v_news_content 择优 / edu-main-content 回退 / 壳页跳过）、
种子 YAML 契约校验、域白名单、幂等铁证（同版本重跑零写入零 embedding 调用 +
fetched_at 零变化 + provenance 100% 非空）、内容变更整页重插、单事务回滚、
CLI 门控与 --fixture/--dry-run 端到端。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.config import Settings
from app.db import make_engine
from app.kb.embedder import FakeEmbedder
from app.kb.ingest import (
    MIN_CONTENT_CHARS,
    IngestReport,
    SeedEntry,
    _ensure_kb_fts,
    extract_boda,
    fetch_html,
    load_seed,
    main,
    make_fetcher,
    run_ingest,
)
from app.orm import KbChunk

FIXTURES = Path(__file__).parent / "fixtures" / "kb"
ARTICLE_URL = "https://jwc.hnist.cn/info/1045/2119.htm"  # fixture 对应的真实 URL（域白名单内）
VERSION = "2026-11-09.1"
SETTINGS = Settings(kb_enabled=True, kb_corpus_version=VERSION)


@pytest_asyncio.fixture
async def db(tmp_path):
    """临时文件库：表 + FTS 虚表由 ingest._ensure_kb_fts 自建（防御路径实测）。"""
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'ingest.db'}")
    from app.orm import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker_for(engine)
    async with factory() as session:
        await _ensure_kb_fts(session)  # 内存库/新库防御路径（Base.metadata 建不出虚表）
        yield session
    await engine.dispose()


def async_sessionmaker_for(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


def fixture_fetcher():
    """url → fixture HTML：真实 URL 映射到本地夹具（零网络）。"""
    mapping = {
        ARTICLE_URL: FIXTURES / "boda_article.html",
        "https://jwc.hnist.cn/bszn/wspk.htm": FIXTURES / "shell_page.html",
        "https://jwc.hnist.cn/bszn/zxsj.htm": FIXTURES / "plain_page.html",
    }

    async def _fetch(url: str) -> str:
        return (mapping[url]).read_text(encoding="utf-8")

    return _fetch


async def _run(session, entries, embedder=None):
    return await run_ingest(
        session, entries,
        settings=SETTINGS, embedder=embedder or FakeEmbedder(),
        corpus_version=VERSION, fetcher=fixture_fetcher(),
    )


# ---- 第 1 步：抓取（4xx 不重试 / 瞬态故障重试）----


def _mock_client(calls: list[int], status: int):
    """httpx.MockTransport 桩：每次请求计数并返回固定状态码（零网络）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_html_4xx_no_retry():
    """404 是确定性失败：仅 1 次请求直接上抛（重试无意义，fail fast）。"""
    calls: list[int] = []
    async with _mock_client(calls, 404) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_html("https://jwc.hnist.cn/missing.htm", SETTINGS, client)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [503, 429])
async def test_fetch_html_transient_retries_then_raises(status):
    """瞬态故障（5xx / 429）：重试 read_retry_times 次后包装 RuntimeError 上抛。"""
    calls: list[int] = []
    settings = Settings(kb_enabled=True, retry_backoff_initial=0.001, retry_backoff_max=0.002)
    async with _mock_client(calls, status) as client:
        with pytest.raises(RuntimeError, match="fetch failed"):
            await fetch_html("https://jwc.hnist.cn/slow.htm", settings, client)
    assert len(calls) == 1 + settings.read_retry_times


# ---- 第 2 步：博达正文提取 ----


def test_extract_vnc_selector_and_h1_title():
    html = (FIXTURES / "boda_article.html").read_text(encoding="utf-8")
    title, text = extract_boda(html)
    assert title == "重修办理流程"
    assert "第一条 重修报名" in text
    assert "校园卡" not in text  # 导航/页脚不混入
    assert "湘北大道" not in text  # 页脚联系块（同名 v_news_content）被择优排除


def test_extract_fallback_edu_main_content():
    html = (FIXTURES / "plain_page.html").read_text(encoding="utf-8")
    title, text = extract_boda(html)
    assert "自习室" in title  # <title> 剥站点后缀
    assert "7:00-22:00" in text.replace(" ", "")


def test_extract_empty_for_empty_html():
    assert extract_boda("<html><body></body></html>") == ("", "")


# ---- 种子契约与域白名单 ----


def test_seed_entry_rejects_unknown_strategy(tmp_path):
    seed = tmp_path / "bad.yaml"
    seed.write_text("- url: https://jwc.hnist.cn/info/1/2.htm\n  column: x\n  strategy: wrong\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_seed(seed)


def test_seed_entry_ok(tmp_path):
    seed = tmp_path / "ok.yaml"
    seed.write_text(
        "- url: https://jwc.hnist.cn/info/1022/4189.htm\n  column: 教务处/上级政策法规\n  strategy: clause\n",
        encoding="utf-8",
    )
    entries = load_seed(seed)
    assert entries[0].strategy == "clause"


async def test_live_fetcher_rejects_off_domain():
    fetcher = make_fetcher("live", SETTINGS, None)
    with pytest.raises(ValueError, match="域白名单外"):
        await fetcher("https://example.com/x.htm")


# ---- run_ingest：幂等铁证（AC-18e）----


async def test_ingest_provenance_100_percent(db):
    entries = [
        SeedEntry(url=ARTICLE_URL, column="教务处/办事指南", strategy="heading"),
        SeedEntry(url="https://jwc.hnist.cn/bszn/zxsj.htm", column="教务处/办事指南", strategy="heading"),
        SeedEntry(url="https://jwc.hnist.cn/bszn/wspk.htm", column="教务处/办事指南", strategy="heading"),
    ]
    report = await _run(db, entries)
    await db.commit()
    assert report.inserted == 2  # 壳页 <200 字符诚实跳过
    assert report.skipped_short == 1
    assert MIN_CONTENT_CHARS == 200
    rows = (await db.execute(select(KbChunk))).scalars().all()
    assert rows, "应写入 chunk 行"
    for row in rows:  # provenance 100% 非空
        assert row.source_url and row.corpus_version and row.fetched_at
        assert row.content_hash and row.embedding and row.embedding_model


async def test_rerun_same_version_zero_write_and_zero_embedding(db):
    """AC-18e 铁证：同版本重跑 inserted=updated=0、行数与 fetched_at 零变化、零 embedding 调用。"""
    entries = [SeedEntry(url=ARTICLE_URL, column="教务处/办事指南", strategy="heading")]

    class CountingEmbedder(FakeEmbedder):
        calls = 0

        async def embed_texts(self, texts):
            CountingEmbedder.calls += 1
            return await super().embed_texts(texts)

    report1 = await _run(db, entries, embedder=CountingEmbedder())
    await db.commit()
    assert report1.inserted == 1 and report1.chunks > 0
    rows_before = (await db.execute(select(KbChunk))).scalars().all()
    fetched_before = {r.id: r.fetched_at for r in rows_before}

    report2 = await _run(db, entries, embedder=CountingEmbedder())
    await db.commit()
    assert report2.inserted == 0
    assert report2.updated == 0
    assert report2.skipped_unchanged == 1
    assert report2.chunks == 0
    assert CountingEmbedder.calls == 1  # 重跑零 embedding 调用
    rows_after = (await db.execute(select(KbChunk))).scalars().all()
    assert {r.id: r.fetched_at for r in rows_after} == fetched_before
    assert len(rows_after) == len(rows_before)


async def test_changed_content_reinserts_page(db):
    """全文已变 → 按 doc_id 整页删除重插（行数 = 新切块数，无旧行残留）。"""
    entries = [SeedEntry(url=ARTICLE_URL, column="教务处/办事指南", strategy="heading")]
    await _run(db, entries)
    await db.commit()
    old_doc_id_rows = (await db.execute(select(KbChunk))).scalars().all()
    old_hashes = {r.content_hash for r in old_doc_id_rows}

    # 模拟站点内容变更：换一版 fixture HTML（追加一段正文 → content_hash 变）
    article = (FIXTURES / "boda_article.html").read_text(encoding="utf-8")
    changed = article.replace("考核成绩按重修成绩记载。", "考核成绩按重修成绩记载。重修通过后学分替换原课程学分。")

    async def changed_fetcher(url: str) -> str:
        return changed

    report = await run_ingest(
        db, entries, settings=SETTINGS, embedder=FakeEmbedder(),
        corpus_version=VERSION, fetcher=changed_fetcher,
    )
    await db.commit()
    assert report.updated == 1
    rows = (await db.execute(select(KbChunk))).scalars().all()
    assert all(r.content_hash not in old_hashes for r in rows)  # 整页替换，无旧 hash 残留
    assert any("学分替换" in r.content for r in rows)


async def test_embedding_failure_rolls_back_everything(db):
    """单事务：embedding 失败上抛 → 无行残留（调用方 session 回滚零脏数据）。"""
    entries = [SeedEntry(url=ARTICLE_URL, column="教务处/办事指南", strategy="heading")]

    class ExplodingEmbedder:
        async def embed_texts(self, texts):
            raise RuntimeError("ark down")

    with pytest.raises(RuntimeError, match="ark down"):
        await _run(db, entries, embedder=ExplodingEmbedder())
    await db.rollback()
    count = (await db.execute(select(func.count(KbChunk.id)))).scalar()
    assert count == 0


async def test_fts_index_synced_after_page_reinsert(db):
    """FTS5 触发器随整页删除重插同步：旧词命中清空、新词可命中（索引零残留）。"""
    entries = [SeedEntry(url=ARTICLE_URL, column="教务处/办事指南", strategy="heading")]
    await _run(db, entries)
    await db.commit()

    async def fts_match(term: str) -> int:
        sql = "SELECT COUNT(*) FROM kb_chunk_fts WHERE kb_chunk_fts MATCH :q"
        return (await db.execute(text(sql).bindparams(q=f'"{term}"'))).scalar()

    assert await fts_match("重修报名") > 0
    article = (FIXTURES / "boda_article.html").read_text(encoding="utf-8")
    changed = article.replace("重修报名", "补考报名")

    async def changed_fetcher(url: str) -> str:
        return changed

    await run_ingest(
        db, entries, settings=SETTINGS, embedder=FakeEmbedder(),
        corpus_version=VERSION, fetcher=changed_fetcher,
    )
    await db.commit()
    assert await fts_match("重修报名") == 0  # 旧索引行已随删除触发器移除
    assert await fts_match("补考报名") > 0  # 新索引行已随插入触发器建立


# ---- CLI（--fixture 档端到端 / 门控）----


def _make_cli_seed(tmp_path: Path) -> Path:
    seed = tmp_path / "seed.yaml"
    entries = [
        {"url": str(FIXTURES / "boda_article.html"), "column": "教务处/办事指南", "strategy": "heading"},
        {"url": str(FIXTURES / "shell_page.html"), "column": "教务处/办事指南", "strategy": "heading"},
    ]
    import yaml

    seed.write_text(yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8")
    return seed


def test_cli_fixture_mode_end_to_end(tmp_path, monkeypatch, capsys):
    """CLI --fixture 端到端：零网络入库 → 再跑一次幂等零变化。"""
    dbfile = tmp_path / "cli.db"
    seed = _make_cli_seed(tmp_path)
    monkeypatch.setattr("app.kb.ingest.default_settings", Settings(kb_enabled=True))

    argv = ["--seed", str(seed), "--fixture", "--db", f"sqlite+aiosqlite:///{dbfile}", "--version", VERSION]
    assert main(argv) == 0
    out1 = capsys.readouterr().out
    assert "inserted=1" in out1 and "provenance" in out1 and "non-empty-violations=0" in out1

    assert main(argv) == 0  # 同版本重跑
    out2 = capsys.readouterr().out
    assert "inserted=0" in out2 and "skipped_unchanged=1" in out2


def test_cli_refuses_when_kb_disabled(monkeypatch, tmp_path, capsys):
    """D5 门控：kb_enabled=False → 拒绝运行（exit 2），不触库不触网。"""
    monkeypatch.setattr("app.kb.ingest.default_settings", Settings(kb_enabled=False))
    argv = ["--seed", str(_make_cli_seed(tmp_path)), "--fixture", "--db", "sqlite+aiosqlite:///:memory:"]
    assert main(argv) == 2
    assert "拒绝运行" in capsys.readouterr().out


def test_cli_dry_run_rejects_off_domain(monkeypatch, tmp_path):
    """dry-run 与正式档同源抓取器：域外 URL 即刻报错（原直连 fetch_html 绕过白名单）。"""
    seed = tmp_path / "bad_seed.yaml"
    seed.write_text("- url: https://example.com/x.htm\n  column: x\n  strategy: heading\n", encoding="utf-8")
    monkeypatch.setattr("app.kb.ingest.default_settings", Settings(kb_enabled=True))
    with pytest.raises(ValueError, match="域白名单外"):
        main(["--seed", str(seed), "--dry-run"])


def test_real_seed_yaml_parses():
    """交付物守护：backend/kb_seed/jwc_seed.yaml 必须可解析且为 15 条 SeedEntry。"""
    seed_path = Path(__file__).parent.parent / "kb_seed" / "jwc_seed.yaml"
    entries = load_seed(seed_path)
    assert len(entries) == 15
    assert all(e.url.startswith("https://jwc.hnist.cn/") for e in entries)
    strategies = {e.strategy for e in entries}
    assert strategies <= {"clause", "heading", "qa"}


def test_report_model_is_pydantic():
    assert IngestReport().inserted == 0
    assert SeedEntry(url="u", column="c", strategy="qa").strategy == "qa"
