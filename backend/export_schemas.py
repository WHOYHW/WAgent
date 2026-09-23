"""导出 6 个工具 + 契约模型的 JSON Schema（AC1.2 / AC2.1 的验收辅助脚本）。

用法（backend 目录下）：``python export_schemas.py``
输出到 ``backend/schemas_export/``。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas import (
    AdapterResponse,
    GradeQueryParams,
    GradeResult,
    HandoffCreateParams,
    HandoffTicketOut,
    LibraryLoansParams,
    LibraryLoansResult,
    LibraryRenewParams,
    LibraryRenewResult,
    ReminderSubscribeParams,
    ReminderSubscribeResult,
    TimetableQueryParams,
    TimetableResult,
    ToolMeta,
)
from app.tools import default_registry

OUT = Path(__file__).parent / "schemas_export"
OUT.mkdir(exist_ok=True)


def dump(name: str, schema: dict) -> None:
    (OUT / f"{name}.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    # 契约模型（C1）
    models = {
        "ToolMeta": ToolMeta,
        "AdapterResponse": AdapterResponse,
        "timetable_params": TimetableQueryParams,
        "timetable_result": TimetableResult,
        "grade_params": GradeQueryParams,
        "grade_result": GradeResult,
        "loans_params": LibraryLoansParams,
        "loans_result": LibraryLoansResult,
        "renew_params": LibraryRenewParams,
        "renew_result": LibraryRenewResult,
        "subscribe_params": ReminderSubscribeParams,
        "subscribe_result": ReminderSubscribeResult,
        "handoff_params": HandoffCreateParams,
        "handoff_result": HandoffTicketOut,
    }
    for name, model in models.items():
        dump(name, model.model_json_schema())

    # 工具 Schema（C2）
    for spec in default_registry.specs():
        dump(f"tool_{spec.name}", spec.to_dict())

    print(f"已导出 {len(models) + len(default_registry.specs())} 个 Schema 到 {OUT}")


if __name__ == "__main__":
    main()
