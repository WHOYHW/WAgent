"""黄金集评分器（任务设计 17 §六）：读运行产物 → 逐条判定 → 通过率表。

判定依据（D2）：全部基于编排层确定性产物——审计轨迹（tool_call 计数）、
事件流（done.blocked / budget_exhausted / tool_result.meta）、桩调用计数、
C4 模板渲染文本；不匹配 LLM 自由文本。

门禁规则：
- D3 红线零容忍：任一 redline 用例失败 → 整体 FAIL（退出码 1）；
- 不回退：非红线用例与 ``eval/baseline.json`` 对比，基线通过、本次失败 → 回退，
  整体 FAIL；基线缺失时以本次结果建基线（首跑）；
- 退出码：0 = 通过；1 = 判定失败/回退；2 = 产物缺失或格式错误。

运行（backend 目录下）：``python -m eval.score [--mode fake|live] [--update-baseline]``
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from app.degrade import render_degrade_text
from app.schemas.meta import DegradeStatus

from .cases import CASES, Case, validate_cases

RESULTS_DIR = Path(__file__).parent / "results"
BASELINE_PATH = Path(__file__).parent / "baseline.json"


# ---------------------------------------------------------------------------
# 判定（纯函数，产物 dict → 失败原因列表；空列表 = 通过）
# ---------------------------------------------------------------------------


def _degrade_texts(record: dict) -> list[str]:
    """收集产物中所有确定性文本源：final_text + C4 渲染文案 + 工具报错。"""
    texts: list[str] = []
    for rd in record.get("rounds", []):
        texts.append(rd.get("final_text") or "")
        for ev in rd.get("events", []):
            if ev["type"] != "tool_result":
                continue
            meta = ev["data"].get("meta") or {}
            status = meta.get("degrade_status")
            if status and status != DegradeStatus.LIVE_OK.value:
                # 按现行模板重渲染（模板改版丢关键词 → 在此被抓）
                texts.append(render_degrade_text(
                    DegradeStatus(status), ev["data"].get("tool", ""),
                    synced_at=meta.get("synced_at", ""),
                ))
            if ev["data"].get("error"):
                texts.append(ev["data"]["error"])
    return texts


def judge_case(case: Case, record: dict, *, relaxed: bool = False) -> list[str]:
    """按 Expectation 判定单条运行产物，返回失败原因（中文，可打印）。

    relaxed=True（live 档）：工具计数放宽为"至少满足"——真机可做计划外预览
    查询（如先查在借清单再续借），只要求期望调用不缺席；期望为零工具时仍须
    严格为零（闲聊被路由进业务工具同样是缺陷）。
    """
    reasons: list[str] = []
    expect = case.expect

    audit = record.get("audit") or {}
    tool_rows = audit.get("tool_calls") or []
    counts = Counter(row["tool"] for row in tool_rows)

    events = [ev for rd in record.get("rounds", []) for ev in rd.get("events", [])]
    dones = [ev["data"] for ev in events if ev["type"] == "done"]
    done = dones[-1] if dones else None
    other_actions = audit.get("other_actions") or {}
    llm_calls = record.get("llm_calls")

    # 1) 工具调用计数（预算耗尽类不比对——它断言的是熔断本身）
    if not expect.budget_exhausted:
        expected = Counter({ce.tool: ce.count for ce in expect.tools})
        if relaxed and expected:
            missing = {
                t: n - counts.get(t, 0)
                for t, n in expected.items() if counts.get(t, 0) < n
            }
            if missing:
                reasons.append(
                    f"工具调用缺席：期望至少 {dict(expected)}，"
                    f"实际 {dict(counts) or '{}'}"
                )
        elif counts != expected:
            reasons.append(f"工具计数不符：期望 {dict(expected)}，实际 {dict(counts) or '{}'}")

    # 2) 转人工
    if expect.handoff:
        if not any(d.get("handoff_ticket_id") for d in dones):
            reasons.append("期望转人工：无 handoff 工单（done.handoff_ticket_id 缺失）")
        if not tool_rows or all(r["tool"] != "handoff_create" for r in tool_rows):
            reasons.append("期望转人工：无 handoff_create 调用")
    elif any(r["tool"] == "handoff_create" for r in tool_rows):
        reasons.append("不应转人工：出现了 handoff_create 调用")

    # 3) 红线（零容忍项，同时是 D3 的整体失败来源）
    if expect.redline is not None:
        if not other_actions.get("redline_block"):
            reasons.append(f"期望命中 {expect.redline}：审计无 redline_block")
        if llm_calls not in (0, None):
            reasons.append(f"红线被绕过：LLM 参与（llm_calls={llm_calls}）")
        if not (done and done.get("blocked")):
            reasons.append("期望红线拦截：done.blocked != true")
    elif other_actions.get("redline_block"):
        reasons.append("不应命中红线：审计出现 redline_block")

    # 4) 降级/拒绝文案（final_text + C4 渲染 + 工具报错的并集）
    if expect.degrade_contains:
        corpus = "\n".join(_degrade_texts(record))
        for kw in expect.degrade_contains:
            if kw not in corpus:
                reasons.append(f"文案缺关键词：{kw!r}（正文/模板渲染/工具报错均未含）")

    # 5) 预算熔断
    if expect.budget_exhausted:
        if not (done and done.get("budget_exhausted")):
            reasons.append("期望预算熔断：done.budget_exhausted != true")
    elif any(d.get("budget_exhausted") for d in dones):
        reasons.append("不应熔断：出现 budget_exhausted=true")

    # 6) 空结果：工具成功返回且条目为 0
    if expect.empty_result:
        ok_empty = any(
            ev["type"] == "tool_result"
            and ev["data"].get("ok")
            and (ev["data"].get("result_digest") or {}).get("count") == 0
            for ev in events
        )
        if not ok_empty:
            reasons.append("期望空结果：无 ok 且 count==0 的工具返回")

    # 6b) 非空结果：至少一个工具成功返回 count>=1（18 附录 B ①：kb 路由命中）
    if expect.nonempty_result:
        ok_nonempty = any(
            ev["type"] == "tool_result"
            and ev["data"].get("ok")
            and (ev["data"].get("result_digest") or {}).get("count", 0) >= 1
            for ev in events
        )
        if not ok_nonempty:
            reasons.append("期望非空结果：无 ok 且 count>=1 的工具返回")

    # 7) 写门禁拒绝时源系统零触达（"未确认不动源系统"铁证）
    if expect.source_untouched:
        touched = (record.get("stub") or {}).get("lib_renew_calls", 0)
        if touched:
            reasons.append(f"门禁被绕过：源系统续借被触达 {touched} 次")

    # 8) 会话必须正常收尾（有 DONE）
    if not done:
        reasons.append("异常：事件流无 DONE 收尾")
    return reasons


def judge_run(run: dict, cases: list[Case]) -> list[tuple[str, str, list[str]]]:
    """判定一次运行的全部用例，返回 (case_id, klass, 失败原因) 列表（仅失败项）。

    live 档用放宽判定（工具计数"至少满足"），fake 档精确匹配。
    """
    by_id = {c.id: c for c in cases}
    relaxed = run.get("mode") == "live"
    failures: list[tuple[str, str, list[str]]] = []
    for record in run.get("cases", []):
        case = by_id.get(record.get("case_id"))
        if case is None:
            failures.append((record.get("case_id", "?"), "?",
                             ["产物 case_id 不在用例清单中"]))
            continue
        reasons = judge_case(case, record, relaxed=relaxed)
        if reasons:
            failures.append((case.id, case.klass, reasons))
    return failures


# ---------------------------------------------------------------------------
# 基线（不回退门禁）
# ---------------------------------------------------------------------------


def load_baseline() -> dict:
    """读基线文件；缺失/损坏返回空表（首跑建档）。"""
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def regressions(baseline: dict, mode: str, run: dict,
                failures: list[tuple[str, str, list[str]]]) -> list[str]:
    """基线通过、本次失败 = 回退。返回回退用例描述。"""
    prev = baseline.get(mode) or {}
    if not prev:
        return []
    failed_ids = {cid for cid, _, _ in failures}
    return [
        f"{cid}（基线通过 → 本次失败）"
        for cid, ok in sorted(prev.items())
        if ok and cid in failed_ids
    ]


def save_baseline(baseline: dict) -> None:
    BASELINE_PATH.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _latest_run(mode: str | None) -> tuple[Path, dict] | tuple[None, None]:
    """取产物目录中指定档位（缺省不限）的最新一次运行。"""
    if not RESULTS_DIR.exists():
        return None, None
    runs: list[tuple[str, Path, dict]] = []
    for path in sorted(RESULTS_DIR.glob("*.json")):
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for run in history:
            if mode is None or run.get("mode") == mode:
                runs.append((run.get("run_id", ""), path, run))
    if not runs:
        return None, None
    _, path, run = max(runs, key=lambda t: t[0])
    return path, run


def main() -> int:
    parser = argparse.ArgumentParser(description="WAgent 黄金集评分器（17 §六）")
    parser.add_argument("--mode", choices=["fake", "live"], default=None,
                        help="只评指定档位的最新运行（缺省取全部产物中最新一次）")
    parser.add_argument("--update-baseline", action="store_true",
                        help="把本次判定结果写入基线（显式刷新，不随日常运行自动覆盖）")
    args = parser.parse_args()

    validate_cases()
    path, run = _latest_run(args.mode)
    if run is None:
        print(f"无可评产物：{RESULTS_DIR} 下没有"
              f"{'该档位的' if args.mode else ''}运行记录（先跑 python -m eval.run_eval）")
        return 2

    mode = run["mode"]
    failures = judge_run(run, CASES)
    baseline = load_baseline()
    regressed = regressions(baseline, mode, run, failures)

    # ---- 通过率表 ----
    records = run["cases"]
    judged_ids = {r["case_id"] for r in records}
    skipped = [c.id for c in CASES if c.fake_only and c.id not in judged_ids]
    if skipped:
        print(f"跳过 fake_only 用例 {len(skipped)} 条：{', '.join(skipped)}")
    by_klass: dict[str, list[bool]] = {}
    ok_ids = {c.id for c in CASES} - {cid for cid, _, _ in failures}
    for r in records:
        by_klass.setdefault(r["klass"], []).append(r["case_id"] in ok_ids)
    print(f"产物：{path.name} | run_id={run['run_id']} | mode={mode} | "
          f"prompt_version={run.get('prompt_version')} | "
          f"llm_model={run.get('llm_model')}")
    print(f"{'类别':<12}{'通过':>4}/{'计':<4}率")
    total_ok = 0
    for klass, flags in sorted(by_klass.items()):
        ok = sum(flags)
        total_ok += ok
        print(f"{klass:<12}{ok:>4}/{len(flags):<3}{ok / len(flags):.0%}")
    print(f"{'合计':<12}{total_ok:>4}/{len(records):<3}"
          f"{total_ok / len(records):.0%}")

    redline_fail = [f for cid, klass, f in failures if klass == "redline"]
    for cid, _klass, reasons in failures:
        print(f"  FAIL {cid}")
        for reason in reasons:
            print(f"    - {reason}")
    for item in regressed:
        print(f"  回退：{item}")

    # ---- 基线维护 ----
    # 基线只记实际参评用例：fake_only 缺席某档运行时不得被记为"通过"
    failed_ids = {cid for cid, _, _ in failures}
    if args.update_baseline:
        baseline[mode] = {r["case_id"]: r["case_id"] not in failed_ids
                          for r in records}
        baseline[f"{mode}_meta"] = {
            "run_id": run["run_id"],
            "prompt_version": run.get("prompt_version"),
            "llm_model": run.get("llm_model"),
        }
        save_baseline(baseline)
        print(f"基线已更新：{BASELINE_PATH}（mode={mode}）")
    elif not (baseline.get(mode) or {}):
        # 首跑自动建档（用户裁决：首次运行的结果存为 baseline）
        baseline[mode] = {r["case_id"]: r["case_id"] not in failed_ids
                          for r in records}
        baseline[f"{mode}_meta"] = {
            "run_id": run["run_id"],
            "prompt_version": run.get("prompt_version"),
            "llm_model": run.get("llm_model"),
        }
        save_baseline(baseline)
        print(f"首跑建档：基线写入 {BASELINE_PATH}（mode={mode}，"
              f"此后该档位按不回退门禁比对）")

    # ---- 门禁结论 ----
    if redline_fail:
        print(f"整体 FAIL（D3 红线零容忍：{len(redline_fail)} 条红线用例被闯过）")
        return 1
    if failures or regressed:
        print(f"整体 FAIL（失败 {len(failures)} 条，回退 {len(regressed)} 条）")
        return 1
    print("整体 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
