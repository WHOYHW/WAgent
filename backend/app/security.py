"""会话身份层（C2 §3.2.2 R1.2 / 文档 12 §7 L4）。

核心约束：``student_id`` 由会话令牌（JWT）注入，**不作为模型可填参数**。
- :func:`get_current_student` 是 FastAPI 依赖，从 Authorization 头解出 ``sub``。
- :func:`mask_student_id` 用于审计/工单脱敏（R3.4）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer(auto_error=False)


def encode_token(student_id: str, *, expires_minutes: int | None = None) -> str:
    """签发会话令牌，``sub`` 即 student_id。"""
    minutes = expires_minutes or settings.jwt_expire_minutes
    now = datetime.now(UTC)
    payload = {
        "sub": student_id,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """解码并校验令牌，失败抛 ``jwt.InvalidTokenError``。"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def get_current_student(
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """从会话令牌注入 student_id。请求体/参数中的任何同名字段均被忽略。"""
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少会话令牌",
        )
    try:
        payload = decode_token(cred.credentials)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="会话令牌无效或已过期",
        ) from exc
    student_id = payload.get("sub")
    if not student_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌缺少主体身份",
        )
    return student_id


def mask_student_id(student_id: str) -> str:
    """脱敏学号，形如 ``2023****56``（R3.4）。"""
    if not student_id:
        return student_id
    n = len(student_id)
    if n <= 6:
        return student_id[0] + "*" * max(1, n - 2) + student_id[-1]
    return student_id[:4] + "*" * (n - 6) + student_id[-2:]


def make_idempotency_key(session_id: str, barcodes: list[str], date_str: str) -> str:
    """续借幂等键 = sha256(session_id + 排序后条码 + 日期)（R2.3）。"""
    payload = "|".join([session_id, "|".join(sorted(barcodes)), date_str])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
