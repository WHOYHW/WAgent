"""契约层基础类型：ToolMeta / DegradeStatus / AdapterResponse。

对应规范：
- §3.1.2 R1.3：所有工具返回值必须携带 ``meta`` 三元组（source / synced_at / is_stale）。
- §3.1.2 R1.4：适配器返回 ``AdapterResponse`` 封装（data + degrade_status + synced_at + source）。
- §3.4.2 R4.1：五级降级状态码 ``DegradeStatus``。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

# R1.2：student_id 的契约层来源约束 —— 会话身份注入，禁止由用户输入。
# 该 description 会原样进入导出的 JSON Schema，是「前置条件显式化」的落点。
STUDENT_ID_DESC = "学号，会话身份注入，禁止由用户输入"


class DegradeStatus(str, Enum):
    """五级降级状态码（§3.4.2 R4.1）。"""

    FRESH = "fresh"                # 缓存命中且未过期，is_stale=False
    LIVE_OK = "live_ok"            # 源系统实时调用成功
    STALE_CACHE = "stale_cache"    # 源超时，返回历史缓存，is_stale=True
    UNAVAILABLE = "unavailable"    # 源超时且无缓存，明确不可用
    WRITE_FAILED = "write_failed"  # 写操作失败（续借），不降级、不重试超 1 次


class ToolSource(str, Enum):
    """meta.source 的可取值（§3.1.2 R1.3）。"""

    EDU_SYSTEM = "edu_system"
    LIBRARY_ILS = "library_ils"
    CACHE = "cache"
    CARD_SYSTEM = "card_system"
    INTERNAL = "internal"  # 内部订阅库/工单库（无外部源系统）


class ToolMeta(BaseModel):
    """所有工具出参顶层必须携带的溯源三元组（+ 降级状态 + 陈旧标注）。"""

    source: ToolSource = Field(description="数据来源系统标识")
    synced_at: datetime = Field(description="数据同步时间（ISO 8601 含时区）")
    degrade_status: DegradeStatus = Field(
        description="五级降级状态（R4.1），渲染层据此选择话术模板；"
        "UNAVAILABLE / WRITE_FAILED 必须有用户可见文案（R4.4 / R5.3）"
    )
    is_stale: bool = Field(default=False, description="是否为陈旧数据（来自降级缓存）")
    stale_reason: str | None = Field(default=None, description="陈旧原因，如 source_timeout")


class AdapterResponse[T](BaseModel):
    """适配器统一封装（§3.1.2 R1.4）。

    区分「业务结果」与「降级标记」：工具层据此决定是否设置 ``meta.is_stale``。
    """

    data: T | None = Field(default=None, description="归一化业务数据；降级时可能为 None")
    degrade_status: DegradeStatus = Field(description="降级状态（见 DegradeStatus）")
    synced_at: datetime = Field(description="数据同步时间")
    source: ToolSource = Field(description="来源系统")
