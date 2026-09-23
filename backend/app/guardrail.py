"""护栏：红线规则（文档 10 §8.3 阶段一红线执行清单）。

阶段一只实现 L1 关键词/正则通道（红线必须零漏拦截，语义通道后续补齐）。
命中返回命中的规则编号，供 ``handoff_create`` 写入 ``hit_rules``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# R1 终局性结论 / R3 敏感信息 / R5 生命优先 关键词（可扩展为 YAML 热更新）
_REDLINE_KEYWORDS: dict[str, list[str]] = {
    "R1_终局性结论": [
        "毕业资格", "能毕业", "毕不了业", "学位", "学籍", "保研", "评优",
        "奖学金评定", "绩点不够", "学业预警", "挂科了", "不能毕业",
    ],
    "R3_敏感信息": [
        "他人成绩", "别人成绩", "同学成绩", "银行卡", "身份证", "心理", "处分",
    ],
    "R4_不代表达诉求": [
        "帮我提交复核", "代我申诉", "帮我申请减免", "代提交",
    ],
    "R5_生命优先": [
        "自杀", "自残", "不想活", "轻生", "伤害自己", "活不下去", "想死",
    ],
}


@dataclass
class GuardrailResult:
    hit: bool
    rules: list[str] = field(default_factory=list)
    life_priority: bool = False  # 命中 R5，走生命优先特殊通道


def check_redlines(text: str) -> GuardrailResult:
    """对用户输入做红线关键词检查，返回命中规则。"""
    rules: list[str] = []
    lowered = text.lower()
    for rule, keywords in _REDLINE_KEYWORDS.items():
        if any(k in lowered for k in keywords):
            rules.append(rule)
    life = "R5_生命优先" in rules
    return GuardrailResult(hit=bool(rules), rules=rules, life_priority=life)
