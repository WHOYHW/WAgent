"""会话身份层专属测试（P3 步骤 1 / 约定③：student_id 由 JWT 注入）。

约定③是四条设计约定之一，值得专属测试面。全部走 ``httpx.ASGITransport``
既有模式（不跑 lifespan，规避 init_db 触碰 PG）＋ dependency_overrides，
零 monkeypatch。覆盖七类威胁面：

1. 无令牌 → 401；
2. 篡改令牌（签名破坏）→ 401；
3. 过期令牌 → 401；
4. 缺 sub 的令牌 → 401；
5. 合法令牌 → 令牌身份到达适配器（模型伪造的 args.student_id 被剥离）；
6. 请求体伪造 student_id 字段被忽略（会话归属仍是令牌身份）；
7. 工具入参 schema（Function Calling 导出）不含 student_id —— 模型想
   伪造都无从下手（导出层剥离 + 编排层覆盖回填，两层防线）；
8. 访问他人 reminder 反馈 → 404（归属校验，不泄露存在性）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import jwt
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, settings
from app.conversation import SessionStore
from app.db import get_db, make_engine
from app.main import app, get_orchestrator
from app.orchestrator import Orchestrator
from app.orm import Base, ConversationSession, ReminderDispatch
from app.security import encode_token
from app.tools import build_registry
from tests.conftest import StubEduAdapter, make_services
from tests.test_api_reminder_feedback import seed_dispatch
from tests.test_orchestrator import FakeLLM, text_script, tool_call_script

STUDENT = "20230001"
OTHER = "20239999"
# 模型在工具参数里伪造的身份（约定③：必须被剥离，以令牌为准）
FORGED = "99999999"


# ---- 夹具与辅助 ----


@pytest_asyncio.fixture
async def session_factory():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory):
    """ASGI 测试客户端；get_db 覆盖注入内存库（feedback 归属用例消费）。"""

    async def override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c
    app.dependency_overrides.clear()


def install_orch(db, llm, *, edu: StubEduAdapter | None = None) -> None:
    """覆盖 ``get_orchestrator``：内存库 + 桩适配器 + FakeLLM（AC-8 姿势）。"""

    def override() -> Orchestrator:
        services = make_services(db, edu=edu)
        registry = build_registry(services)
        store = SessionStore(db, Settings())
        return Orchestrator(store, registry, llm)

    app.dependency_overrides[get_orchestrator] = override


class RecordingEdu(StubEduAdapter):
    """记录到达适配器的 student_id（注入正确性的观测点）。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen_student_ids: list[str] = []

    async def fetch_timetable(self, student_id, start_date, end_date, course_name=None):
        self.seen_student_ids.append(student_id)
        return await super().fetch_timetable(
            student_id, start_date, end_date, course_name
        )


def tamper(token: str) -> str:
    """破坏签名段（payload 合法但签名失配）。"""
    return token[:-4] + "XXXX"


# ---------------------------------------------------------------------------
# 401 组：无令牌 / 篡改 / 过期 / 缺 sub
# ---------------------------------------------------------------------------


async def test_no_token_401(client):
    resp = await client.post("/agent/chat", json={"content": "你好"})
    assert resp.status_code == 401


async def test_tampered_token_401(client):
    resp = await client.post(
        "/agent/chat",
        json={"content": "你好"},
        headers={"Authorization": f"Bearer {tamper(encode_token(STUDENT))}"},
    )
    assert resp.status_code == 401


async def test_expired_token_401(client):
    resp = await client.post(
        "/agent/chat",
        json={"content": "你好"},
        headers={
            "Authorization": "Bearer "
            + encode_token(STUDENT, expires_minutes=-1)
        },
    )
    assert resp.status_code == 401


async def test_token_without_sub_401(client):
    """签名合法但缺 sub：身份层拒绝（令牌缺少主体身份）。"""
    token = jwt.encode(
        {"iat": datetime.now(UTC)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    resp = await client.post(
        "/agent/chat",
        json={"content": "你好"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 注入正确性：令牌身份到达适配器，伪造身份被剥离
# ---------------------------------------------------------------------------


async def test_valid_token_injects_identity_and_strips_forged_args(db, client):
    """合法令牌 + 模型在 args 里伪造 student_id → 适配器只见到令牌身份。"""
    edu = RecordingEdu()
    llm = FakeLLM([
        tool_call_script("timetable_query", {
            "student_id": FORGED,  # 模型伪造身份
            "start_date": "2026-10-14",
            "end_date": "2026-10-15",
        }),
    ])
    install_orch(db, llm, edu=edu)

    resp = await client.post(
        "/agent/chat",
        json={"content": "我明天有什么课"},
        headers={"Authorization": f"Bearer {encode_token(STUDENT)}"},
    )

    assert resp.status_code == 200
    assert edu.seen_student_ids == [STUDENT]  # 伪造值被剥离，令牌身份注入
    assert FORGED not in edu.seen_student_ids


async def test_body_forged_student_id_field_ignored(db, client):
    """请求体塞 student_id 字段 → 被忽略（ChatRequest 无此字段，静默丢弃），
    会话归属仍按令牌身份建立。"""
    llm = FakeLLM([text_script("你好。")])
    install_orch(db, llm)

    resp = await client.post(
        "/agent/chat",
        json={"content": "你好", "student_id": OTHER},  # 伪造请求体身份
        headers={"Authorization": f"Bearer {encode_token(STUDENT)}"},
    )

    assert resp.status_code == 200
    row = (
        await db.execute(
            select(ConversationSession).order_by(ConversationSession.id.desc())
        )
    ).scalars().first()
    assert row is not None
    assert row.student_id == STUDENT  # 归属 = 令牌身份，而非请求体字段


# ---------------------------------------------------------------------------
# Schema 导出：student_id 不出现在 Function Calling Schema（约定③）
# ---------------------------------------------------------------------------


async def test_tool_schemas_hide_student_id(db):
    """全部注册工具的导出 schema 均不含 student_id 参数（导出层剥离）。"""
    registry = build_registry(make_services(db))
    specs = registry.specs()
    assert specs  # 防空转：注册表必须非空

    for spec in specs:
        schema = spec.parameters
        assert "student_id" not in schema.get("properties", {}), spec.name
        assert "student_id" not in (schema.get("required") or []), spec.name
        # 导出物（OpenAI tools 入参）同样干净——同一导出边界
        fn = spec.to_openai_function()["function"]
        assert "student_id" not in fn["parameters"].get("properties", {}), spec.name


# ---------------------------------------------------------------------------
# 归属校验：访问他人 reminder 反馈 → 404 且零写入
# ---------------------------------------------------------------------------


async def test_other_students_feedback_404_no_write(client, session_factory):
    """他人令牌访问他人 dispatch → 404（不泄露存在性），行零写入。"""
    await seed_dispatch(session_factory)

    resp = await client.post(
        "/agent/reminders/feedback",
        json={"dispatch_id": 1, "action": "viewed"},
        headers={"Authorization": f"Bearer {encode_token(OTHER)}"},
    )

    assert resp.status_code == 404
    row = (
        await session_factory()
        .execute(select(ReminderDispatch).where(ReminderDispatch.id == 1))
    ).scalar_one()
    assert row.opened_at is None and row.action_taken is None
