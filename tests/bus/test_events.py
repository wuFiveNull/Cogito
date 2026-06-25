"""Tests for cogito.bus.events — core message dataclasses."""

from __future__ import annotations

from datetime import datetime

import pytest

from cogito.bus.events import (
    AttachmentRef,
    DeliveryError,
    DeliveryReceipt,
    InboundControl,
    InboundMessage,
    MessagePayload,
    OutboundRequest,
    TextPart,
    TurnContext,
)


class TestTextPart:
    def test_create(self):
        t = TextPart(text="hello")
        assert t.text == "hello"

    def test_frozen(self):
        t = TextPart(text="hello")
        with pytest.raises(AttributeError):
            t.text = "world"

    def test_str_in_message_payload(self):
        payload = MessagePayload(parts=[TextPart(text="hi")])
        assert len(payload.parts) == 1
        assert payload.parts[0].text == "hi"


class TestAttachmentRef:
    def test_create(self):
        now = datetime.now()
        ref = AttachmentRef(
            id="att-1",
            content_type="image/png",
            size=1024,
            sha256="abc123",
            local_path="/tmp/img.png",
        )
        assert ref.id == "att-1"
        assert ref.content_type == "image/png"
        assert ref.size == 1024
        assert ref.sha256 == "abc123"
        assert ref.local_path == "/tmp/img.png"
        assert ref.remote_refs == {}

    def test_default_remote_refs(self):
        ref = AttachmentRef(
            id="att-2",
            content_type="text/plain",
            size=512,
            sha256="def456",
        )
        assert ref.remote_refs == {}

    def test_frozen(self):
        ref = AttachmentRef(id="a", content_type="t", size=1, sha256="s")
        with pytest.raises(AttributeError):
            ref.size = 999


class TestMessagePayload:
    def test_empty_parts(self):
        payload = MessagePayload(parts=[])
        assert payload.parts == []

    def test_mixed_parts(self):
        payload = MessagePayload(parts=[
            TextPart(text="hello"),
            AttachmentRef(id="att-1", content_type="img", size=100, sha256="s"),
        ])
        assert len(payload.parts) == 2
        assert isinstance(payload.parts[0], TextPart)
        assert isinstance(payload.parts[1], AttachmentRef)

    def test_frozen(self):
        payload = MessagePayload(parts=[TextPart(text="hi")])
        with pytest.raises(AttributeError):
            payload.parts = []


class TestInboundMessage:
    @pytest.fixture
    def payload(self):
        return MessagePayload(parts=[TextPart(text="hello")])

    @pytest.fixture
    def msg(self, payload):
        return InboundMessage(
            message_id="msg-1",
            external_message_id="ext-1",
            session_key="telegram:bot:123:456",
            channel="telegram",
            target="123",
            payload=payload,
            trace_id="trace-1",
            received_at=datetime(2026, 6, 23, 12, 0, 0),
        )

    def test_create(self, msg):
        assert msg.message_id == "msg-1"
        assert msg.external_message_id == "ext-1"
        assert msg.session_key == "telegram:bot:123:456"
        assert msg.channel == "telegram"
        assert msg.target == "123"
        assert msg.received_at == datetime(2026, 6, 23, 12, 0, 0)
        assert msg.occurred_at is None
        assert msg.reply_to is None
        assert msg.metadata == {}

    def test_frozen(self, msg):
        with pytest.raises(AttributeError):
            msg.channel = "cli"

    def test_with_optional_fields(self, payload):
        msg = InboundMessage(
            message_id="msg-2",
            external_message_id=None,
            session_key="cli:default:term-1",
            channel="cli",
            target="term-1",
            payload=payload,
            trace_id="trace-2",
            received_at=datetime.now(),
            occurred_at=datetime.now(),
            reply_to="prev-msg-id",
            metadata={"key": "value"},
        )
        assert msg.external_message_id is None
        assert msg.reply_to == "prev-msg-id"
        assert msg.metadata == {"key": "value"}


class TestInboundControl:
    def test_interrupt(self):
        ctrl = InboundControl(
            control_id="ctrl-1",
            kind="interrupt",
            session_key="telegram:bot:123:456",
            channel="telegram",
            trace_id="trace-1",
        )
        assert ctrl.kind == "interrupt"
        assert ctrl.session_key == "telegram:bot:123:456"

    def test_shutdown(self):
        ctrl = InboundControl(
            control_id="ctrl-2",
            kind="shutdown",
            session_key=None,
            channel="cli",
            trace_id="trace-2",
        )
        assert ctrl.kind == "shutdown"
        assert ctrl.session_key is None

    def test_frozen(self):
        ctrl = InboundControl(
            control_id="c1", kind="interrupt", session_key=None,
            channel="t", trace_id="t",
        )
        with pytest.raises(AttributeError):
            ctrl.kind = "shutdown"


class TestTurnContext:
    def test_create(self):
        ctx = TurnContext(
            turn_id="turn-1",
            trace_id="trace-1",
            session_key="telegram:bot:123:456",
            trigger_message_id="msg-1",
            origin="inbound",
            started_at=datetime(2026, 6, 23, 12, 0, 0),
        )
        assert ctx.origin == "inbound"
        assert ctx.trigger_message_id == "msg-1"

    def test_origins(self):
        for origin in ("inbound", "proactive", "system"):
            ctx = TurnContext(
                turn_id="t", trace_id="t", session_key="s",
                trigger_message_id=None, origin=origin,
                started_at=datetime.now(),
            )
            assert ctx.origin == origin

    def test_frozen(self):
        ctx = TurnContext(
            turn_id="t", trace_id="t", session_key="s",
            trigger_message_id=None, origin="inbound",
            started_at=datetime.now(),
        )
        with pytest.raises(AttributeError):
            ctx.origin = "system"


class TestOutboundRequest:
    def test_create(self):
        payload = MessagePayload(parts=[TextPart(text="reply")])
        req = OutboundRequest(
            outbound_id="out-1",
            channel="telegram",
            target="123",
            payload=payload,
            origin="reply",
            trace_id="trace-1",
        )
        assert req.origin == "reply"
        assert req.priority == 100
        assert req.session_key is None
        assert req.turn_id is None
        assert req.idempotency_key is None
        assert req.created_at is None

    def test_priority(self):
        payload = MessagePayload(parts=[TextPart(text="urgent")])
        req = OutboundRequest(
            outbound_id="out-2",
            channel="telegram",
            target="123",
            payload=payload,
            origin="proactive",
            trace_id="trace-2",
            priority=50,
        )
        assert req.priority == 50

    def test_origins(self):
        payload = MessagePayload(parts=[TextPart(text="x")])
        for origin in ("reply", "proactive", "tool"):
            req = OutboundRequest(
                outbound_id="o", channel="c", target="t",
                payload=payload, origin=origin, trace_id="tr",
            )
            assert req.origin == origin

    def test_frozen(self):
        payload = MessagePayload(parts=[TextPart(text="x")])
        req = OutboundRequest(
            outbound_id="o", channel="c", target="t",
            payload=payload, origin="reply", trace_id="tr",
        )
        with pytest.raises(AttributeError):
            req.origin = "proactive"


class TestDeliveryReceipt:
    def test_accepted(self):
        receipt = DeliveryReceipt(
            outbound_id="out-1",
            status="accepted",
        )
        assert receipt.status == "accepted"
        assert receipt.attempts == 0

    def test_delivered(self):
        receipt = DeliveryReceipt(
            outbound_id="out-1",
            status="delivered",
            external_message_id="ext-msg-1",
            attempts=1,
        )
        assert receipt.external_message_id == "ext-msg-1"

    def test_failed(self):
        receipt = DeliveryReceipt(
            outbound_id="out-1",
            status="failed",
            attempts=3,
            error_code="rate_limited",
            error_message="Too many requests",
        )
        assert receipt.error_code == "rate_limited"

    def test_status_values(self):
        for status in ("accepted", "delivered", "retrying", "failed", "dead"):
            receipt = DeliveryReceipt(outbound_id="o", status=status)
            assert receipt.status == status


class TestDeliveryError:
    def test_retryable(self):
        err = DeliveryError(
            code="timeout",
            message="Connection timed out",
            retryable=True,
            retry_after=5.0,
        )
        assert err.retryable is True
        assert err.retry_after == 5.0
        assert str(err) == "[timeout] Connection timed out"

    def test_non_retryable(self):
        err = DeliveryError(
            code="invalid_target",
            message="Chat not found",
            retryable=False,
        )
        assert err.retryable is False
        assert err.retry_after is None

    def test_is_exception(self):
        err = DeliveryError(code="e", message="err", retryable=False)
        assert isinstance(err, Exception)
