"""推送通道适配器（R2，任务设计 09 §2.5/F4）。

完全对齐 edu/lib 适配器模式：
- **依赖注入构造**（httpx client + Settings），客户端懒创建；
- **未配置惰性校验**：``PushNotConfiguredError`` 镜像 ``SourceNotConfiguredError``，
  构造零副作用（对齐 LLM 适配器"构造无副作用"约定）；
- **aclose() 对齐 BaseAdapter**：关闭当前持有的客户端并置空引用（幂等），
  应用 shutdown 时调用防连接泄漏；
- **写路径不重试**（R4.3 同源约定：重试 = 重复触达风险），传输失败不抛异常，
  返回 ``failed`` 回执——重试/换道语义归引擎降级链（09 §2.5）。

降级链骨架：回执 ``status`` 直接映射 ``reminder_dispatch.status``
（sent / failed / degraded），开发桩回执 ``degraded`` 落 ``is_degraded=true``
——桩发送不冒充真实触达，效果统计天然区分。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from ..config import Settings, settings as default_settings
from ..schemas.reminder import ReminderChannel
from ..security import mask_student_id

logger = logging.getLogger(__name__)


class PushNotConfiguredError(Exception):
    """推送通道未配置（PUSH_WECOM_WEBHOOK_URL 为空）。惰性抛出，不重试。"""


class PushError(Exception):
    """推送调用的编程错误（通道不匹配等）——区别于传输失败（返回 failed 回执）。"""


@dataclass(frozen=True)
class PushReceipt:
    """推送回执（R4 落库映射零思考：status 直接对应 dispatch.status）。"""

    status: str            # "sent" / "failed" / "degraded"
    channel: str
    detail: str = ""       # 回执摘要或失败原因（只含元数据，不含 content）

    @property
    def ok(self) -> bool:
        return self.status != "failed"

    @property
    def is_degraded(self) -> bool:
        return self.status == "degraded"


@runtime_checkable
class PushAdapter(Protocol):
    """推送通道协议（鸭子类型，对齐 LLMClient Protocol 约定）。"""

    channel: str

    async def send(self, student_id: str, channel: str, content: str) -> PushReceipt: ...

    async def aclose(self) -> None: ...


class DevStubPushAdapter:
    """开发桩：不触达任何真实通道，记日志即回执 ``degraded``。

    桩不是"假装成功"——回执显式降级态，验收与效果统计不会把桩触达
    误当真实触达（C4 无静默失败）。日志含 content（开发期可见性优先，
    提醒内容为课表/到期日等非敏感明细，与成绩类 R3 红线无关）。
    """

    channel = "stub"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or default_settings

    async def send(self, student_id: str, channel: str, content: str) -> PushReceipt:
        logger.info(
            "推送[开发桩] student=%s channel=%s bytes=%d content=%s",
            mask_student_id(student_id),
            channel,
            len(content.encode("utf-8")),
            content,
        )
        return PushReceipt(status="degraded", channel=channel, detail="dev-stub")

    async def aclose(self) -> None:
        return None


class WecomWebhookAdapter:
    """企业微信群机器人 webhook 通道（阶段一唯一真实通道）。

    请求体 ``{"msgtype": "text", "text": {"content": ...}}``；回执
    ``errcode == 0`` → sent，非 0 → failed（含 errmsg）。HTTP/传输失败
    一律返回 failed 回执，不抛异常、不重试。
    """

    channel = ReminderChannel.WECOM.value

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self.settings = settings or default_settings

    def _webhook_url(self) -> str:
        url = self.settings.push_wecom_webhook_url
        if not url:
            raise PushNotConfiguredError("PUSH_WECOM_WEBHOOK_URL 未配置")
        return url

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.push_timeout_seconds)
        return self._client

    async def send(self, student_id: str, channel: str, content: str) -> PushReceipt:
        if channel != self.channel:
            raise PushError(f"通道不匹配：{self.channel} 适配器收到 {channel!r}")
        url = self._webhook_url()  # 未配置：惰性显式失败，不落静默降级
        try:
            client = await self._get_client()
            resp = await client.post(
                url, json={"msgtype": "text", "text": {"content": content}}
            )
            resp.raise_for_status()
            payload = json.loads(resp.text)
        except httpx.HTTPStatusError as exc:
            return PushReceipt(
                status="failed",
                channel=self.channel,
                detail=f"企微 webhook 拒绝（HTTP {exc.response.status_code}）",
            )
        except httpx.TimeoutException:
            return PushReceipt(status="failed", channel=self.channel, detail="企微 webhook 超时")
        except httpx.HTTPError:
            return PushReceipt(status="failed", channel=self.channel, detail="企微 webhook 不可用")
        except ValueError:
            return PushReceipt(
                status="failed", channel=self.channel, detail="企微 webhook 回执非 JSON"
            )
        errcode = payload.get("errcode")
        if errcode != 0:
            errmsg = payload.get("errmsg", "")
            logger.warning("企微推送失败 errcode=%s", errcode)  # 只记元数据，不记 content
            return PushReceipt(
                status="failed",
                channel=self.channel,
                detail=f"企微 errcode={errcode} {errmsg}".strip(),
            )
        return PushReceipt(status="sent", channel=self.channel, detail="wecom ok")

    async def aclose(self) -> None:
        """对齐 BaseAdapter：关闭持有的客户端并置空引用（幂等）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_push_adapter(
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> PushAdapter:
    """工厂（镜像 make_cache 风格）：配置了企微 webhook → 真实通道；
    否则开发桩（degraded 回执，无静默失败）。"""
    s = settings or default_settings
    if s.push_wecom_webhook_url:
        return WecomWebhookAdapter(client=client, settings=s)
    return DevStubPushAdapter(settings=s)
