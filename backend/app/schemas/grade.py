"""成绩查询契约（文档 12 §3.1 只读工具 grade_query）。

红线约束（文档 10 §5.2 三不原则 + 文档 12 §3 存储红线 R3）：
- 成绩明细不落任何持久化存储（GradeItem 不得作为 ORM 字段类型）。
- ``system_status`` 为教务系统原始状态，Agent 不得自行计算。
- 仅允许「算术汇总」（均分/学分），并附口径说明 + 免责声明。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .meta import STUDENT_ID_DESC, ToolMeta

# 成绩认定免责声明（文档 12 §3.1 grade.returns.meta.disclaimer 的设计意图，
# 收敛为出参顶层字段，保持 meta 三元组在全部工具间一致）。
GRADE_DISCLAIMER = "以上为原始成绩呈现，成绩认定及各类资格判定以教务处正式结论为准"


class GradeQueryParams(BaseModel):
    student_id: str = Field(description=STUDENT_ID_DESC)
    term: str | None = Field(default=None, description="学期，如 2025-2026-1；缺省为最新学期")
    course_name: str | None = Field(default=None, description="可选，按课程名筛选")
    aggregate: bool = Field(default=False, description="是否返回算术汇总（均分/学分）")


class GradeItem(BaseModel):
    """单门已发布成绩（源系统原文，不加工、不判定）。"""

    course_name: str
    credit: float = Field(ge=0, description="学分")
    score: float = Field(description="成绩数值")
    system_status: str = Field(description="教务系统原始状态，如 通过/及格/不及格")
    status_note: str = Field(description="状态原文说明，Agent 不得自行计算")


class GradeAggregate(BaseModel):
    """算术汇总（允许，但须附口径说明）。"""

    avg_score: float | None = Field(default=None, description="算术平均分")
    earned_credits: float | None = Field(default=None, description="已获学分合计")
    caliber: str = Field(description="统计口径说明")


class GradeResult(BaseModel):
    items: list[GradeItem] = Field(default_factory=list)
    aggregate: GradeAggregate | None = Field(default=None)
    disclaimer: str = Field(default=GRADE_DISCLAIMER)
    meta: ToolMeta
