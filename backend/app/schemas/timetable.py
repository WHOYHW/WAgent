"""课表查询契约（文档 12 §3.1 只读工具 timetable_query）。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from .meta import STUDENT_ID_DESC, ToolMeta


class TimetableQueryParams(BaseModel):
    """入参：student_id 由会话身份注入（禁止用户输入）。"""

    student_id: str = Field(description=STUDENT_ID_DESC)
    start_date: date = Field(description="查询起始日期（含）")
    end_date: date = Field(description="查询结束日期（含）")
    course_name: str | None = Field(default=None, description="可选，模糊匹配课程名")


class TimetableItem(BaseModel):
    """单节课（归一化后，与源系统字段解耦）。"""

    course_name: str
    course_code: str
    teacher: str
    location: str
    weekday: int = Field(ge=1, le=7, description="星期几（1=周一 … 7=周日）")
    period_start: int = Field(ge=1, description="起始节次")
    period_end: int = Field(ge=1, description="结束节次")
    weeks: str = Field(description="上课周次描述，如 1-16 周")
    is_changed: bool = Field(default=False, description="是否为调停课变更（必须显式高亮）")
    change_note: str | None = Field(default=None, description="变更说明，如「本周改至 5-108」")


class TimetableResult(BaseModel):
    """出参：结构化课表 + 溯源 meta。"""

    items: list[TimetableItem] = Field(default_factory=list)
    meta: ToolMeta
