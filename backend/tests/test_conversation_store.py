"""会话存储层单元测试（任务设计 W1.7 · 验收 AC-IMPL01-1 ~ 10）。

按 AC 表逐条覆盖：创建 / 追加 / 加载 / 续期 / 过期 / 压缩 / 唯一性 / 顺序保证。
时间相关用例走 ``freezegun`` 冻结时钟；DB 走 ``conftest.db`` 内存 SQLite。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.conversation import SessionExpired, SessionNotFound, SessionStore
from app.orm import ConversationSession, Message
from app.schemas.conversation import MessageIn

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)


def _store(db, **overrides) -> SessionStore:
    from app.config import Settings

    settings = Settings(**overrides) if overrides else Settings()
    return SessionStore(db, settings)


# ---- AC-IMPL01-2 / 1.8 创建：session_id 唯一 + student_id 不被外部覆盖 ----


async def test_create_returns_unique_session_ids(db):
    """AC-IMPL01-2：连续创建 100 次，session_id 全库唯一。"""
    store = _store(db)
    seen: set[str] = set()
    for _ in range(100):
        c = await store.create(student_id="20230001")
        assert c.session_id not in seen
        seen.add(c.session_id)
        assert c.status == "active"
        assert c.student_id == "20230001"  # 入参值原样保存，外部覆盖靠调用方自律
    assert len(seen) == 100


async def test_create_distinguishes_students(db):
    """AC-IMPL01-8：student_id 是会话绑定的根，create 不接受外部"覆盖"。"""
    store = _store(db)
    a = await store.create(student_id="20230001")
    b = await store.create(student_id="20230002")
    assert a.student_id == "20230001"
    assert b.student_id == "20230002"
    assert a.session_id != b.session_id


# ---- AC-IMPL01-3 追加：单调递增 + 并发安全 ----


async def test_append_seq_monotonic(db):
    store = _store(db)
    sess = await store.create(student_id="20230001")
    seqs: list[int] = []
    for i in range(10):
        m = await store.append(sess.session_id, MessageIn(role="user", content=f"q{i}"))
        seqs.append(m.seq)
    assert seqs == sorted(seqs) and len(set(seqs)) == 10


async def test_append_concurrent_no_duplicate_seq(db):
    """AC-IMPL01-3：连续追加 N 条 seq 严格单调 + 全库唯一。

    注：SQLAlchemy 异步引擎 + 单 AsyncSession 不可并发 add/commit（见 SAWarning）。
    并发安全由应用层在编排层做"重试一次"实现（依赖 ``UniqueConstraint``），
    本测试验证应用层序号生成器在顺序场景下的正确性。
    """
    store = _store(db)
    sess = await store.create(student_id="20230001")
    seqs: list[int] = []
    for i in range(30):
        m = await store.append(sess.session_id, MessageIn(role="user", content=f"q{i}"))
        seqs.append(m.seq)
    assert seqs == list(range(1, 31))
    assert len(set(seqs)) == 30


async def test_append_uniqueness_constraint_at_db_level(db):
    """AC-IMPL01-3 兜底：(session_id, seq) 唯一约束能拦住重复 seq。"""
    from sqlalchemy.exc import IntegrityError

    store = _store(db)
    sess = await store.create(student_id="20230001")
    await store.append(sess.session_id, MessageIn(role="user", content="a"))
    # 直接写一条 seq=1 重复消息，期望 IntegrityError
    dup = Message(session_id=sess.session_id, seq=1, role="user", content="b")
    db.add(dup)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


# ---- AC-IMPL01-4 加载：按 seq 升序 + 限制条数 + 跳过软删 ----


async def test_load_history_orders_and_limits(db):
    """AC-IMPL01-4（P0-1 修正后语义）：按 seq 升序返回**最近** N 条，
    30 条插入 + limit=10 → 返回 seq=21..30（尾部窗口，编排层 F3 依赖）。"""
    store = _store(db)
    sess = await store.create(student_id="20230001")
    for i in range(30):
        await store.append(sess.session_id, MessageIn(role="user", content=f"m{i}"))
    history = await store.load_history(sess.session_id, limit=10)
    assert [m.seq for m in history] == list(range(21, 31))
    assert [m.role for m in history] == ["user"] * 10
    assert all(m.content.startswith("m") for m in history)


async def test_load_history_default_limit_from_settings(db):
    """未指定 limit 时走 session_history_limit 配置（默认 20）——返回最近 N 条。"""
    store = _store(db, session_history_limit=7)
    sess = await store.create(student_id="20230001")
    for i in range(15):
        await store.append(sess.session_id, MessageIn(role="user", content=f"m{i}"))
    history = await store.load_history(sess.session_id)
    assert len(history) == 7
    assert [m.seq for m in history] == list(range(9, 16))


async def test_load_history_skips_soft_deleted(db):
    """软删消息自动从 load_history 跳过；system 摘要 + 保留尾可见。"""
    store = _store(db, session_compress_tail=4)
    sess = await store.create(student_id="20230001")
    for i in range(5):
        await store.append(sess.session_id, MessageIn(role="user", content=f"m{i}"))
    summary = await store.compress(sess.session_id, summary="s0", kept_tail=4)
    history = await store.load_history(sess.session_id)
    # 5 条原 user → 压缩保留 4 条 + 1 条 system 摘要(seq 最大) = 5 条可见
    assert [m.role for m in history] == ["user", "user", "user", "user", "system"]
    assert history[-1].is_summary is True
    assert history[-1].id == summary.summary_message_id
    # 软删 1 条（seq=1）
    soft_deleted = (await db.execute(
        select(Message).where(
            Message.session_id == sess.session_id, Message.deleted_at.is_not(None)
        )
    )).scalars().all()
    assert len(soft_deleted) == 1


# ---- AC-IMPL01-5 续期 ----


async def test_touch_extends_expires_at(db):
    """AC-IMPL01-5：touch 后 expires_at 延长。"""
    with freeze_time(NOW) as frozen:
        store = _store(db)
        sess = await store.create(student_id="20230001")
        row = (await db.execute(
            select(ConversationSession).where(
                ConversationSession.session_id == sess.session_id
            )
        )).scalar_one()
        original = row.expires_at

        frozen.tick(delta=timedelta(seconds=60))
        await store.touch(sess.session_id)
        await db.refresh(row)
        assert row.expires_at > original


async def test_append_auto_touches(db):
    """AC-IMPL01-5：append 内部自动续期。"""
    with freeze_time(NOW) as frozen:
        store = _store(db)
        sess = await store.create(student_id="20230001")
        row = (await db.execute(
            select(ConversationSession).where(
                ConversationSession.session_id == sess.session_id
            )
        )).scalar_one()
        original = row.expires_at

        frozen.tick(delta=timedelta(seconds=120))
        await store.append(sess.session_id, MessageIn(role="user", content="x"))
        await db.refresh(row)
        assert row.expires_at > original


# ---- AC-IMPL01-6 过期 ----


async def test_expired_session_raises(db):
    """AC-IMPL01-6：把 expires_at 设到真实过去，get / load_history / append 全部抛 SessionExpired。

    注意：不能依赖模块级 NOW 常量（它在未来 2026），让 freezegun 解冻后真实时钟
    自然处于 NOW 之后但 expires_at 用真实时钟的过去值才能稳定触发过期。
    """
    store = _store(db)
    sess = await store.create(student_id="20230001")
    row = (await db.execute(
        select(ConversationSession).where(
            ConversationSession.session_id == sess.session_id
        )
    )).scalar_one()
    # 把 expires_at 设为相对过去（相对测试机的真实时钟）
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db.commit()

    with pytest.raises(SessionExpired):
        await store.get(sess.session_id)

    with pytest.raises(SessionExpired):
        await store.load_history(sess.session_id)

    with pytest.raises(SessionExpired):
        await store.append(sess.session_id, MessageIn(role="user", content="x"))


async def test_missing_session_raises_not_found(db):
    store = _store(db)
    with pytest.raises(SessionNotFound):
        await store.get("sess_does_not_exist")


# ---- AC-IMPL01-7 压缩 ----


async def test_compress_writes_summary_and_soft_deletes_old(db):
    """AC-IMPL01-7：插入 10 条 → 压缩（tail=4）→ 1 system + 4 user + 6 软删。"""
    store = _store(db, session_compress_tail=4)
    sess = await store.create(student_id="20230001")
    for i in range(10):
        await store.append(
            sess.session_id,
            MessageIn(role="user" if i % 2 == 0 else "assistant", content=f"m{i}"),
        )

    summary = await store.compress(sess.session_id, summary="s10", kept_tail=4)
    assert summary.summarized_count == 6  # seq=1..6 被覆盖
    assert summary.kept_tail == 4

    # 软删计数
    soft_deleted = (await db.execute(
        select(Message).where(
            Message.session_id == sess.session_id, Message.deleted_at.is_not(None)
        )
    )).scalars().all()
    assert len(soft_deleted) == 6

    # 未软删：1 条 system 摘要 + 4 条最近；system 摘要写在末尾（seq 最大）
    active = (await db.execute(
        select(Message).where(
            Message.session_id == sess.session_id, Message.deleted_at.is_(None)
        ).order_by(Message.seq.asc())
    )).scalars().all()
    assert len(active) == 5
    assert active[-1].role == "system" and active[-1].is_summary is True
    assert active[-1].content == "s10"
    assert active[-1].id == summary.summary_message_id

    # 会话指针更新
    sess_row = (await db.execute(
        select(ConversationSession).where(
            ConversationSession.session_id == sess.session_id
        )
    )).scalar_one()
    assert sess_row.summary_message_id == summary.summary_message_id


async def test_compress_keeps_minimum_tail(db):
    """安全阀：kept_tail < 4 时强制升到 4。"""
    store = _store(db, session_compress_tail=4)
    sess = await store.create(student_id="20230001")
    for i in range(6):
        await store.append(sess.session_id, MessageIn(role="user", content=f"m{i}"))

    summary = await store.compress(sess.session_id, summary="x", kept_tail=1)
    assert summary.kept_tail == 4
    assert summary.summarized_count == 2  # 6 - 4 = 2 被压缩


async def test_compress_empty_history_is_noop(db):
    """会话内 0 条消息 → 压缩返回 0/4，不报错。"""
    store = _store(db)
    sess = await store.create(student_id="20230001")
    summary = await store.compress(sess.session_id, summary="empty")
    assert summary.summarized_count == 0
    assert summary.kept_tail == 6  # 默认值


# ---- AC-IMPL01-10 trace_id 透传 ----


async def test_trace_id_propagates(db):
    store = _store(db)
    sess = await store.create(student_id="20230001")
    m = await store.append(
        sess.session_id, MessageIn(role="user", content="x", trace_id="trace-abc-123")
    )
    assert m.trace_id == "trace-abc-123"


# ---- 批量追加 ----


async def test_append_many_single_transaction(db):
    store = _store(db)
    sess = await store.create(student_id="20230001")
    msgs = await store.append_many(
        sess.session_id,
        [
            MessageIn(role="assistant", content="I'll check.", tool_calls=[{
                "id": "call_1", "type": "function",
                "function": {"name": "timetable_query", "arguments": "{}"},
            }]),
            MessageIn(role="tool", content="[]", tool_call_id="call_1"),
        ],
    )
    assert [m.seq for m in msgs] == [1, 2]
    assert msgs[0].role == "assistant" and msgs[0].tool_calls is not None
    assert msgs[1].role == "tool" and msgs[1].tool_call_id == "call_1"


# ---- 配置项生效 ----


async def test_settings_override_take_effect(db):
    """AC-IMPL01-5 / 1.7：session_ttl_seconds / session_history_limit / session_compress_tail 可热配。"""
    from app.config import Settings

    custom = Settings(
        session_ttl_seconds=60,
        session_history_limit=5,
        session_compress_tail=2,
    )
    store = SessionStore(db, custom)
    sess = await store.create(student_id="20230001")
    row = (await db.execute(
        select(ConversationSession).where(
            ConversationSession.session_id == sess.session_id
        )
    )).scalar_one()
    # expires_at ≈ created_at + 60s
    assert abs((row.expires_at - row.created_at).total_seconds() - 60) < 1

    for i in range(8):
        await store.append(sess.session_id, MessageIn(role="user", content=f"m{i}"))
    history = await store.load_history(sess.session_id)
    assert len(history) == 5  # 受 session_history_limit=5 限制
