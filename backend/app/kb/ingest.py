"""入库管线 CLI（文档 18 §三 v1.6，K5）：种子 YAML → 抓取 → 博达正文提取 →
K1 切块 → embedding → 单事务落库（业务表 + FTS5 触发器同步 + corpus_version 标记）。

    python -m app.kb.ingest --seed kb_seed/jwc_seed.yaml           # 真机档（网络 + 方舟）
    python -m app.kb.ingest --seed <yaml> --dry-run                # 只抓取+提取+切块+报告，不落库
    python -m app.kb.ingest --seed <yaml> --fixture --db <url>     # fixture 档：url=本地 HTML 文件
                                                                   # + FakeEmbedder，零网络（CI 用）

裁决锚（v1.6）：判重键 = doc_id + content_hash（清洗后全文 sha256）——同版本重跑
且全文未变全部 skip（零写入零 embedding 调用）；已变整页删除重插。落库单事务：
一次 session 贯穿、最终单次 commit，embedding 失败整体回滚（不做 embedding=None
半态入库——矩阵加载过滤下只会产生死数据）。AC-18e 铁证 = 同版本重跑行数与
fetched_at 零变化 + provenance 字段 100% 非空。

分层约束：本模块属 app.kb 层，不 import app.llm / app.tools（forbidden 契约）；
重试对齐 adapters/base 口径（read_retry_times + 指数退避）但独立实现——ingest 是
离线运营 CLI，不走「缓存→源系统→降级」链。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html as html_lib
import re
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import numpy as np
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..config import settings as default_settings
from ..db import make_engine
from ..logging_util import get_logger
from ..orm import Base, KbChunk
from .chunking import by_clause, by_heading, by_qa
from .embedder import ArkEmbeddingClient, EmbeddingClient, FakeEmbedder
from .fts import KB_FTS_TABLE_DDL, KB_FTS_TRIGGER_DDLS
from .schemas import make_doc_id

logger = get_logger("kb")

#: 抓取 UA（v1.6 ③：对校方站点明示身份）
CRAWLER_USER_AGENT = "WAgent-KB-Ingest/1.0 (campus FAQ corpus; contact: ops@wagent.local)"

#: 单次抓取超时（官网站点与校内源系统 2s 口径不同源，独立常量，v1.6 ③）
CRAWLER_TIMEOUT_SECONDS = 20.0

#: 域白名单（附录 A 抓取边界；fixture 档豁免——url 是本地文件路径）
ALLOWED_DOMAIN_SUFFIXES = (".hnist.cn", ".hnist.edu.cn")

#: 正文最小字符数（§三清洗规则：<200 字符或纯链接壳页丢弃）
MIN_CONTENT_CHARS = 200

#: 站点后缀（<title> 剥离用；博达 CMS 每页 title 带站点名）
_TITLE_SUFFIXES = ("-湖南理工大学教务处", "-湖南理工学院教务处")

_Strategy = Literal["clause", "heading", "qa"]
_STRATEGY_FNS = {"clause": by_clause, "heading": by_heading, "qa": by_qa}


class SeedEntry(BaseModel):
    """种子清单条目契约（v1.6 ②，禁止裸 dict）。

    strategy 显式指定（§三"策略选择是 ingest 职责"落地，无隐藏默认值）。
    """

    url: str = Field(description="文章页 URL（域白名单内）；fixture 档为本地 HTML 文件路径")
    column: str = Field(max_length=128, description="所属栏目，如 教务处/办事指南")
    strategy: _Strategy = Field(description="切块策略：clause（规章）/ heading（指南）/ qa（FAQ）")


class IngestReport(BaseModel):
    """单次运行报告（CLI 输出与单测断言依据，禁止裸 dict）。"""

    fetched: int = Field(default=0, description="成功抓取并提取的文档数")
    inserted: int = Field(default=0, description="新插入文档数")
    updated: int = Field(default=0, description="内容已变整页重插文档数")
    skipped_unchanged: int = Field(default=0, description="同版本重跑未变跳过文档数")
    skipped_short: int = Field(default=0, description="<200 字符壳页丢弃数")
    chunks: int = Field(default=0, description="本次写入的 chunk 行数")
    skipped_list: list[str] = Field(default_factory=list, description="跳过明细（URL + 原因）")


# ---- 第 1 步：抓取（v1.6 ③，对齐 adapters/base 重试口径）----


def _backoff_delay(attempt: int, settings: Settings) -> float:
    """指数退避：initial × 2^attempt，上限 max（对齐 adapters/base 公式）。"""
    return min(settings.retry_backoff_initial * (2**attempt), settings.retry_backoff_max)


async def fetch_html(url: str, settings: Settings, client: httpx.AsyncClient) -> str:
    """单页抓取：重试 read_retry_times 次（指数退避），失败上抛最后一错。

    重试只覆盖瞬态故障（传输错误 / 5xx / 429）；4xx（除 429）是确定性失败
    （404 不存在 / 403 拒绝），重试无意义——直接上抛，fail fast 暴露种子错误。
    """
    attempts = 1 + settings.read_retry_times
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500 and status != 429:
                raise
            last_exc = exc
            _log_fetch_retry(url, attempt, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 统一包装为可读消息
            last_exc = exc
            _log_fetch_retry(url, attempt, type(exc).__name__)
        if attempt < attempts - 1:
            await asyncio.sleep(_backoff_delay(attempt, settings))
    raise RuntimeError(f"fetch failed after {attempts} attempts: {last_exc!r}") from last_exc


def _log_fetch_retry(url: str, attempt: int, error_type: str) -> None:
    logger.warning(
        "kb_fetch_retry", url_domain=url.split("/")[2] if "://" in url else url,
        attempt=attempt + 1, error=error_type,
    )


# ---- 第 2 步：博达正文提取（v1.6 ④）----


def _strip_tags(raw: str) -> str:
    """script/style 剥离 + 标签剥离 + 实体归一 + 空白折叠（清洗管线的通用尾段）。"""
    raw = re.sub(r"<script\b.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style\b.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def _extract_div_block(html: str, start: int) -> str:
    """从 ``start``（某 div 开标签处）做配平计数，返回该 div 的完整内层 HTML。"""
    open_re = re.compile(r"<div\b", re.I)
    close_re = re.compile(r"</div>", re.I)
    pos = start
    depth = 0
    end = len(html)
    while pos < len(html):
        o = open_re.search(html, pos)
        c = close_re.search(html, pos)
        if c is None:
            break
        if o is not None and o.start() < c.start():
            depth += 1
            pos = o.end()
        else:
            depth -= 1
            pos = c.end()
            if depth == 0:
                end = pos
                break
    return html[start:end]


def extract_boda(html: str) -> tuple[str, str]:
    """博达 CMS 正文提取：``v_news_content`` 选择器优先（取最长 content 区），
    无选择器回退 ``edu-main-content`` 区纯文本；返回 ``(title, text)``。

    提取失败/无正文返回 ``("", "")``。纯文本行保留原换行结构（切块滑窗
    行感知断点依赖行边界），段间以单个换行分隔。
    """
    # 标题：<h1> 优先，回退 <title> 剥站点后缀
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if m:
        title = _strip_tags(m.group(1))
    else:
        t = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        title = _strip_tags(t.group(1)) if t else ""
        for suffix in _TITLE_SUFFIXES:
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
                break

    # 正文：全部 v_news_content 区取最长（页脚联系块同名类，必须择优而非取首）
    blocks: list[str] = []
    for m in re.finditer(r'<div[^>]+class="v_news_content"[^>]*>', html, re.I):
        blocks.append(_extract_div_block(html, m.start()))
    if not blocks:
        m = re.search(r'<div[^>]+class="[^"]*edu-main-content[^"]*"[^>]*>', html, re.I)
        if m:
            blocks.append(_extract_div_block(html, m.start()))

    best = max(blocks, key=len) if blocks else ""
    # 段落结构保留：块级闭合标签折为换行再剥离剩余标签
    body = re.sub(r"<script\b.*?</script>", " ", best, flags=re.S | re.I)
    body = re.sub(r"<style\b.*?</style>", " ", body, flags=re.S | re.I)
    body = re.sub(r"</(?:p|div|li|tr|h[1-6]|section)>", "\n", body, flags=re.I)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html_lib.unescape(body)
    lines = [re.sub(r"\s+", " ", line).strip() for line in body.split("\n")]
    text = "\n".join(line for line in lines if line)
    if title or text:
        return title, text
    return "", ""


# ---- 第 3 步：切块（K1 纯函数路由）+ 第 4 步：向量化与落库 ----


def page_content_hash(text: str) -> str:
    """页面级判重哈希：清洗后全文 sha256（同文档所有 chunk 行冗余同值）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _ensure_kb_fts(session: AsyncSession) -> None:
    """幂等 ensure FTS 虚表 + 三触发器（内存库/新库防御性自建，SQLite 专有）。

    迁移 0005 已建的库此处零操作（sqlite_master 先查后建）；对齐 K4
    空库用例教训：``Base.metadata.create_all`` 建不出虚表。
    """
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect != "sqlite":
        return  # PG 无 FTS5（迁移 0005 同守卫），全文检索方案另立项
    master = (await session.execute(text("SELECT name FROM sqlite_master"))).scalars().all()
    names = set(master)
    if "kb_chunk_fts" not in names:
        await session.execute(text(KB_FTS_TABLE_DDL))
    for trigger_name, ddl in KB_FTS_TRIGGER_DDLS:
        if trigger_name not in names:
            await session.execute(text(ddl))


async def run_ingest(
    session: AsyncSession,
    entries: list[SeedEntry],
    *,
    settings: Settings,
    embedder: EmbeddingClient,
    corpus_version: str,
    fetcher: Callable[[str], Awaitable[str]],  # url → html（真网络抓取或本地 fixture 读取）
) -> IngestReport:
    """入库主流程（v1.6 ⑤）：抓取 → 提取 → 切块 → embedding → **单事务**落库。

    一次 session 贯穿全程、由调用方 commit；任一步失败向上抛（整体回滚）。
    幂等：同 doc_id + 同 content_hash → skip（零写入零 embedding）；
    同 doc_id + 异 content_hash → 整页删除重插。
    """
    report = IngestReport()
    now = datetime.now(UTC)
    drafts_by_doc: list[tuple[SeedEntry, str, str, str, list]] = []  # (entry, doc_id, title, hash, drafts)

    # ---- 抓取 + 提取 + 切块（确定性纯计算，无写入）----
    for entry in entries:
        html = await fetcher(entry.url)
        title, body = extract_boda(html)
        if len(body.replace("\n", "").strip()) < MIN_CONTENT_CHARS:
            report.skipped_short += 1
            report.skipped_list.append(f"{entry.url} (<{MIN_CONTENT_CHARS} 字符壳页)")
            continue
        doc_id = make_doc_id(entry.url)
        content_hash = page_content_hash(body)
        chunks = _STRATEGY_FNS[entry.strategy](
            body, max_chars=settings.kb_chunk_max_chars
        )
        drafts_by_doc.append((entry, doc_id, title, content_hash, chunks))
        report.fetched += 1

    if not drafts_by_doc:
        return report

    # ---- 幂等裁决：与库内既有行按 (doc_id, content_hash) 比对 ----
    doc_ids = [doc_id for _, doc_id, _, _, _ in drafts_by_doc]
    existing: dict[str, tuple[str, list[KbChunk]]] = {}  # doc_id → (page_hash, 任意一行实体)
    if doc_ids:
        rows = (await session.execute(
            select(KbChunk).where(KbChunk.doc_id.in_(doc_ids))
        )).scalars().all()
        for row in rows:
            existing.setdefault(row.doc_id, (row.content_hash, []))
            existing[row.doc_id][1].append(row)

    to_write: list[tuple[SeedEntry, str, str, str, list]] = []
    for item in drafts_by_doc:
        entry, doc_id, _title, content_hash, _chunks = item
        prior = existing.get(doc_id)
        if prior is not None and prior[0] == content_hash:
            report.skipped_unchanged += 1  # 同版本重跑铁证路径：零写入零 embedding
            continue
        if prior is not None:
            for row in prior[1]:  # ORM 实体删除：identity map 正确驱逐（同 session 重跑安全）
                await session.delete(row)
            await session.flush()  # 先执行删除再插新行：(doc_id, section_idx) 唯一约束
            report.updated += 1
        else:
            report.inserted += 1
        to_write.append(item)

    if not to_write:
        return report

    # ---- embedding（新变文档全量文本一次批量调用；失败上抛 → 整体回滚）----
    texts = [chunk.text for _e, _d, _t, _h, chunks in to_write for chunk in chunks]
    vectors = await embedder.embed_texts(texts)
    if len(vectors) != len(texts):
        raise RuntimeError(f"embedding count mismatch: {len(vectors)} != {len(texts)}")

    # ---- 装配 KbChunk 行（provenance 100% 非空：v1.6 ⑦）----
    cursor = 0
    for entry, doc_id, title, content_hash, chunks in to_write:
        for chunk in chunks:
            vec = vectors[cursor]
            cursor += 1
            session.add(
                KbChunk(
                    doc_id=doc_id,
                    section_idx=chunk.section_idx,
                    anchor=chunk.anchor,
                    title=title or entry.url,
                    content=chunk.text,
                    embedding=np.asarray(vec, dtype=np.float32).tobytes(),
                    embedding_model=settings.kb_embedding_model,
                    content_hash=content_hash,
                    source_url=entry.url,
                    column=entry.column,
                    corpus_version=corpus_version,
                    fetched_at=now,
                    synced_at=now,
                )
            )
            report.chunks += 1
    return report


# ---- 抓取器工厂（真机档 / fixture 档）----


def make_fetcher(
    mode: str, settings: Settings, client: httpx.AsyncClient | None
):
    """返回 ``async (url) -> html`` 抓取器。

    - live/dry-run：httpx 网络抓取（域白名单 + 限速 kb_request_interval_seconds）；
    - fixture：url 即本地 HTML 文件路径，零网络零限速。
    """
    if mode == "fixture":
        async def _read_file(url: str) -> str:
            return Path(url).read_text(encoding="utf-8")

        return _read_file

    async def _fetch(url: str) -> str:
        host = url.split("/")[2] if "://" in url else ""
        if host != "hnist.cn" and not host.endswith(ALLOWED_DOMAIN_SUFFIXES):
            raise ValueError(f"URL 域白名单外: {host}")
        await asyncio.sleep(settings.kb_request_interval_seconds)  # 礼貌限速
        return await fetch_html(url, settings, client)

    return _fetch


def load_seed(path: str | Path) -> list[SeedEntry]:
    """种子 YAML → SeedEntry 列表（Pydantic 校验，禁止裸 dict）。"""
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"种子文件必须是条目列表: {path}")
    return [SeedEntry.model_validate(item) for item in raw]


async def _amain(args: argparse.Namespace) -> int:
    settings = default_settings
    entries = load_seed(args.seed)
    mode = "fixture" if args.fixture else "live"
    embedder: EmbeddingClient = FakeEmbedder() if args.fixture else ArkEmbeddingClient(settings=settings)
    client = None if args.fixture else httpx.AsyncClient(
        headers={"User-Agent": CRAWLER_USER_AGENT},
        timeout=httpx.Timeout(CRAWLER_TIMEOUT_SECONDS),
        follow_redirects=True,
    )
    fetcher = make_fetcher(mode, settings, client)

    engine = make_engine(args.db)  # 缺省 settings.db_url
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:  # 建表（幂等）+ FTS ensure 在同连接
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            await _ensure_kb_fts(session)
            report = await run_ingest(
                session, entries,
                settings=settings, embedder=embedder,
                corpus_version=args.version or settings.kb_corpus_version,
                fetcher=fetcher,
            )
            await session.commit()  # 单事务：全程一次 commit
        print(
            f"[ingest] mode={mode} fetched={report.fetched} inserted={report.inserted} "
            f"updated={report.updated} skipped_unchanged={report.skipped_unchanged} "
            f"skipped_short={report.skipped_short} chunks={report.chunks}"
        )
        for item in report.skipped_list:
            print(f"  [skipped] {item}")

        # AC-18e provenance 校验：非空 100% 才算通过
        async with factory() as session:
            total = (await session.execute(select(KbChunk.id))).scalars().all()
            bad = (await session.execute(
                select(KbChunk.id).where(
                    (KbChunk.source_url == "") | (KbChunk.source_url.is_(None))
                    | (KbChunk.corpus_version == "") | (KbChunk.corpus_version.is_(None))
                    | (KbChunk.fetched_at.is_(None))
                )
            )).scalars().all()
            print(f"[provenance] rows={len(total)} non-empty-violations={len(bad)}")
        return 0
    finally:
        if client is not None:
            await client.aclose()
        if not args.fixture and isinstance(embedder, ArkEmbeddingClient):
            await embedder.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识库语料入库管线（文档 18 §三 v1.6）")
    parser.add_argument("--seed", required=True, help="种子 YAML 路径（SeedEntry 列表）")
    parser.add_argument("--dry-run", action="store_true", help="只抓取+提取+切块+报告，不落库不调 embedding")
    parser.add_argument("--fixture", action="store_true", help="fixture 档：url=本地 HTML 文件 + FakeEmbedder（零网络）")
    parser.add_argument("--version", default=None, help="语料版本锚（缺省 settings.kb_corpus_version）")
    parser.add_argument("--db", default=None, help="数据库连接串（缺省 settings.db_url）")
    args = parser.parse_args(argv)

    # D5 门控：kb_enabled=False 时 ingest CLI 拒绝运行（dry-run 同门，能力语义一致）
    if not default_settings.kb_enabled:
        print("拒绝运行：kb_enabled=False（D5 门控——能力上线是运营决策）。"
              "临时开启请用 KB_ENABLED=true 前缀运行。")
        return 2

    if args.dry_run:
        # dry-run：网络抓取与提取照跑，零 DB 零 embedding
        return asyncio.run(_dry_run(args))
    return asyncio.run(_amain(args))


async def _dry_run(args: argparse.Namespace) -> int:
    settings = default_settings
    entries = load_seed(args.seed)
    client = httpx.AsyncClient(
        headers={"User-Agent": CRAWLER_USER_AGENT},
        timeout=httpx.Timeout(CRAWLER_TIMEOUT_SECONDS),
        follow_redirects=True,
    )
    try:
        # 与正式档同源抓取器：域白名单 + 限速照走（种子写错域 dry-run 即报错，
        # 不留到正式跑才炸）
        fetcher = make_fetcher("live", settings, client)
        report = IngestReport()
        total_chunks = 0
        for entry in entries:
            html = await fetcher(entry.url)
            _title, body = extract_boda(html)
            if len(body.replace("\n", "").strip()) < MIN_CONTENT_CHARS:
                report.skipped_short += 1
                report.skipped_list.append(f"{entry.url} (<{MIN_CONTENT_CHARS} 字符壳页)")
                continue
            chunks = _STRATEGY_FNS[entry.strategy](body, max_chars=settings.kb_chunk_max_chars)
            report.fetched += 1
            total_chunks += len(chunks)
        print(
            f"[dry-run] fetched={report.fetched} chunks={total_chunks} "
            f"skipped_short={report.skipped_short}（不落库不调 embedding）"
        )
        for item in report.skipped_list:
            print(f"  [skipped] {item}")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    started = time.perf_counter()
    code = main()
    print(f"[ingest] done in {time.perf_counter() - started:.1f}s")
    sys.exit(code)
