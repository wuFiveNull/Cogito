"""Tests for cogito.bus.inbound — InboundBus and InboundPort."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from cogito.bus.events import InboundControl, InboundMessage, MessagePayload, TextPart
from cogito.bus.inbound import InboundBus


@pytest.fixture
def payload():
    return MessagePayload(parts=[TextPart(text="hello")])


@pytest.fixture
def message(payload):
    return InboundMessage(
        message_id="msg-1",
        external_message_id="ext-1",
        session_key="test:session:1",
        channel="test",
        target="target-1",
        payload=payload,
        trace_id="trace-1",
        received_at=datetime(2026, 6, 23, 12, 0, 0),
    )


@pytest.fixture
def control():
    return InboundControl(
        control_id="ctrl-1",
        kind="shutdown",
        session_key=None,
        channel="test",
        trace_id="trace-2",
    )


class TestInboundBus:
    def test_default_maxsize(self):
        bus = InboundBus()
        assert bus.maxsize == 100

    def test_custom_maxsize(self):
        bus = InboundBus(maxsize=10)
        assert bus.maxsize == 10

    def test_publish_and_consume(self, message):
        async def run():
            bus = InboundBus()
            await bus.publish(message)
            item = await bus.consume()
            assert item is message
            assert isinstance(item, InboundMessage)
        asyncio.run(run())

    def test_publish_and_consume_control(self, control):
        async def run():
            bus = InboundBus()
            await bus.publish(control)
            item = await bus.consume()
            assert isinstance(item, InboundControl)
        asyncio.run(run())

    def test_publish_and_consume_mixed(self, message, control):
        async def run():
            bus = InboundBus()
            await bus.publish(message)
            await bus.publish(control)
            item1 = await bus.consume()
            item2 = await bus.consume()
            assert item1 is message
            assert item2 is control
        asyncio.run(run())

    def test_task_done(self, message):
        async def run():
            bus = InboundBus()
            await bus.publish(message)
            item = await bus.consume()
            assert item is message
            bus.task_done()
        asyncio.run(run())

    def test_join(self, message):
        async def run():
            bus = InboundBus()
            await bus.publish(message)
            item = await bus.consume()
            bus.task_done()
            await bus.join()
        asyncio.run(run())

    def test_qsize(self, message):
        async def run():
            bus = InboundBus()
            assert bus.qsize == 0
            await bus.publish(message)
            assert bus.qsize == 1
            await bus.consume()
            assert bus.qsize == 0
        asyncio.run(run())

    def test_queue_respects_maxsize(self):
        async def run():
            bus = InboundBus(maxsize=2)
            msg = InboundMessage(
                message_id="m", external_message_id=None,
                session_key="s", channel="c", target="t",
                payload=MessagePayload(parts=[TextPart(text="x")]),
                trace_id="t", received_at=datetime.now(),
            )
            await bus.publish(msg)
            await bus.publish(msg)
            assert bus.qsize == 2
        asyncio.run(run())

    def test_consume_blocks_when_empty(self):
        async def run():
            bus = InboundBus()

            async def delayed_publish():
                await asyncio.sleep(0.05)
                msg = InboundMessage(
                    message_id="m", external_message_id=None,
                    session_key="s", channel="c", target="t",
                    payload=MessagePayload(parts=[TextPart(text="x")]),
                    trace_id="t", received_at=datetime.now(),
                )
                await bus.publish(msg)

            async def consume_with_timeout():
                try:
                    item = await asyncio.wait_for(bus.consume(), timeout=0.2)
                    return item
                except asyncio.TimeoutError:
                    return None

            task = asyncio.create_task(delayed_publish())
            item = await consume_with_timeout()
            assert item is not None
            await task
        asyncio.run(run())
