"""提醒引擎心跳骨架（R3a，任务设计 09 §F1/§2.6，实施设计 WAgent-IMPL-10）。

D1 裁决（09 §三）：**调度器应当愚蠢，正确性交给数据库**——心跳只负责
"定期触发对账"，幂等性、去重、并发安全全部下沉到 ``reminder_dispatch``
的约束与状态位上（R4+ 落地）。本模块只做三件事：

1. ``start``/``stop``：随应用生命周期启停（幂等，镜像 aclose 置空引用模式）；
2. ``_loop``：定期调 ``scan_once``，睡眠加 ±抖动（摊薄多实例同刻 claim 碰撞）；
3. ``scan_once``：独立 db 会话 → 拉 enabled 订阅 → 喂求值器 → 汇总
   :class:`ScanStats`；两层异常捕获（求值器级 / 会话级），任何失败不杀心跳。

求值器实现（``LibraryDueEvaluator`` / ``CourseEvaluator``，R3b）实现本模块
定义的 :class:`ReminderEvaluator` 协议；``evaluators`` 为空集时 ``scan_once``
为纯读空转，零写副作用。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence, Set
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..config import settings as default_settings
from ..logging_util import get_logger
from ..orm import ReminderSubscription
from .candidates import ReminderCandidate

logger = get_logger("reminder.engine")

#: 心跳任务名（测试断言 asyncio.all_tasks() 无悬挂时按名检索）
HEARTBEAT_TASK_NAME = "reminder-engine-heartbeat"

#: 睡眠下限（秒）：防 interval 配成 0 退化为热循环
_MIN_SLEEP_SECONDS = 0.01


@runtime_checkable
class ReminderEvaluator(Protocol):
    """求值器协议（09 §2.3 定版）：订阅 + 当前时刻 → 候选集。

    实现随源数据接入逐个注册（R3b 起）。``subjects`` 为该求值器认领的
    subject 值集合——ORM ``ReminderSubscription.subject`` 是 str 列，
    鸭子类型直接对齐，不经枚举转换。
    """

    subjects: Set[str]

    async def evaluate(
        self, subs: Sequence[Any], now: datetime
    ) -> list[ReminderCandidate]: ...


@dataclass
class ScanStats:
    """单次扫描统计（``scan_once`` 返回值：单测断言面 + 运行观测面）。"""

    started_at: datetime
    finished_at: datetime | None = None
    subscriptions: int = 0      # enabled 订阅数（分发前计数）
    candidates: int = 0         # 全部求值器产出候选总数
    evaluator_errors: int = 0   # 求值器级异常次数（风险③第②层）
    scan_errors: int = 0        # 会话/查询级异常次数（风险③第③层）
    # R4b 闸门计数（GateStats 回填；默认 0 向后兼容）
    sent: int = 0
    failed: int = 0
    degraded: int = 0
    skipped_quiet: int = 0
    skipped_quota: int = 0
    duplicates: int = 0

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def to_dict(self) -> dict[str, Any]:
        """JSON 序列化（R3c 手动触发端点响应形状；datetime → ISO 8601）。"""
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "subscriptions": self.subscriptions,
            "candidates": self.candidates,
            "evaluator_errors": self.evaluator_errors,
            "scan_errors": self.scan_errors,
            "sent": self.sent,
            "failed": self.failed,
            "degraded": self.degraded,
            "skipped_quiet": self.skipped_quiet,
            "skipped_quota": self.skipped_quota,
            "duplicates": self.duplicates,
            "duration_ms": self.duration_ms,
        }


class ReminderEngine:
    """进程内 asyncio 心跳（D1 裁决：不引入 APScheduler，零新依赖）。

    请求级组件（Orchestrator）按请求构造；引擎是**进程级单例**，由
    lifespan 装配（镜像 LLM 单例 / 共享适配器的第三个成员）。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        evaluators: Sequence[ReminderEvaluator] = (),
        settings: Settings | None = None,
        *,
        push_adapter: Any | None = None,
        jitter_ratio: float = 0.1,
    ) -> None:
        self._session_factory = session_factory
        self._evaluators = tuple(evaluators)
        self.settings = settings or default_settings
        self._push_adapter = push_adapter  # R4b：None → 只求值不发送（安全空转）
        self._jitter_ratio = max(0.0, jitter_ratio)
        self._task: asyncio.Task | None = None

    # ---- 生命周期（幂等，镜像 aclose 置空引用模式）----

    async def start(self) -> None:
        """启动心跳。``reminder_enabled=False`` 时不启动（AC-1a）；
        已有存活任务时不重复启动（AC-1b 幂等）。"""
        if not self.settings.reminder_enabled:
            logger.info(
                "reminder_disabled",
                hint="REMINDER_ENABLED=false，心跳不启动；测试/联调请显式开启",
            )
            return
        if self._task is not None and not self._task.done():
            return  # 幂等：已在跑
        self._task = asyncio.create_task(self._loop(), name=HEARTBEAT_TASK_NAME)
        logger.info(
            "reminder_started",
            interval=self.settings.reminder_scan_interval_seconds,
            evaluators=len(self._evaluators),
        )

    async def stop(self) -> None:
        """停止心跳：cancel + await，幂等（AC-1c/1d）。

        先置空引用再 cancel——并发 stop 走 no-op 路径；与
        ``BaseAdapter.aclose()`` 同构，允许之后再次 ``start()`` 重建。
        """
        task = self._task
        if task is None:
            return  # 幂等：未启动或已停
        self._task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("reminder_stopped")

    # ---- 心跳循环 ----

    async def _loop(self) -> None:
        """while True: 扫描 → 抖动睡眠。

        ``CancelledError`` 是唯一退出路径：``scan_once`` 全捕获普通异常
        （见下），``except Exception`` 不会吞掉 cancel。
        """
        while True:
            await self.scan_once()
            await asyncio.sleep(self._jittered_interval())

    def _jittered_interval(self) -> float:
        """interval × (1 ± jitter_ratio)，下限 0.01s。

        抖动只作用于"下次何时扫"：多实例同刻启动会把 claim 碰撞集中在
        同一 tick，±随机化把碰撞摊薄（09 §2.2）。窗口 horizon 语义不受
        影响（horizon = 间隔 × 2 已容忍 tick 抖动，09 §2.3）。
        """
        base = self.settings.reminder_scan_interval_seconds
        jittered = base * (1.0 + random.uniform(-self._jitter_ratio, self._jitter_ratio))
        return max(_MIN_SLEEP_SECONDS, jittered)

    # ---- 单次扫描（单测直调入口，09 §F1）----

    async def scan_once(self) -> ScanStats:
        """单次对账扫描。**永不抛出**——两层异常捕获，任何失败不杀心跳。

        上半段（R3）：独立会话拉 enabled 订阅 → 求值器产出候选；
        下半段（R4b）：候选非空且有推送适配器 → 四闸全序 + 下发闭环
        （:func:`gate.dispatch` 自开独立会话，异常并入 scan_errors 兜底）。
        """
        stats = ScanStats(started_at=datetime.now(UTC))
        try:
            async with self._session_factory() as db:
                rows = await db.execute(
                    select(ReminderSubscription).where(
                        ReminderSubscription.enabled.is_(True)
                    )
                )
                subs = list(rows.scalars().all())
            stats.subscriptions = len(subs)

            now = datetime.now(UTC)
            candidates: list[ReminderCandidate] = []
            for ev in self._evaluators:
                matched = [s for s in subs if s.subject in ev.subjects]
                if not matched:
                    continue
                try:
                    candidates.extend(await ev.evaluate(matched, now))
                except Exception:
                    # 风险③第②层：单求值器故障只记数，不扩散、不杀心跳
                    # （单点毒不扩散，而非全员灭，09 v1.3 修订）。
                    stats.evaluator_errors += 1
                    logger.exception("reminder_evaluator_failed")
            stats.candidates = len(candidates)

            if candidates and self._push_adapter is not None:
                from .gate import dispatch  # 局部导入避免 gate↔engine 环

                gate_stats = await dispatch(
                    candidates,
                    session_factory=self._session_factory,
                    push=self._push_adapter,
                    settings=self.settings,
                    now=now,
                )
                stats.sent = gate_stats.sent
                stats.failed = gate_stats.failed
                stats.degraded = gate_stats.degraded
                stats.skipped_quiet = gate_stats.skipped_quiet
                stats.skipped_quota = gate_stats.skipped_quota
                stats.duplicates = gate_stats.duplicates
        except Exception:
            # 风险③第③层：会话/查询失败同样不杀心跳——loop 层无需兜底重启。
            stats.scan_errors += 1
            logger.exception("reminder_scan_failed")
        finally:
            stats.finished_at = datetime.now(UTC)
            logger.info(
                "reminder_scan",
                subscriptions=stats.subscriptions,
                candidates=stats.candidates,
                evaluator_errors=stats.evaluator_errors,
                scan_errors=stats.scan_errors,
                sent=stats.sent,
                failed=stats.failed,
                degraded=stats.degraded,
                duration_ms=stats.duration_ms,
            )
        return stats
