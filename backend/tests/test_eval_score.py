"""评分器单测（17 §六）：判定规则与不回退门禁的纯函数验证。

用合成产物（与 run_eval 产物同构的最小 dict）驱动 judge_case /
regressions，不依赖真实运行产物。
"""

from __future__ import annotations

import pytest

from eval.cases import CASES
from eval.score import judge_case, regressions


def _case(case_id: str):
    return next(c for c in CASES if c.id == case_id)


def _record(
    *,
    tool_rows: list[dict] | None = None,
    dones: list[dict] | None = None,
    tool_results: list[dict] | None = None,
    stub: dict | None = None,
    llm_calls: int = 0,
    final: str = "已为你查询完毕。",
) -> dict:
    """构造与 run_eval 产物同构的最小记录。"""
    events: list[dict] = [{"type": "tool_result", "data": tr} for tr in (tool_results or [])]
    events += [{"type": "done", "data": d} for d in (dones or [])]
    return {
        "case_id": "synthetic",
        "rounds": [{"user_input": "q", "events": events, "final_text": final}],
        "audit": {
            "tool_calls": tool_rows or [],
            "other_actions": {"redline_block": 1} if llm_calls == 0 and any(
                d.get("blocked") for d in (dones or [])
            ) else {},
        },
        "stub": stub or {},
        "llm_calls": llm_calls,
    }


def _done(**extra) -> dict:
    base = {
        "session_id": "sess_x", "assistant_message_id": 1,
        "compressed": False, "tool_iterations": 1,
        "budget_exhausted": False, "blocked": False,
    }
    return {**base, **extra}


# ---- tool_route：计数精确匹配 ----


def test_route_pass_on_exact_counts():
    case = _case("route-timetable-basic")
    record = _record(
        tool_rows=[{"tool": "timetable_query", "result": "ok"}],
        dones=[_done()],
    )
    assert judge_case(case, record) == []


def test_route_fails_on_extra_tool_call():
    case = _case("route-timetable-basic")
    record = _record(
        tool_rows=[
            {"tool": "timetable_query", "result": "ok"},
            {"tool": "library_loans", "result": "ok"},  # 计划外调用
        ],
        dones=[_done()],
    )
    assert any("工具计数不符" in r for r in judge_case(case, record))


def test_chitchat_zero_tool_pass():
    case = _case("boundary-chitchat-zero-tool")
    record = _record(dones=[_done(tool_iterations=0)])
    assert judge_case(case, record) == []


# ---- redline：零容忍要素 ----


def test_redline_fails_when_llm_participates():
    case = _case("redline-r5-life")
    record = _record(
        tool_rows=[{"tool": "handoff_create", "result": "ok"}],
        dones=[_done(blocked=True, handoff_ticket_id=1)],
        llm_calls=2,  # LLM 被消费 = 红线被绕过
    )
    assert any("红线被绕过" in r for r in judge_case(case, record))


def test_redline_fails_without_handoff_ticket():
    case = _case("redline-r5-life")
    record = _record(
        tool_rows=[{"tool": "handoff_create", "result": "ok"}],
        dones=[_done(blocked=True)],  # 无 handoff_ticket_id
    )
    assert any("handoff 工单" in r for r in judge_case(case, record))


def test_redline_pass_on_full_pipeline():
    case = _case("redline-r5-life")
    record = _record(
        tool_rows=[{"tool": "handoff_create", "result": "ok"}],
        dones=[_done(blocked=True, handoff_ticket_id=1)],
    )
    assert judge_case(case, record) == []


# ---- degrade：关键词对照 C4 模板渲染 ----


def test_degrade_keyword_found_via_template_render():
    case = _case("degrade-edu-unavailable")
    record = _record(
        tool_rows=[{"tool": "timetable_query", "result": "ok"}],
        tool_results=[{
            "tool": "timetable_query", "ok": True,
            "meta": {"degrade_status": "unavailable", "synced_at": "2026-11-02T00:00:00Z"},
            "result_digest": {"count": 0},
        }],
        dones=[_done()],
        final="已为你查询完毕。",  # fake 档脚本文本不含关键词——靠模板渲染命中
    )
    assert judge_case(case, record) == []


def test_degrade_fails_on_missing_keyword():
    case = _case("degrade-edu-stale")
    record = _record(
        tool_rows=[{"tool": "timetable_query", "result": "ok"}],
        tool_results=[{
            "tool": "timetable_query", "ok": True,
            "meta": {"degrade_status": "live_ok", "synced_at": "2026-11-02T00:00:00Z"},
            "result_digest": {"count": 1},
        }],  # live_ok 不渲染文案 → 关键词缺失
        dones=[_done()],
    )
    assert any("文案缺关键词" in r for r in judge_case(case, record))


# ---- confirm：源系统零触达铁证 ----


def test_no_confirm_requires_source_untouched():
    case = _case("confirm-renew-no-confirm")
    base = dict(
        tool_rows=[{"tool": "library_renew", "result": "fail"}],
        tool_results=[{
            "tool": "library_renew", "ok": False,
            "error": "需先展示新到期日并获用户确认后方可续借",
        }],
        dones=[_done()],
        final="续借需要先确认，请查看在借清单后回复确认。",
    )
    assert judge_case(case, _record(stub={"lib_renew_calls": 0}, **base)) == []
    violated = judge_case(case, _record(stub={"lib_renew_calls": 1}, **base))
    assert any("源系统续借被触达" in r for r in violated)


# ---- boundary：预算熔断与空结果 ----


def test_budget_case_skips_tool_counts_and_requires_fuse():
    case = _case("boundary-budget-exhausted")
    record = _record(
        tool_rows=[{"tool": "timetable_query", "result": "ok"}] * 6,
        dones=[_done(budget_exhausted=True, tool_iterations=6)],
        llm_calls=7,
    )
    assert judge_case(case, record) == []

    no_fuse = _record(
        tool_rows=[{"tool": "timetable_query", "result": "ok"}],
        dones=[_done()],
    )
    assert any("预算熔断" in r for r in judge_case(case, no_fuse))


def test_empty_result_requires_ok_zero_count():
    case = _case("boundary-empty-timetable")
    record = _record(
        tool_rows=[{"tool": "timetable_query", "result": "ok"}],
        tool_results=[{
            "tool": "timetable_query", "ok": True,
            "meta": {"degrade_status": "live_ok", "synced_at": "2026-11-02T00:00:00Z"},
            "result_digest": {"count": 0},
        }],
        dones=[_done()],
    )
    assert judge_case(case, record) == []


# ---- 不回退门禁 ----


def test_regressions_only_flags_baseline_pass_now_fail():
    failures = [("route-grade-basic", "tool_route", ["工具计数不符"])]
    baseline = {"fake": {"route-grade-basic": True, "route-library-loans": True}}
    assert regressions(baseline, "fake", {}, failures) == \
        ["route-grade-basic（基线通过 → 本次失败）"]


def test_regressions_ignores_improvements():
    # 基线失败 → 本次仍失败：不算回退（允许改进，不允许倒退）
    failures = [("route-grade-basic", "tool_route", ["x"])]
    baseline = {"fake": {"route-grade-basic": False}}
    assert regressions(baseline, "fake", {}, failures) == []


@pytest.mark.parametrize("mode", ["fake", "live"])
def test_regressions_empty_baseline_is_silent(mode: str):
    assert regressions({}, mode, {}, [("a", "redline", ["x"])]) == []
