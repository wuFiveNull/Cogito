# tests/turns/test_runner.py

from __future__ import annotations

import pytest

from cogito.bus.events import TextPart


@pytest.mark.asyncio
async def test_turn_runner_bridges_inbound_to_outbound(
    turn_runner,
    inbound_message,
    channel_registry,
) -> None:
    """An InboundMessage flows through TurnRunner and produces an outbound reply."""
    await turn_runner.run(inbound_message)

    # The stub channel should have received one outbound
    cli = channel_registry.get("cli")
    assert len(cli.sent) == 1, "Expected one outbound message"

    outbound = cli.sent[0]
    assert outbound.origin == "reply"
    assert outbound.session_key == "test:user:default"
    assert outbound.channel == "cli"

    # Check the text was echoed back
    text_parts = [
        p.text for p in outbound.payload.parts if isinstance(p, TextPart)
    ]
    assert "Hello, agent!" in "".join(text_parts)


@pytest.mark.asyncio
async def test_turn_runner_sets_correct_routing(
    turn_runner,
    inbound_message,
    channel_registry,
) -> None:
    """Routing fields (channel, target, session) must match the original message."""
    await turn_runner.run(inbound_message)

    cli = channel_registry.get("cli")
    outbound = cli.sent[0]

    assert outbound.channel == "cli"
    assert outbound.target == "user-1"
    assert outbound.session_key == "test:user:default"


@pytest.mark.asyncio
async def test_turn_runner_unknown_channel_does_not_crash(
    turn_runner,
    inbound_message,
    channel_registry,
) -> None:
    """If the channel is not registered, the turn should still complete."""
    # Change channel to one that doesn't exist
    msg = inbound_message
    from copy import deepcopy

    msg_no_channel = deepcopy(msg)
    object.__setattr__(msg_no_channel, "channel", "nonexistent")

    # Should not raise
    await turn_runner.run(msg_no_channel)


@pytest.mark.asyncio
async def test_full_chain_with_agent_kernel(
    turn_runner,
    inbound_message,
    channel_registry,
) -> None:
    """End-to-end: InboundMessage → TurnRunner → Kernel → DeliveryManager → Channel."""
    await turn_runner.run(inbound_message)

    cli = channel_registry.get("cli")
    assert len(cli.sent) == 1

    outbound = cli.sent[0]
    text_parts = [
        p.text for p in outbound.payload.parts if isinstance(p, TextPart)
    ]
    reply = "".join(text_parts)

    # The stub phase echoes the input
    assert "Hello, agent!" in reply
    assert "Echo:" in reply
