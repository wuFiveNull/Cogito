"""GatewayClient transport contract tests for split-process deployment."""

from __future__ import annotations

import json

import httpx

from cogito.service.http_gateway_client import HttpGatewayClient


TARGET = json.dumps(
    {
        "adapter_id": "qq-main",
        "channel": "onebot",
        "conversation_id": "123",
        "delivery_id": "del-1",
        "attempt_id": "att-1",
    }
)


def test_send_serializes_bridge_v1_and_preserves_receipt() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "platform_message_id": "pm-1",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = HttpGatewayClient("http://gateway", client=client)
    result = gateway.send(TARGET, "hello", "idem-1")
    assert result.status == "success"
    assert result.platform_message_id == "pm-1"
    assert seen["schema_version"] == "1"
    assert seen["delivery_id"] == "del-1"
    assert seen["content"][0]["data"] == "hello"


def test_edit_delete_reconcile_actions() -> None:
    actions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        actions.append(payload["action"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "platform_message_id": payload.get("platform_message_id"),
            },
        )

    gateway = HttpGatewayClient(
        "http://gateway",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    gateway.start_placeholder(TARGET, "...", "op-1")
    gateway.edit(TARGET, "pm-1", "partial", 2, "op-2")
    gateway.finish(TARGET, "pm-1", "done", 3, "op-3")
    gateway.delete(TARGET, "pm-1", 4, "op-4")
    gateway.reconcile(TARGET, "pm-1", "op-5")
    assert actions == [
        "start_placeholder",
        "append_or_replace",
        "finish",
        "delete",
        "reconcile",
    ]


def test_timeout_is_unknown_not_retried_blindly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lost response", request=request)

    gateway = HttpGatewayClient(
        "http://gateway",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = gateway.send(TARGET, "hello", "idem-timeout")
    assert result.status == "unknown"
    assert result.error_code == "timeout"


def test_health_failure_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    gateway = HttpGatewayClient(
        "http://gateway",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert gateway.health()["status"] == "unavailable"
