"""Tests for cogito.bus.event_bus — DomainEventBus."""

from __future__ import annotations

import asyncio

import pytest

from cogito.bus.event_bus import DomainEventBus
from cogito.bus.events_lifecycle import LifecycleEvent, TurnStarted


@pytest.fixture
def bus():
    return DomainEventBus()


def _run_async(coro):
    """Run an async coroutine synchronously for testing."""
    return asyncio.run(coro)


class TestDomainEventBus:
    def test_publish_no_handlers(self, bus):
        """Publishing with no subscribers should not error."""
        async def run():
            event = LifecycleEvent(event_type="test_event")
            await bus.publish(event)
        asyncio.run(run())

    def test_subscribe_and_publish(self, bus):
        async def run():
            received = []

            async def handler(event):
                received.append(event)

            bus.on("test_event", handler)
            event = LifecycleEvent(event_type="test_event")
            await bus.publish(event)

            assert len(received) == 1
            assert received[0] is event
        asyncio.run(run())

    def test_multiple_handlers(self, bus):
        async def run():
            received = []

            async def handler1(event):
                received.append("h1")

            async def handler2(event):
                received.append("h2")

            bus.on("test_event", handler1)
            bus.on("test_event", handler2)

            await bus.publish(LifecycleEvent(event_type="test_event"))
            assert received == ["h1", "h2"]
        asyncio.run(run())

    def test_handler_exception_does_not_propagate(self, bus):
        """Handler failures should not break the bus or other handlers."""
        async def run():
            received = []

            async def broken_handler(event):
                raise ValueError("Something went wrong")

            async def good_handler(event):
                received.append("ok")

            bus.on("test_event", broken_handler)
            bus.on("test_event", good_handler)

            await bus.publish(LifecycleEvent(event_type="test_event"))
            assert received == ["ok"]
        asyncio.run(run())

    def test_event_type_filtering(self, bus):
        async def run():
            received = []

            async def handler_a(event):
                received.append("a")

            async def handler_b(event):
                received.append("b")

            bus.on("type_a", handler_a)
            bus.on("type_b", handler_b)

            await bus.publish(LifecycleEvent(event_type="type_a"))
            assert received == ["a"]
        asyncio.run(run())

    def test_subscription_unsubscribe(self, bus):
        async def run():
            received = []

            async def handler(event):
                received.append(event)

            sub = bus.on("test_event", handler)
            await bus.publish(LifecycleEvent(event_type="test_event"))
            assert len(received) == 1

            sub.unsubscribe()
            await bus.publish(LifecycleEvent(event_type="test_event"))
            assert len(received) == 1  # still 1 — handler was removed
        asyncio.run(run())

    def test_double_unsubscribe(self, bus):
        async def run():
            received = []

            async def handler(event):
                received.append(event)

            sub = bus.on("test_event", handler)
            sub.unsubscribe()
            sub.unsubscribe()  # should not raise
        asyncio.run(run())

    def test_enqueue(self, bus):
        """enqueue should schedule but not block."""
        async def run():
            received = []

            async def handler(event):
                received.append(event)

            bus.on("test_event", handler)
            event = LifecycleEvent(event_type="test_event")
            bus.enqueue(event)  # should return immediately

            await asyncio.sleep(0.05)
            assert len(received) == 1
        asyncio.run(run())

    def test_publish_multi(self, bus):
        async def run():
            received = []

            async def handler(event):
                received.append(event.event_type)

            bus.on("type_a", handler)
            bus.on("type_b", handler)

            events = [
                LifecycleEvent(event_type="type_a"),
                LifecycleEvent(event_type="type_b"),
                LifecycleEvent(event_type="type_a"),
            ]
            await bus.publish_multi(events)
            assert received == ["type_a", "type_b", "type_a"]
        asyncio.run(run())

    def test_concrete_event_type(self, bus):
        async def run():
            received = []

            async def handler(event):
                received.append(event)

            bus.on("turn_started", handler)
            event = TurnStarted(trace_id="tr", turn_id="t1")
            await bus.publish(event)

            assert len(received) == 1
            assert received[0].event_type == "turn_started"
            assert received[0].turn_id == "t1"
        asyncio.run(run())

    def test_sync_handler(self, bus):
        """Sync handlers that return None should work fine."""
        async def run():
            received = []

            def handler(event):  # sync
                received.append(event.event_type)

            bus.on("test", handler)
            await bus.publish(LifecycleEvent(event_type="test"))
            assert received == ["test"]
        asyncio.run(run())

    def test_handler_receives_frozen_event(self, bus):
        """Handlers should not be able to modify the event."""
        async def run():
            event = LifecycleEvent(event_type="test", trace_id="original")

            async def handler(ev):
                with pytest.raises(AttributeError):
                    ev.trace_id = "modified"  # frozen

            bus.on("test", handler)
            await bus.publish(event)
        asyncio.run(run())
