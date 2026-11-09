"""黄金集运行器（任务设计 17 §五）：装配 → 逐用例执行 → 产物落盘。

复用既有基建，不新造轮子（用户裁决 2026-11-02）：
- 服务装配沿用 tests/conftest.py 的 ``make_services`` + 桩适配器模式
  （eval 本地实现状态驱动版，不反向依赖 tests/）；
- 工具调用轨迹读 ``audit_log``（tools/base.dispatch 每次调用本就落审计行，
  这就是现成 trace 源，不另做录制器）；
- 两档运行沿用 --live 惯例：默认 FakeLLM 脚本化（CI 可跑，验证管线与
  红线/降级类），``--live`` 接真机 ArkLLMClient（验证 tool_route 实际路由质量）；
- 产物写 ``eval/results/<日期>.json``（同日多次运行按 run 追加）。

桩状态语义（17 §三 StubSetup）：OK/EMPTY/STALE/FAIL 均模拟**降级链终态**
（链路内部过程——缓存命中、重试、stale 判定——由 tests/test_degrade_and_stale
单测覆盖，黄金集只观测编排层对终态的行为，边界不重叠）。

运行（backend 目录下）：``python -m eval.run_eval [--live] [--only case-id]``
"""

from __future__ import annotations

import argparse
import json
import platform
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import select, text

from app.cache import TTLCache
from app.config import Settings
from app.conversation import SessionStore
from app.db import make_engine
from app.kb import KB_FTS_TABLE_DDL, KB_FTS_TRIGGER_DDLS, FakeEmbedder, HybridRetriever, make_doc_id
from app.llm.adapter import ArkLLMClient
from app.orchestrator import EventType, LLMChunk, Orchestrator, ToolCallRequest
from app.orm import AuditLog, Base, KbChunk
from app.prompts import PROMPT_VERSION
from app.schemas.meta import AdapterResponse, DegradeStatus, ToolSource
from app.schemas.timetable import TimetableItem
from app.tools import build_registry
from app.tools.base import Services

from .cases import CASES, AdapterState, Case

STUDENT = "20230001"  # 与单测一致的测试学号（脱敏后落审计）
RESULTS_DIR = Path(__file__).parent / "results"


# ---------------------------------------------------------------------------
# 状态驱动桩适配器（模拟降级链终态）
# ---------------------------------------------------------------------------


class EvalEduAdapter:
    """教务桩：按 AdapterState 返回对应终态。"""

    source = ToolSource.EDU_SYSTEM

    def __init__(self, state: AdapterState) -> None:
        self.state = state
        self.calls = 0

    def _resp(self, data: list | None, status: DegradeStatus) -> AdapterResponse:
        synced_at = datetime.now(UTC)
        if status == DegradeStatus.STALE_CACHE:
            synced_at -= timedelta(hours=2)  # 旧同步点（远超 TTL）
        return AdapterResponse(
            data=data, degrade_status=status,
            synced_at=synced_at, source=self.source,
        )

    async def fetch_timetable(self, student_id, start_date, end_date, course_name=None):
        self.calls += 1
        match self.state:
            case AdapterState.FAIL:
                return self._resp(None, DegradeStatus.UNAVAILABLE)
            case AdapterState.STALE:
                return self._resp([self._item()], DegradeStatus.STALE_CACHE)
            case AdapterState.EMPTY:
                return self._resp([], DegradeStatus.LIVE_OK)
            case _:
                return self._resp([self._item()], DegradeStatus.LIVE_OK)

    async def fetch_grades(self, student_id, term=None, course_name=None):
        self.calls += 1
        match self.state:
            case AdapterState.FAIL:
                return self._resp(None, DegradeStatus.UNAVAILABLE)
            case AdapterState.STALE:
                return self._resp([], DegradeStatus.STALE_CACHE)
            case _:
                return self._resp([], DegradeStatus.LIVE_OK)

    @staticmethod
    def _item() -> TimetableItem:
        return TimetableItem(
            course_name="高等数学", course_code="MA101", teacher="张老师",
            location="一教-101", weekday=1, period_start=1, period_end=2,
            weeks="1-16 周",
        )

    async def aclose(self) -> None:
        return None


class EvalLibAdapter:
    """图书馆桩：借阅查询按状态返回；续借成功（写失败路径由单测覆盖）。"""

    source = ToolSource.LIBRARY_ILS

    def __init__(self, state: AdapterState) -> None:
        self.state = state
        self.loans_calls = 0
        self.renew_calls = 0  # 断言"未确认不触达源系统"的证据

    async def fetch_loans(self, student_id, include_history=False):
        self.loans_calls += 1
        synced_at = datetime.now(UTC)
        if self.state == AdapterState.FAIL:
            return AdapterResponse(
                data=None, degrade_status=DegradeStatus.UNAVAILABLE,
                synced_at=synced_at, source=self.source,
            )
        status = (
            DegradeStatus.STALE_CACHE
            if self.state == AdapterState.STALE
            else DegradeStatus.LIVE_OK
        )
        if status == DegradeStatus.STALE_CACHE:
            synced_at -= timedelta(hours=2)
        # OK 态返回一条在借（与 confirm 用例的条码 01020 呼应）——live 档模型
        # 需要看到真实在借才能走"预览→确认→续借"协议；空清单会让真机如实
        # 拒绝续借（2026-11-02 实测），场景即不成立。fake 档脚本不读数据，无影响。
        from app.schemas.library import LibraryLoanItem

        items = [] if self.state == AdapterState.EMPTY else [
            LibraryLoanItem(
                title="三体", barcode="01020",
                due_date=date.today() + timedelta(days=3),
                overdue_days=0, renewable=True,
            )
        ]
        return AdapterResponse(
            data=items, degrade_status=status,
            synced_at=synced_at, source=self.source,
        )

    async def renew(self, student_id, barcodes, idempotency_key):
        self.renew_calls += 1
        from app.schemas.library import LibraryRenewItem

        items = [
            LibraryRenewItem(
                barcode=b, ok=True,
                new_due_date=date.today() + timedelta(days=14),  # 相对时间，防日期炸弹
            )
            for b in barcodes
        ]
        return AdapterResponse(
            data=items, degrade_status=DegradeStatus.LIVE_OK,
            synced_at=datetime.now(UTC), source=self.source,
        ), None

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# LLM 桩（脚本化 / 预算循环）
# ---------------------------------------------------------------------------


class ScriptLLM:
    """每次 stream() 弹一段脚本；记录调用供红线类断言"LLM 零参与"。"""

    def __init__(self, script: list[list[LLMChunk]]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def stream(self, messages, tools=None):
        self.calls.append({"tools": tools})
        for chunk in self.script.pop(0):
            yield chunk


class LoopLLM:
    """无限请求工具（驱动预算熔断）；收尾请求（tools=None）时给文本。

    携带合法参数——真机循环发出的调用参数是合法的（否则编排层参数校验
    会先拒绝，见 tools/base.dispatch 的审计边界说明）。
    """

    def __init__(self, tool: str = "timetable_query") -> None:
        self.tool = tool
        self.calls: list[dict] = []

    async def stream(self, messages, tools=None):
        self.calls.append({"tools": tools})
        if tools is None:  # 熔断后的收尾请求
            yield LLMChunk(delta="查询步骤较多未能完成，请拆分问题。", finish_reason="stop")
            return
        yield LLMChunk(
            tool_calls=[ToolCallRequest(
                id=f"loop_{len(self.calls)}", name=self.tool,
                arguments=json.dumps(_TOOL_ARGS.get(self.tool, {}), ensure_ascii=False),
            )],
            finish_reason="tool_calls",
        )


# ---------------------------------------------------------------------------
# 脚本构造（fake 档）
# ---------------------------------------------------------------------------

# 脚本化调用的最小合法参数（student_id 由编排层注入，脚本不填）。
_TOOL_ARGS: dict[str, dict] = {
    "timetable_query": {"start_date": "2026-11-02", "end_date": "2026-11-08"},
    "grade_query": {},
    "library_loans": {},
    "reminder_subscribe": {"subject": "library_due"},
    "library_renew": {"barcodes": ["01020"]},
    "kb_search": {"query": "转专业条件"},
}


def _tool_stream(name: str, call_id: str) -> list[LLMChunk]:
    return [
        LLMChunk(
            tool_calls=[ToolCallRequest(
                id=call_id, name=name,
                arguments=json.dumps(_TOOL_ARGS.get(name, {}), ensure_ascii=False),
            )],
            finish_reason="tool_calls",
        )
    ]


def _text_stream(text: str) -> list[LLMChunk]:
    return [LLMChunk(delta=text, finish_reason="stop")]


def build_script(case: Case) -> list[list[LLMChunk]]:
    """按用例结构构造脚本化回复（fake 档专用）。

    红线类返回空脚本——红线门在 LLM 之前拦截，脚本被消费即属异常，
    运行器会把 llm_calls 记进产物供判定器核查"零参与"。
    """
    if case.klass == "redline":
        return []

    # confirm 两轮协议：r1 预览（loans + renew，renew 被门禁拒）→ r2 确认放行。
    # 注意脚本形状：一次 stream() = 一次 LLM 轮次，编排层只看末 chunk 的
    # tool_calls——工具调用与收尾文本必须是独立流，不能拼进同一流。
    if case.id == "confirm-renew-round-trip":
        return [
            _tool_stream("library_loans", "c1"),
            _tool_stream("library_renew", "c2"),
            _text_stream("请回复「确认续借」以完成办理。"),
            _tool_stream("library_renew", "c3"),
            _text_stream("已为你完成续借。"),
        ]
    if case.klass == "confirm":
        return [
            _tool_stream("library_renew", "c1"),
            _text_stream("续借需要先确认，请查看在借清单后回复确认。"),
        ]

    # 其余：按期望依次发出工具调用，再给收尾文本
    calls = [
        ce.tool for ce in case.expect.tools for _ in range(ce.count)
    ]
    streams = [
        _tool_stream(name, f"c{i + 1}") for i, name in enumerate(calls)
    ]
    streams.append(_text_stream("已为你查询完毕。"))
    return streams


# ---------------------------------------------------------------------------
# 装配与运行
# ---------------------------------------------------------------------------


@asynccontextmanager
async def make_db():
    """函数级内存库（对齐 tests/conftest.py 的 db 夹具）。"""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _settings(live: bool, case: Case) -> Settings:
    """运行档位设置：fake 档 llm_model 标脚本桩（归因不撒谎）；kb 类与预灌语料
    用例强制开启 kb 门控（工具注册是场景前提，18 附录 B v1.5）。"""
    if live:
        return Settings()
    return Settings(
        llm_model="fake-script",
        kb_enabled=case.stub_setup.kb_corpus or case.klass == "kb",
    )


# kb 语料夹具（18 附录 B）：与 test_kb_retriever 同源的转专业条款块。
_KB_CLAUSE = (
    "第一条 转专业条件：平均学分绩点排名专业前百分之二十，"
    "转专业申请于每学期第二周提交。"
)
_KB_URL = "https://jwc.hnist.cn/gzzd/xjgl.htm"


async def _ensure_kb_fts(db) -> None:
    """建 FTS 虚表 + 触发器（单源 DDL，迁移 0005 同款）。"""
    await db.execute(text(KB_FTS_TABLE_DDL))
    for _, ddl in KB_FTS_TRIGGER_DDLS:
        await db.execute(text(ddl))
    await db.commit()


async def _preload_kb(db) -> None:
    """预灌固定语料：条款块 + FakeEmbedder 确定性向量（零网络，hybrid 双路成立）。"""
    now = datetime.now(UTC)
    (vec,) = await FakeEmbedder().embed_texts([_KB_CLAUSE])
    db.add(
        KbChunk(
            doc_id=make_doc_id(_KB_URL),
            section_idx=0,
            anchor="第一条",
            title="转专业实施办法",
            content=_KB_CLAUSE,
            embedding=np.asarray(vec, dtype=np.float32).tobytes(),
            embedding_model="doubao-embedding",
            content_hash="0" * 64,
            source_url=_KB_URL,
            column="教务处/规章制度",
            corpus_version="2026-11-05.1",
            fetched_at=now,
            synced_at=now,
        )
    )
    await db.commit()


async def _audit_trace(db, session_id: str) -> dict:
    """从 audit_log 读本会话的工具调用轨迹（现成 trace 源）+ 其他动作计数。"""
    rows = list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.session_id == session_id)
                .order_by(AuditLog.id)
            )
        ).scalars().all()
    )
    tool_calls = [
        {
            "tool": r.tool_name, "result": r.result,
            "latency_ms": r.latency_ms, "ts": r.ts.isoformat(),
        }
        for r in rows
        if r.action == "tool_call"
    ]
    other = Counter(r.action for r in rows if r.action != "tool_call")
    return {"tool_calls": tool_calls, "other_actions": dict(other)}


async def run_case(case: Case, *, live: bool) -> dict:
    """执行单条用例（1~2 轮协议），返回可 JSON 化的运行产物。"""
    edu = EvalEduAdapter(case.stub_setup.edu)
    lib = EvalLibAdapter(case.stub_setup.lib)
    async with make_db() as db:
        settings = _settings(live, case)
        if settings.kb_enabled:
            # FTS 虚表独立于语料行（内存库 create_all 不建虚表；空库诚实用例
            # 也必须有表，否则 FTS 查询直接 OperationalError）
            await _ensure_kb_fts(db)
        if case.stub_setup.kb_corpus:
            await _preload_kb(db)
        store = SessionStore(db, settings)
        services = Services(
            cache=TTLCache(),
            settings=settings, edu=edu, lib=lib, db=db,
            idempotency={}, renew_session_count={},
            # 显式注入 FakeEmbedder 桩（覆盖 Services 懒装配的真机档，零网络）
            kb_retriever=(
                HybridRetriever(settings, embedder=FakeEmbedder())
                if settings.kb_enabled
                else None
            ),
        )
        registry = build_registry(services)
        if live:
            client = ArkLLMClient()
            llm_calls: int | None = None
        elif case.llm_loop:
            client = LoopLLM()
            llm_calls = None  # 循环次数运行后回填
        else:
            client = ScriptLLM(build_script(case))
            llm_calls = None  # 运行后回填

        orch = Orchestrator(store, registry, client, settings=settings)

        rounds: list[dict] = []
        session_id: str | None = None
        inputs = [case.user_message] + (
            [case.followup_message] if case.followup_message else []
        )
        for user_input in inputs:
            events = [
                ev async for ev in orch.run(STUDENT, user_input, session_id)
            ]
            session_id = next(
                (
                    ev.data.get("session_id")
                    for ev in events
                    if ev.data.get("session_id")
                ),
                session_id,
            )
            rounds.append({
                "user_input": user_input,
                "events": [
                    {"type": ev.type.value, "data": ev.data} for ev in events
                ],
                "final_text": "".join(
                    ev.data["delta"]
                    for ev in events
                    if ev.type == EventType.TOKEN
                ),
            })

        if not live:
            llm_calls = len(client.calls)

        return {
            "case_id": case.id,
            "klass": case.klass,
            "session_id": session_id,
            "rounds": rounds,
            "audit": await _audit_trace(db, session_id),
            "stub": {
                "edu_calls": edu.calls,
                "lib_loans_calls": lib.loans_calls,
                "lib_renew_calls": lib.renew_calls,
            },
            "llm_calls": llm_calls,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="WAgent 黄金集运行器（17 §五）")
    parser.add_argument("--live", action="store_true",
                        help="接真机 LLM（默认脚本化 FakeLLM，CI 可跑）")
    parser.add_argument("--only", metavar="CASE_ID", help="只跑指定用例（调试用）")
    args = parser.parse_args()

    if args.live:
        probe = ArkLLMClient()
        if not probe.is_configured:
            print("LLM 未配置（LLM_API_BASE/LLM_API_KEY/LLM_MODEL），"
                  "真机档不可用；去掉 --live 用脚本档验证管线。")
            return 2

    # live 档跳过 fake_only 用例（脚本桩专属场景，真机行为不等价）
    cases = [c for c in CASES if not (args.live and c.fake_only)]
    if args.only:
        cases = [c for c in cases if c.id == args.only]
        if not cases:
            print(f"未找到用例：{args.only}")
            return 2

    import asyncio

    records = [asyncio.run(run_case(c, live=args.live)) for c in cases]

    run_record = {
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "mode": "live" if args.live else "fake",
        "prompt_version": PROMPT_VERSION,
        "llm_model": _settings(args.live, CASES[0]).llm_model,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cases": records,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    # live 档单独命名（live-<日期>.json）：fake 产物只做门禁比对不入库，
    # live 结果按用户裁决提交 eval/results/ 留档（.gitignore 白名单放行）。
    prefix = "live-" if args.live else ""
    out = RESULTS_DIR / f"{prefix}{date.today().isoformat()}.json"
    history: list = []
    if out.exists():
        history = json.loads(out.read_text(encoding="utf-8"))
    history.append(run_record)
    out.write_text(
        json.dumps(history, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # 运行摘要（判定归 score.py，这里只给人看的概况）
    print(f"产物：{out}（第 {len(history)} 次运行，mode={run_record['mode']}，"
          f"prompt_version={PROMPT_VERSION}）")
    by_klass = Counter(r["klass"] for r in records)
    print(f"用例：{len(records)} 条 {dict(by_klass)}")
    for r in records:
        last = r["rounds"][-1]["events"]
        done = next((e for e in reversed(last) if e["type"] == "done"), None)
        flags = []
        if done:
            d = done["data"]
            if d.get("blocked"):
                flags.append("blocked")
            if d.get("budget_exhausted"):
                flags.append("budget_exhausted")
        tools = Counter(t["tool"] for t in r["audit"]["tool_calls"])
        errs = sum(1 for t in r["audit"]["tool_calls"] if t["result"] != "ok")
        print(f"  {r['case_id']:<34} tools={dict(tools) or '-'} "
              f"err={errs} {' '.join(flags)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
