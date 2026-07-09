"""Tests for Plan 05 M2 / T2 — LangBot Bridge versioned DTO.

覆盖：JSON round-trip、V0→V1 升级、未知字段忽略、缺必填字段、错误码。
"""

from __future__ import annotations

import json

import pytest

from cogito.contracts.bridge_dto import (
    BridgeError,
    ContentPart,
    DeliveryOperation,
    InboundMessage,
    InboundMessageV0,
    ReplyRoute,
    TargetSnapshot,
    TraceContext,
    decode_inbound,
)


# ── 9.1: InboundMessage round-trip ─────────────────────────


class TestInboundMessageRoundTrip:
    """br-dto-01: InboundMessage JSON round-trip。"""

    def test_round_trip(self):
        msg = InboundMessage(
            event_id="evt-1",
            channel_name="onebot",
            instance_id="qq-main",
            conversation_ref="conv-123",
            sender_ref="user-456",
            content_parts=[ContentPart(type="text", data="hello")],
            reply_route=ReplyRoute(
                adapter_id="qq-main",
                channel_type="onebot",
                conversation_id="conv-123",
                endpoint_ref="ep-1",
            ),
            trace=TraceContext(trace_id="trace-1"),
            received_at="2026-07-07T12:00:00+00:00",
        )
        json_str = msg.to_json()
        restored = InboundMessage.from_json(json_str)
        assert restored.event_id == "evt-1"
        assert restored.content_parts[0].data == "hello"
        assert restored.reply_route.endpoint_ref == "ep-1"
        assert restored.schema_version == "1"

    def test_unknown_fields_ignored(self):
        """br-dto-03: 未知字段忽略或保留。"""
        data = {
            "schema_version": "1",
            "event_id": "evt-x",
            "unknown_field": "should-be-ignored",
            "content_parts": [],
        }
        msg = InboundMessage.from_json(data)
        assert msg.event_id == "evt-x"
        # 未知字段不导致失败

    def test_missing_optional_fields_default(self):
        """br-dto-04: 缺必填字段返回安全默认值。"""
        msg = InboundMessage.from_json("{}")
        assert msg.schema_version == "1"
        assert msg.content_parts == []
        assert msg.trust_label == "external_untrusted"


# ── 9.2: V0 → V1 升级 ─────────────────────────────────────


class TestV0ToV1Upgrade:
    """br-dto-02: V0→V1 升级。"""

    def test_v0_to_v1(self):
        v0 = InboundMessageV0(
            event_id="old-1",
            channel="onebot",
            instance="qq-main",
            conversation_id="conv-99",
            sender_id="user-10",
            text="hello from v0",
            timestamp=1700000000,
        )
        v1 = v0.to_v1()
        assert v1.schema_version == "1"
        assert v1.event_id == "old-1"
        assert v1.conversation_ref == "conv-99"
        assert v1.content_parts[0].data == "hello from v0"

    def test_decode_v0(self):
        """decode_inbound 自动检测 V0 并升级。"""
        v0_json = json.dumps({
            "event_id": "legacy-1",
            "channel": "onebot",
            "text": "legacy text",
        })
        msg = decode_inbound(v0_json)
        assert msg.schema_version == "1"
        assert msg.event_id == "legacy-1"

    def test_decode_v1(self):
        """decode_inbound 直接解析 V1。"""
        v1_json = json.dumps({
            "schema_version": "1",
            "event_id": "v1-evt",
            "content_parts": [],
        })
        msg = decode_inbound(v1_json)
        assert msg.event_id == "v1-evt"


# ── 9.3: DeliveryOperation round-trip ──────────────────────


class TestDeliveryOperation:
    def test_round_trip(self):
        op = DeliveryOperation(
            operation_id="op-1",
            delivery_id="del-1",
            attempt_id="att-1",
            operation_seq=3,
            idempotency_key="idem-1",
            target_snapshot=TargetSnapshot(
                adapter_id="qq-main",
                channel_type="onebot",
                conversation_id="conv-1",
                endpoint_ref="ep-1",
            ),
            action="append_or_replace",
            content=[ContentPart(type="text", data="partial text")],
        )
        restored = DeliveryOperation.from_json(op.to_json())
        assert restored.delivery_id == "del-1"
        assert restored.operation_seq == 3
        assert restored.action == "append_or_replace"
        assert restored.target_snapshot.endpoint_ref == "ep-1"


# ── 9.4: BridgeError ───────────────────────────────────────


class TestBridgeError:
    def test_error_serialization(self):
        err = BridgeError(
            error_code="route_expired",
            message="Reply token expired",
        )
        data = json.loads(err.to_json())
        assert data["error_code"] == "route_expired"
        # 错误响应不携带 Secret
        assert "secret" not in data["message"].lower()
