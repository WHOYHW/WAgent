"""全局配置（C5 §3.5 环境变量注入 + 规范 §4.6 环境变量清单）。

设计约定：
- 所有阈值（TTL、重试、超时）通过环境变量 / `.env` 注入，调停课高发期等运营调整不发版。
- `db_url` 默认走 SQLite（开发零配置），生产通过 `DB_URL=postgresql+asyncpg://...` 切换。
- 敏感凭证（JWT_SECRET / 源系统凭证）生产必须覆盖，本模块仅提供开发默认值。
"""

from __future__ import annotations

from datetime import time
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

    # ---- Web 层（C2 · 子任务 4 SSE 端点）----
    # 逗号分隔的前端源；空 = 不挂 CORS 中间件，生产行为不变。
    # 开发态接前端时在 .env 配置（如 http://localhost:5173）。
    cors_allow_origins: str = ""

    # ---- LLM（C2 Function Calling · 子任务 3 适配器消费）----
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None          # 模型名 / 方舟推理接入点 ID
    # 豆包 seed 系列为思考模型：thinking 计费且推高首字延迟。三态：
    # None=不发该参数（代码默认，任何 OpenAI 兼容端点安全，严格端点对未知
    # 参数返回 400）；True/False=显式开/关。方舟 seed 用户建议 .env 配
    # LLM_THINKING_ENABLED=false（服务端默认可能开思考）。
    # 传输层始终剥离 reasoning_content（纵深防御，见 llm/adapter.py）
    llm_thinking_enabled: bool | None = None
    # usage 尾块（空 choices）计量：方舟兼容性待联调验证，默认关
    llm_stream_include_usage: bool = False

    # ---- 源系统适配器（C4）----
    edu_api_base_url: str | None = None
    edu_api_token: str | None = None
    lib_sip2_host: str | None = None
    lib_sip2_port: int = 6001

    # ---- 推送通道（R2，任务设计 09 §2.5）----
    push_wecom_webhook_url: str | None = None  # 企微群机器人 webhook；未配置 → 开发桩
    push_timeout_seconds: float = 10.0         # 推送单次超时（与源系统 2s 语义不同）

    # ---- 提醒引擎 ----
    # 心跳是"学生不提问也会发生"的进程副作用，默认关：本地裸起 uvicorn
    # 不触发任何扫描；测试/联调显式置 true。
    reminder_enabled: bool = False
    # 扫描间隔（对齐 12 §2 "轮询兜底 5 分钟"）；float 便于测试注入小间隔。
    reminder_scan_interval_seconds: float = 300.0
    # 静默窗口（本地时；R4 时机门消费，随 §2.6 一次补齐避免 R4 再动 config）。
    reminder_quiet_start: time = time(23, 0)
    reminder_quiet_end: time = time(7, 0)
    # 单用户单日上限（P0 不计；R4 频控消费）。
    reminder_daily_cap: int = 8
    # claimed 遗留自愈阈值（R6 CAS 认领消费）。
    reminder_stale_claim_seconds: int = 300
    # 窗口前瞻 = 扫描间隔 × N（R3b 求值器消费）。
    reminder_horizon_ticks: int = 2

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

    # ---- 知识库检索 RAG（文档 18 §六，K1 v1.2）----
    # D5 总门控：False 时 kb_search 不注册、ingest CLI 拒绝运行（能力上线是运营决策）。
    kb_enabled: bool = False
    # D4：方舟 embedding 模型 ID，base_url/key 复用 llm 既有凭证，零新增凭证面。
    kb_embedding_model: str = "doubao-embedding"
    kb_top_k: int = 4                            # 返回给 LLM 的块数上限
    kb_chunk_max_chars: int = 500                # 切块单块长度上限（滑窗窗口长同值；重叠固定 50）
    # RRF 融合分过滤线：k=60 下单路 rank≤6 过线（1/66≈0.0152，rank-7=1/67≈0.0149），
    # 双路共现分远高于此；候选池 20 时旧值 0.01 对池内任何结果（rank-20=1/80=0.0125）
    # 都不构成过滤，语义失真已修正。弱匹配时少返回而非凑满 top_k
    kb_score_threshold: float = 0.015
    kb_rrf_k: int = 60                           # RRF 融合常数（D3）
    kb_corpus_version: str = "2026-11-05.1"      # 语料版本锚，随正式灌库递增
    kb_stale_hours: int = 24 * 30                # 语料陈旧判定（知识比课表陈旧得慢）
    kb_crawl_depth: int = 3                      # 抓取深度上限
    kb_request_interval_seconds: float = 1.0     # 抓取节流（对校方站点的礼貌下限）
    # K3 v1.4 真机档：ArkEmbeddingClient 批量请求大小（ingest 批量向量化切批）
    kb_embedding_batch_size: int = 16
    # 期望向量维度：None=不校验只报告实际维度；配置时全批维度必须一致且等于
    # 该值（--live 冒烟判据 + 入口防脏，检索内核矩阵加载降级是第二道兜底）
    kb_embedding_dim: int | None = None

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
