"""全局配置（C5 §3.5 环境变量注入 + 规范 §4.6 环境变量清单）。

设计约定：
- 所有阈值（TTL、重试、超时）通过环境变量 / `.env` 注入，调停课高发期等运营调整不发版。
- `db_url` 默认走 SQLite（开发零配置），生产通过 `DB_URL=postgresql+asyncpg://...` 切换。
- 敏感凭证（JWT_SECRET / 源系统凭证）生产必须覆盖，本模块仅提供开发默认值。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "WAgent 校园智能体"
    env: str = "dev"

    # ---- 存储（C3）----
    # 生产：postgresql+asyncpg://user:pass@host:5432/campus_agent
    db_url: str = "sqlite+aiosqlite:///./campus_agent.db"
    # Alembic 迁移用同步连接串（迁移脚本不跑异步引擎）
    db_url_sync: str = "sqlite:///./campus_agent.db"

    # ---- 缓存（C4/C5）----
    # backend：memory（默认，零依赖，单测确定性）| redis（生产/联调，agent-redis 容器）
    cache_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # ---- 会话身份（C2）----
    jwt_secret: str = "dev-only-secret-REPLACE-IN-PROD"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # ---- LLM（C2 Function Calling）----
    llm_api_base: str | None = None
    llm_api_key: str | None = None

    # ---- 源系统适配器（C4）----
    edu_api_base_url: str | None = None
    edu_api_token: str | None = None
    lib_sip2_host: str | None = None
    lib_sip2_port: int = 6001

    # ---- TTL 阈值（C5 §3.5.2 R5.2，单位：秒）----
    ttl_timetable: int = 900        # 课表正常 15min
    ttl_timetable_peak: int = 300   # 调停课高发期 5min
    ttl_library: int = 300          # 借阅 5min
    ttl_card: int = 600             # 一卡通 10min

    # ---- 降级与重试（C4 §3.4.2 R4.3）----
    read_retry_times: int = 2                   # 读源系统重试次数
    retry_backoff_initial: float = 0.2          # 指数退避起点 200ms
    retry_backoff_max: float = 0.6              # 退避上限 600ms
    source_timeout_seconds: float = 2.0         # 源系统单次超时

    # ---- 频控（C2 §3.2.2 R2.4）----
    renew_per_session_limit: int = 10           # 单会话续借上限

    # ---- 数据时效说明 ----
    grade_cache_ttl: int = 0                    # 成绩明细不缓存（红线 R3）

    # ---- 会话（C2 编排层 · 任务设计 W1.6）----
    session_ttl_seconds: int = 1800             # 会话过期时间（30 min，每次追加自动续期）
    session_history_limit: int = 20             # 加载历史的默认条数（超过即触发压缩）
    session_compress_tail: int = 6              # 压缩时保留最近 N 条（安全阀：≥4）

    # ---- 编排层（C2 · 子任务 2）----
    orchestrator_tool_budget: int = 6               # 单轮对话工具调用上限（防 LLM 死循环）
    orchestrator_summary_max_chars: int = 200       # 会话压缩摘要字数上限（硬截断）
    orchestrator_llm_timeout_seconds: float = 15.0  # 单次 LLM 流式请求超时（子任务 3 消费）
    orchestrator_history_fetch_limit: int = 60      # 历史探测/上下文窗口（> history_limit
                                                    # 才能检测超阈值；降级时兜住最新消息）

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
