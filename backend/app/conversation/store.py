"""``SessionStore`` 实现（任务设计 W1.5）。

设计要点：
- 序号生成走应用层 ``MAX(seq)+1`` + ``UniqueConstraint(session_id, seq)`` 双重保护；
  并发撞号时依赖唯一约束抛 ``IntegrityError``，调用方应在编排层做"重试一次"处理
  （短期方案；中期迁 PG 序列，见任务设计文档"难点 1"）。
- 过期语义：``load_history`` / ``append`` / ``compress`` 都先调用 ``get``，
  过期会话的 ``status`` 被惰性置为 ``expired`` 并抛 ``SessionExpired``，消息本体不删
  （满足 ≥180 天审计留存）。
- 历史压缩：把除最近 ``kept_tail`` 条外的全部旧消息打 ``deleted_at`` 软删，再插入 1
  条 ``role='system', is_summary=true`` 的摘要。安全阀：``kept_tail`` 至少 4 条，
  防止丢上下文。
- 与既有 ``write_audit`` 解耦：审计由编排层（子任务 2）按 ``tool_call`` / ``redline_block``
  等统一触发，避免每条消息都写一条审计（噪声 + 性能）。

注意：ORM 模型 ``Message``（``app.orm.models.Message``）与契约层 ``Message``
（``app.schemas.conversation.Message``）同名；本模块一律用 ``MessageORM`` 别名
引用 ORM 类，避免覆盖 Pydantic 契约。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..orm.models import ConversationSession
from ..orm.models import Message as MessageORM
from ..schemas.conversation import (
    CompressedSummary,
    Conversation,
    Message,
    MessageIn,
)


class SessionNotFound(Exception):
    """会话不存在（首次访问 / 已被物理删除）。"""


class SessionExpired(Exception):
    """会话已过期，编排层应决定重建或拒绝。"""


class SessionStore:
    """会话与消息的异步 CRUD + 压缩（编排层 M2 唯一合法入口）。

    生命周期：``create → touch(自动) → append → load_history → compress �� expire``。
    """

    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # ---- 会话 ----

    async def create(self, student_id: str, channel: str = "wecom") -> Conversation:
        """创建会话。``session_id`` 由应用层��成 ``sess_<16hex>``，全局唯一。"""
        now = datetime.now(timezone.utc)
        session_id = f"sess_{uuid.uuid4().hex[:16]}"
        row = ConversationSession(
            session_id=session_id,
            student_id=student_id,
            channel=channel,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.settings.session_ttl_seconds),
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return Conversation.model_validate(row, from_attributes=True)

    async def get(self, session_id: str) -> Conversation:
        """获取会话；过期则惰性置 expired 并抛 :class:`SessionExpired`。

        注：显式按 ``session_id`` 列查询，不依赖 ``session.get()`` 的 identity map
        行为 —— SQLAlchemy 异步 + SQLite 下 commit 后对象可能已从 identity 移除，
        ``session.get()`` 按主键查不到非主键唯一列对应的记录。
        """
        stmt = select(ConversationSession).where(
            ConversationSession.session_id == session_id
        )
        row = (await self.db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise SessionNotFound(session_id)
        await self._expire_if_needed(row)
        if row.status == "expired":
            raise SessionExpired(session_id)
        return Conversation.model_validate(row, from_attributes=True)

    async def touch(self, session_id: str) -> None:
        """显式续期 ``expires_at``。``append`` / ``compress`` 内部已自动调用。"""
        await self._touch(session_id)

    # ---- 消息 ----

    async def load_history(
        self, session_id: str, *, limit: int | None = None
    ) -> list[Message]:
        """按 ``seq ASC`` 加载**最近** ``limit`` 条消息（尾部窗口）。

        P0-1 修正（子任务 2 设计评审）：先按 seq 倒序取最近 N 条，内存反转为
        时间正序——保证"最新消息（含本轮 user）必然在窗口内"，供编排层 F3
        探测/降级上下文使用。默认 ``limit`` = ``session_history_limit``。

        软删消息（``deleted_at IS NOT NULL``）自动跳过；``role='system'`` 的摘要不
        排除，调用方按需显式过滤（编排层会按"首条 system 作为 system prompt"处理）。
        """
        await self.get(session_id)
        effective_limit = limit or self.settings.session_history_limit
        stmt = (
            select(MessageORM)
            .where(
                MessageORM.session_id == session_id,
                MessageORM.deleted_at.is_(None),
            )
            .order_by(MessageORM.seq.desc())     # 倒序取最近 N 条
            .limit(effective_limit)
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        rows.reverse()                           # 反转为时间正序（N ≤ 窗口量级）
        return [Message.model_validate(r, from_attributes=True) for r in rows]

    async def append(self, session_id: str, msg: MessageIn) -> Message:
        """追加单条消息，自动 ``touch`` 续期。"""
        await self.get(session_id)
        seq = await self._next_seq(session_id)
        row = MessageORM(
            session_id=session_id,
            seq=seq,
            role=msg.role,
            content=msg.content,
            tool_calls=msg.tool_calls,
            tool_call_id=msg.tool_call_id,
            trace_id=msg.trace_id,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(row)
        await self._touch(session_id)
        await self.db.commit()
        await self.db.refresh(row)
        return Message.model_validate(row, from_attributes=True)

    async def append_many(self, session_id: str, msgs: list[MessageIn]) -> list[Message]:
        """批量追加（单事务；用于 LLM 一轮多工具调用 + 工具结果回填）。"""
        await self.get(session_id)
        seq = await self._next_seq(session_id)
        rows: list[MessageORM] = []
        for m in msgs:
            row = MessageORM(
                session_id=session_id,
                seq=seq,
                role=m.role,
                content=m.content,
                tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id,
                trace_id=m.trace_id,
                created_at=datetime.now(timezone.utc),
            )
            self.db.add(row)
            rows.append(row)
            seq += 1
        await self._touch(session_id)
        await self.db.commit()
        for r in rows:
            await self.db.refresh(r)
        return [Message.model_validate(r, from_attributes=True) for r in rows]

    # ---- 压缩 ----

    async def compress(
        self,
        session_id: str,
        summary: str,
        *,
        kept_tail: int | None = None,
    ) -> CompressedSummary:
        """把除最近 ``kept_tail`` 条外的消息聚合成 1 条 system 摘要。

        - ``kept_tail`` 至少 4 条（防丢上下文）；
        - 旧消息走 ``deleted_at`` 软删，便于审计回溯；
        - 新摘要消息 ``role='system', is_summary=true``，供编排层在 LLM 调用时识别。
        """
        await self.get(session_id)
        keep = max(4, kept_tail or self.settings.session_compress_tail)

        # 取除最近 keep 条外的全部未软删消息（按 seq desc 取第 keep+1 条作为分界）。
        cutoff_subq = (
            select(MessageORM.seq)
            .where(
                MessageORM.session_id == session_id,
                MessageORM.deleted_at.is_(None),
            )
            .order_by(MessageORM.seq.desc())
            .offset(keep)
            .limit(1)
            .scalar_subquery()
        )
        old_stmt = select(MessageORM).where(
            MessageORM.session_id == session_id,
            MessageORM.deleted_at.is_(None),
            MessageORM.seq <= cutoff_subq,
        )
        old_rows = list((await self.db.execute(old_stmt)).scalars().all())
        summarized_count = len(old_rows)

        # 写入 system 摘要。
        summary_row = MessageORM(
            session_id=session_id,
            seq=await self._next_seq(session_id),
            role="system",
            content=summary,
            is_summary=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(summary_row)
        await self.db.flush()  # 拿到 summary_row.id

        # 软删旧消息 + 更新会话指针。
        now = datetime.now(timezone.utc)
        for r in old_rows:
            r.deleted_at = now
        sess_stmt = select(ConversationSession).where(
            ConversationSession.session_id == session_id
        )
        sess = (await self.db.execute(sess_stmt)).scalar_one()
        sess.summary_message_id = summary_row.id
        await self._touch(session_id)
        await self.db.commit()
        await self.db.refresh(summary_row)

        return CompressedSummary(
            session_id=session_id,
            summarized_count=summarized_count,
            kept_tail=keep,
            summary_message_id=summary_row.id,
        )

    # ---- 私有 ----

    async def _expire_if_needed(self, row: ConversationSession) -> None:
        """惰性过期检测：当前时间已过 expires_at → status='expired'。

        SQLite 不原生支持带时区类型，SQLAlchemy 读出的 ``expires_at`` 可能是
        naive datetime；比较前统一按 UTC 处理。
        """
        now = datetime.now(timezone.utc)
        expires_at = row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if (
            row.status == "active"
            and expires_at is not None
            and expires_at < now
        ):
            row.status = "expired"
            row.updated_at = now
            await self.db.commit()

    async def _next_seq(self, session_id: str) -> int:
        """应用层序号生成：``COALESCE(MAX(seq), 0) + 1``。

        并发风险依赖 ``UniqueConstraint(session_id, seq)`` 兜底；中期迁 PG 序列
        后此函数可删除，详见任务设计文档"难点 1"。
        """
        stmt = select(func.coalesce(func.max(MessageORM.seq), 0) + 1).where(
            MessageORM.session_id == session_id,
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def _touch(self, session_id: str) -> None:
        """续期：``expires_at = now() + ttl``，``updated_at = now()``。"""
        await self.db.execute(
            update(ConversationSession)
            .where(ConversationSession.session_id == session_id)
            .values(
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=self.settings.session_ttl_seconds),
                updated_at=func.now(),
            )
        )
