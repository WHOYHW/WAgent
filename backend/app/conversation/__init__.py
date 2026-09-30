"""会话存储层（任务设计 W1：Conversation Store）。

M2 编排层的唯一合法入口。负责：
- 会话生命周期：create / get / touch / 过期检测；
- 消息流：append / load_history；
- 历史压缩：compress（system 摘要 + 软删旧消息）。

约束（与既有架构对齐）：
- ``student_id`` 来自调用方注入（JWT），本层不校验但也不接受外部"覆盖"；
- 与审计解耦：本层不写 ``audit_log``，由编排层统一调度；
- 与既有降级 / 缓存 / 工具层无依赖关系，可独立单测。
"""

from .store import SessionExpired, SessionNotFound, SessionStore

__all__ = ["SessionStore", "SessionNotFound", "SessionExpired"]
