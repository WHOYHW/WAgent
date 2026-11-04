"""确定性防线资产（F2 红线 / F4b 写门禁 / F5 熔断的话术与词表）。

P4 原则：授权判定、危机路由、拦截话术全部是确定性资产，不允许概率性
组件参与。单独成模块——它们变更频率低、但每一处措辞都有制度含义，
改前须对照设计文档 F2/F4b 与《11 提醒引擎》§4.2 SLA 表。
"""

from __future__ import annotations

from ..schemas import HandoffPriority

# 规则 → 优先级 / 归属映射（11 §4.2 SLA 表，设计文档 F2）
REDLINE_PRIORITY: dict[str, HandoffPriority] = {
    "R5_生命优先": HandoffPriority.P0,
    "R3_敏感信息": HandoffPriority.P1,
    "R4_不代表达诉求": HandoffPriority.P1,
    "R1_终局性结论": HandoffPriority.P2,
}
REDLINE_OWNER: dict[str, str] = {
    "R5_生命优先": "学生工作部/心理中心",
    "R3_敏感信息": "现代教育技术中心",
    "R4_不代表达诉求": "二级学院教务办",
    "R1_终局性结论": "二级学院教务办",
}

R5_LIFELINE_SCRIPT = (
    "同学你好，谢谢你愿意把这些说出来，你现在的安全是最重要的事。"
    "学校心理健康中心随时可以为你提供支持（大学生活动中心 3 楼），"
    "24 小时心理援助热线：0730-8888888，也可以直接联系你的辅导员。"
    "已为你转接人工老师，请一定留意回复，我们不会让你一个人面对。"
)

BLOCKED_REPLY_TEMPLATES: dict[str, str] = {
    "R1_终局性结论": (
        "毕业资格、学籍等终局性判断需要由二级学院教务办老师依据正式规则答复，"
        "我不能代替老师下结论。已为你创建人工工单，老师会尽快联系你，请留意回复。"
    ),
    "R3_敏感信息": (
        "这个问题涉及个人敏感信息，为保护同学隐私我无法直接处理。"
        "已为你转接相关老师核实处理，请留意回复。"
    ),
    "R4_不代表达诉求": (
        "代你提交正式申请需要本人操作或人工老师协助完成，我不能代替你表达诉求。"
        "已为你创建人工工单，老师会联系你一起完成，请留意回复。"
    ),
}

# 写工具的服务端注入字段（F4b）；读工具仅剥离 student_id
WRITE_GUARDED_FIELDS = ("student_id", "confirmed", "idempotency_key")

# 阶段一唯一写白名单成员（F4b 门禁适用范围）
RENEW_TOOL_NAME = "library_renew"

# 续借确认关键词集（确定性匹配，P4：授权判定不允许概率性组件）
RENEW_CONFIRM_KEYWORDS = ("确认续借", "确定续借", "继续续借", "确认", "确定")

HARD_FUSE_SCRIPT = (
    "查询步骤较多暂时未能完成，建议把问题拆分得更具体一些，"
    "或者直接转人工老师协助处理。给你带来不便非常抱歉。"
)

# 不暴露给 LLM 的工具（F4b）：handoff_create 由编排层确定性路径调用（F2），
# 不经 LLM——否则模型可伪造 hit_rules/priority 生成 P0 危机工单
LLM_EXCLUDED_TOOLS = ("handoff_create",)
