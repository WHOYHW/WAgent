"""推送通道适配器单测（R2，验收：协议测试全绿）。

零网络：WecomWebhookAdapter 依赖注入 httpx.MockTransport 客户端（对齐
"假客户端注入而非 SDK mock"约定）；开发桩纯本地日志。零桩外 IO。
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.push import (
    DevStubPushAdapter,
    PushAdapter,
    PushError,
    PushNotConfiguredError,
    PushReceipt,
    WecomWebhookAdapter,
    build_push_adapter,
)
from app.config import Settings

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
CONF = Settings(push_wecom_webhook_url=WEBHOOK)


def make_wecom(handler) -> tuple[WecomWebhookAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WecomWebhookAdapter(client=client, settings=CONF), client


# ---- 工厂与协议形状 ----


def test_factory_without_config_returns_dev_stub():
    adapter = build_push_adapter(Settings())
    assert isinstance(adapter, DevStubPushAdapter)


def test_factory_with_config_returns_wecom():
    adapter = build_push_adapter(CONF)
    assert isinstance(adapter, WecomWebhookAdapter)
    assert adapter.channel == "wecom"


def test_both_implementations_satisfy_protocol():
    # runtime_checkable：方法面形状检查（鸭子类型约定的护栏）
    assert isinstance(DevStubPushAdapter(), PushAdapter)
    wecom, _ = make_wecom(lambda req: httpx.Response(200, json={"errcode": 0}))
    assert isinstance(wecom, PushAdapter)


def test_receipt_ok_and_is_degraded_semantics():
    assert PushReceipt(status="sent", channel="wecom").ok
    stub = PushReceipt(status="degraded", channel="stub")
    assert stub.ok and stub.is_degraded  # 桩发送=降级态，不冒充真实触达
    assert not PushReceipt(status="failed", channel="wecom").ok


# ---- 开发桩 ----


async def test_dev_stub_returns_degraded_receipt_for_any_channel():
    adapter = DevStubPushAdapter()
    r = await adapter.send("20230001", "wecom", "你有一本书将于 3 天后到期")
    assert r.ok and r.is_degraded and r.status == "degraded"
    assert r.channel == "wecom"  # 桩服务任意通道
    r2 = await adapter.send("20230001", "sms_fallback", "x")
    assert r2.channel == "sms_fallback"
    await adapter.aclose()
    await adapter.aclose()  # 幂等


# ---- 企微 webhook：回执语义 ----


async def test_wecom_send_success_shape_and_receipt():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    adapter, _ = make_wecom(handler)
    r = await adapter.send("20230001", "wecom", "明天 08:00 高等数学 @5-108")
    assert r.status == "sent" and r.ok and not r.is_degraded
    assert captured["url"] == WEBHOOK
    assert b'"msgtype":"text"' in captured["body"]
    assert "高等数学".encode() in captured["body"]


async def test_wecom_errcode_nonzero_is_failed_with_errmsg():
    adapter, _ = make_wecom(lambda req: httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook url"}))
    r = await adapter.send("20230001", "wecom", "x")
    assert r.status == "failed" and not r.ok
    assert "93000" in r.detail and "invalid webhook url" in r.detail


async def test_wecom_http_5xx_is_failed():
    adapter, _ = make_wecom(lambda req: httpx.Response(500, text="oops"))
    r = await adapter.send("20230001", "wecom", "x")
    assert r.status == "failed"
    assert "HTTP 500" in r.detail


async def test_wecom_timeout_is_failed_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    adapter, _ = make_wecom(handler)
    r = await adapter.send("20230001", "wecom", "x")
    assert r.status == "failed" and "超时" in r.detail  # 写路径不抛异常（R4.3 语义）


async def test_wecom_transport_error_is_failed_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conn refused", request=request)

    adapter, _ = make_wecom(handler)
    r = await adapter.send("20230001", "wecom", "x")
    assert r.status == "failed" and "不可用" in r.detail


async def test_wecom_non_json_receipt_is_failed():
    adapter, _ = make_wecom(lambda req: httpx.Response(200, text="<html>gateway</html>"))
    r = await adapter.send("20230001", "wecom", "x")
    assert r.status == "failed" and "非 JSON" in r.detail


# ---- 配置门控与通道校验 ----


async def test_wecom_unconfigured_raises_lazily_on_send():
    adapter = WecomWebhookAdapter(settings=Settings())  # 构造零副作用
    with pytest.raises(PushNotConfiguredError):
        await adapter.send("20230001", "wecom", "x")


async def test_wecom_channel_mismatch_is_push_error():
    adapter, _ = make_wecom(lambda req: httpx.Response(200, json={"errcode": 0}))
    with pytest.raises(PushError, match="通道不匹配"):
        await adapter.send("20230001", "sms_fallback", "x")


# ---- aclose 语义 ----


async def test_aclose_closes_held_client_and_is_idempotent():
    # 对齐 BaseAdapter 语义：aclose 关闭所持有的客户端（含注入）并置空引用
    adapter, injected = make_wecom(lambda req: httpx.Response(200, json={"errcode": 0}))
    await adapter.aclose()
    assert injected.is_closed
    assert adapter._client is None  # 置空 → 二次调用 no-op（幂等）
    await adapter.aclose()


async def test_aclose_closes_self_created_client():
    adapter = WecomWebhookAdapter(settings=CONF)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 0})

    # 懒创建发生在首次 send（MockTransport 无法注入自建路径，
    # 这里直接驱动 _get_client 验证懒创建 + aclose 闭环）
    client = await adapter._get_client()
    await adapter.aclose()
    assert client.is_closed
    assert adapter._client is None
    # 关闭后再次使用会重建新客户端（懒创建可重复）
    client2 = await adapter._get_client()
    assert client2 is not client
    await adapter.aclose()
