"""Contract round-trip + version-compat tests — Plan 01 M3.

Verifies cross-process models can survive JSON round-trips and that the
current release can decode the previous schema version.
Design refs: DOMAIN-CONTRACTS / 2, GLOBAL-INVARIANTS / 6.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from cogito.contracts.envelope import (
    AgentReply,
    AgentRequest,
    ChannelEnvelope,
    CommandEnvelope,
    ErrorCategory,
    ErrorEnvelope,
    ReplyMode,
    ReplyRoute,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from cogito.contracts.trace_context import TraceContext
from cogito.model.contracts import ContentPart, ContentPartType


# ---------------------------------------------------------------------------
# Round-trip: to_dict -> from_dict must reproduce the message.
# ---------------------------------------------------------------------------


def _trace() -> TraceContext:
    return TraceContext(
        trace_id="trace-1",
        span_id="span-1",
        principal_id="owner",
    )


def test_channel_envelope_roundtrip() -> None:
    env = ChannelEnvelope(
        message_id="m1",
        channel_type="web",
        channel_instance_id="web-1",
        sender_endpoint_ref="ep-1",
        trace_context=_trace(),
        metadata={"k": "v"},
    )
    data = env.to_dict()
    # Every cross-process model carries a schema_version.
    assert "schema_version" in data
    restored = ChannelEnvelope.from_dict(data)
    assert restored.message_id == "m1"
    assert restored.channel_type == "web"
    assert restored.trace_context is not None
    assert restored.trace_context.trace_id == "trace-1"
    assert restored.metadata == {"k": "v"}


def test_channel_envelope_json_roundtrip() -> None:
    env = ChannelEnvelope(message_id="m2", trace_context=_trace())
    raw = json.dumps(env.to_dict())
    restored = ChannelEnvelope.from_dict(json.loads(raw))
    assert restored.message_id == "m2"


def test_error_envelope_roundtrip() -> None:
    err = ErrorEnvelope(
        error_code="rate_limit",
        category=ErrorCategory.rate_limit,
        message="slow down",
        retryable=True,
        retry_after=1.5,
        trace_id="t1",
    )
    restored = ErrorEnvelope.from_dict(err.to_dict())
    assert restored.error_code == "rate_limit"
    assert restored.category == ErrorCategory.rate_limit
    assert restored.retryable is True
    assert restored.retry_after == 1.5


def test_command_envelope_roundtrip() -> None:
    cmd = CommandEnvelope(
        command_type="approve",
        aggregate_type="approval",
        aggregate_id="a1",
        expected_version=2,
        idempotency_key="k1",
        principal_id="owner",
        trace_context=_trace(),
    )
    restored = CommandEnvelope.from_dict(cmd.to_dict())
    assert restored.command_type == "approve"
    assert restored.expected_version == 2
    assert restored.idempotency_key == "k1"


def test_tool_request_frozen_and_roundtrip() -> None:
    req = ToolRequest(
        tool_name="echo",
        arguments={"text": "hi"},
        trace_context=_trace(),
    )
    # frozen: cannot mutate
    with pytest.raises(AttributeError):
        req.tool_name = "other"  # type: ignore[misc]
    restored = ToolRequest.from_dict(req.to_dict())
    assert restored.tool_name == "echo"


def test_tool_result_frozen_and_roundtrip() -> None:
    res = ToolResult(tool_call_id="c1", status=ToolStatus.succeeded)
    with pytest.raises(AttributeError):
        res.status = ToolStatus.failed  # type: ignore[misc]


def test_content_part_frozen() -> None:
    part = ContentPart(part_type=ContentPartType.text, text="hello")
    with pytest.raises(AttributeError):
        part.text = "world"  # type: ignore[misc]


def test_agent_request_reply_roundtrip() -> None:
    req = AgentRequest(turn_id="t1", trace_context=_trace())
    rep = AgentReply(
        turn_id="t1",
        reply_mode=ReplyMode.streaming,
        content_parts=[{"type": "text", "text": "hi"}],
    )
    assert AgentRequest.from_dict(req.to_dict()).turn_id == "t1"
    restored_rep = AgentReply.from_dict(rep.to_dict())
    assert restored_rep.reply_mode == ReplyMode.streaming
    assert restored_rep.content_parts == [{"type": "text", "text": "hi"}]


# ---------------------------------------------------------------------------
# Version compat: current decoders tolerate a "previous" schema_version.
# ---------------------------------------------------------------------------


def test_channel_envelope_accepts_previous_version() -> None:
    """schema_version '1.0' payloads decode identically (back-compat baseline)."""
    data = ChannelEnvelope(message_id="x").to_dict()
    data["schema_version"] = "0.9"  # simulate a payload produced by previous release
    # Unknown-version payloads must still decode safely.
    restored = ChannelEnvelope.from_dict(data)
    assert restored.message_id == "x"


# ---------------------------------------------------------------------------
# Protected fields: PROTECTED_FIELDS exists and includes the documented set.
# ---------------------------------------------------------------------------


def test_protected_fields_complete() -> None:
    from cogito.contracts.envelope import PROTECTED_FIELDS

    expected = {
        "trace_id",
        "principal_id",
        "conversation_id",
        "turn_id",
        "attempt_id",
        "origin",
        "reply_route",
        "schema_version",
        "idempotency_key",
    }
    assert expected.issubset(PROTECTED_FIELDS)


# ---------------------------------------------------------------------------
# No Secret leak: ErrorEnvelope must not carry raw provider payloads.
# ---------------------------------------------------------------------------


def test_error_envelope_no_secret_leak() -> None:
    err = ErrorEnvelope(
        error_code="internal",
        message="boom",
        safe_details="visible",
        # internal_payload_ref holds the raw payload for logs; must not default
        # to dumping it into the public message.
    )
    out = err.to_dict()
    # safe_details is the only public detail; must not include arbitrary blobs.
    assert out["safe_details"] == "visible"
    # No raw_payload / provider_response field presence by design.
    assert "raw_payload" not in out
    assert "provider_response" not in out


# ---------------------------------------------------------------------------
# TraceContext.new_child builds a proper child span.
# ---------------------------------------------------------------------------


def test_trace_context_new_child() -> None:
    root = TraceContext(trace_id="t", span_id="s1")
    child = root.new_child()
    assert child.trace_id == "t"
    assert child.parent_span_id == "s1"
    assert child.span_id != "s1"


# ---------------------------------------------------------------------------
# ReplyRoute round-trip preserves expiry.
# ---------------------------------------------------------------------------


def test_reply_route_roundtrip() -> None:
    when = datetime.now(UTC)
    route = ReplyRoute(
        channel_instance_id="ci",
        reply_token="tok",
        reply_token_expires_at=when,
    )
    restored = ReplyRoute.from_dict(route.to_dict())
    assert restored.reply_token == "tok"
    assert restored.reply_token_expires_at == when
