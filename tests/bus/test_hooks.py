"""Tests for cogito.bus.hooks — HookPipeline."""

from __future__ import annotations

import asyncio

import pytest

from cogito.bus.hooks import (
    HookExecutionError,
    HookPipeline,
    HookRejected,
    shortcircuit,
    is_shortcircuit,
)


class TestShortcircuit:
    def test_shortcircuit_returns_sentinel(self):
        result = shortcircuit(42)
        assert is_shortcircuit(result) is True

    def test_not_shortcircuit(self):
        assert is_shortcircuit(None) is False
        assert is_shortcircuit(42) is False
        assert is_shortcircuit(("not", "shortcircuit")) is False

    def test_shortcircuit_none(self):
        result = shortcircuit()
        assert is_shortcircuit(result) is True


class TestHookPipeline:
    def test_empty_stage_returns_none(self):
        async def run():
            pipeline = HookPipeline()
            result = await pipeline.run("before_turn", {})
            assert result is None
        asyncio.run(run())

    def test_sync_handler(self):
        async def run():
            pipeline = HookPipeline()

            def handler(ctx):
                return {**ctx, "modified": True}

            pipeline.register("before_turn", handler)
            result = await pipeline.run("before_turn", {"original": True})
            assert result == {"original": True, "modified": True}
        asyncio.run(run())

    def test_async_handler(self):
        async def run():
            pipeline = HookPipeline()

            async def handler(ctx):
                return {**ctx, "async": True}

            pipeline.register("before_turn", handler)
            result = await pipeline.run("before_turn", {"original": True})
            assert result == {"original": True, "async": True}
        asyncio.run(run())

    def test_priority_order(self):
        async def run():
            pipeline = HookPipeline()
            order = []

            def handler_a(ctx):
                order.append("a")
                return ctx

            def handler_b(ctx):
                order.append("b")
                return ctx

            def handler_c(ctx):
                order.append("c")
                return ctx

            pipeline.register("stage", handler_a, priority=100)
            pipeline.register("stage", handler_b, priority=50)
            pipeline.register("stage", handler_c, priority=75)

            await pipeline.run("stage", {})
            assert order == ["b", "c", "a"]
        asyncio.run(run())

    def test_handler_passes_result_to_next(self):
        async def run():
            pipeline = HookPipeline()

            def add_one(ctx):
                return ctx + 1

            def add_two(ctx):
                return ctx + 2

            pipeline.register("calc", add_one, priority=100)
            pipeline.register("calc", add_two, priority=200)

            result = await pipeline.run("calc", 0)
            assert result == 3  # 0 + 1 + 2
        asyncio.run(run())

    def test_shortcircuit_stops_execution(self):
        async def run():
            pipeline = HookPipeline()
            order = []

            def first(ctx):
                order.append("first")
                return shortcircuit("stopped")

            def second(ctx):
                order.append("second")
                return ctx

            pipeline.register("stage", first, priority=100)
            pipeline.register("stage", second, priority=200)

            result = await pipeline.run("stage", "start")
            assert result == "stopped"
            assert order == ["first"]
        asyncio.run(run())

    def test_hook_rejected(self):
        async def run():
            pipeline = HookPipeline()

            def rejecting(ctx):
                raise HookRejected("Permission denied")

            pipeline.register("auth", rejecting)

            with pytest.raises(HookRejected):
                await pipeline.run("auth", {})
        asyncio.run(run())

    def test_handler_exception_wrapped(self):
        async def run():
            pipeline = HookPipeline()

            def broken(ctx):
                raise ValueError("something went wrong")

            pipeline.register("stage", broken)

            with pytest.raises(HookExecutionError):
                await pipeline.run("stage", {})
        asyncio.run(run())

    def test_register_and_unregister(self):
        pipeline = HookPipeline()

        def handler(ctx):
            return ctx

        pipeline.register("stage", handler)
        assert pipeline.handlers_count("stage") == 1

        result = pipeline.unregister("stage", handler)
        assert result is True
        assert pipeline.handlers_count("stage") == 0

    def test_unregister_nonexistent(self):
        pipeline = HookPipeline()

        def handler(ctx):
            return ctx

        result = pipeline.unregister("nonexistent", handler)
        assert result is False

    def test_unregister_nonexistent_handler(self):
        pipeline = HookPipeline()

        def handler_a(ctx):
            return ctx

        def handler_b(ctx):
            return ctx

        pipeline.register("stage", handler_a)
        result = pipeline.unregister("stage", handler_b)
        assert result is False
        assert pipeline.handlers_count("stage") == 1

    def test_clear_stage(self):
        pipeline = HookPipeline()
        pipeline.register("a", lambda x: x)
        pipeline.register("b", lambda x: x)
        assert pipeline.handlers_count("a") == 1
        pipeline.clear_stage("a")
        assert pipeline.handlers_count("a") == 0
        assert pipeline.handlers_count("b") == 1

    def test_clear_all(self):
        pipeline = HookPipeline()
        pipeline.register("a", lambda x: x)
        pipeline.register("b", lambda x: x)
        pipeline.clear_all()
        assert pipeline.handlers_count("a") == 0
        assert pipeline.handlers_count("b") == 0
        assert pipeline.list_stages() == ()

    def test_list_stages(self):
        pipeline = HookPipeline()
        pipeline.register("z", lambda x: x)
        pipeline.register("a", lambda x: x)
        pipeline.register("m", lambda x: x)
        stages = pipeline.list_stages()
        assert stages == ("a", "m", "z")

    def test_handlers_count(self):
        pipeline = HookPipeline()
        assert pipeline.handlers_count("nonexistent") == 0
        pipeline.register("stage", lambda x: x)
        assert pipeline.handlers_count("stage") == 1
        pipeline.register("stage", lambda x: x)
        assert pipeline.handlers_count("stage") == 2

    def test_mixed_sync_async(self):
        async def run():
            pipeline = HookPipeline()

            def sync_handler(ctx):
                return {**ctx, "sync": True}

            async def async_handler(ctx):
                return {**ctx, "async": True}

            pipeline.register("stage", sync_handler, priority=100)
            pipeline.register("stage", async_handler, priority=200)

            result = await pipeline.run("stage", {})
            assert result == {"sync": True, "async": True}
        asyncio.run(run())

    def test_run_unknown_stage(self):
        async def run():
            pipeline = HookPipeline()
            result = await pipeline.run("unknown", {"key": "value"})
            assert result is None
        asyncio.run(run())
