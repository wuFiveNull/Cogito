"""Tests for ModelRouter (PR 9-B)."""

from __future__ import annotations

import pytest

from cogito.model.contracts import (
    ErrorCategory,
    ErrorEnvelope,
    ModelRequest,
    FinishReason,
)
from cogito.model.router import ModelRouter, RouterError
from cogito.model.stub_provider import StubModelProvider, StubScenario


class TestModelRouter:
    @pytest.mark.asyncio
    async def test_route_by_role(self):
        provider = StubModelProvider()
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )

        response = await router.generate(ModelRequest(), model_role="main")
        assert response.finish_reason == FinishReason.stop

    @pytest.mark.asyncio
    async def test_unknown_role_raises(self):
        router = ModelRouter(providers={}, role_map={})

        with pytest.raises(RouterError, match="No provider configured"):
            await router.generate(ModelRequest(), model_role="nonexistent")

    @pytest.mark.asyncio
    async def test_fallback_when_primary_unhealthy(self):
        class UnhealthyProvider(StubModelProvider):
            async def health(self):
                from cogito.model.provider import HealthStatus
                return HealthStatus(healthy=False, message="Down")

        primary = UnhealthyProvider()
        fallback = StubModelProvider([StubScenario(response_text="Fallback OK")])

        router = ModelRouter(
            providers={"primary": primary, "fallback": fallback},
            role_map={"main": "primary"},
            fallbacks={"primary": ["fallback"]},
        )

        response = await router.generate(ModelRequest(), model_role="main")
        assert "Fallback" in response.text

    @pytest.mark.asyncio
    async def test_all_providers_exhausted(self):
        class AlwaysDown(StubModelProvider):
            async def health(self):
                from cogito.model.provider import HealthStatus
                return HealthStatus(healthy=False, message="Down")

        router = ModelRouter(
            providers={"p1": AlwaysDown()},
            role_map={"main": "p1"},
            max_retries=0,
        )

        with pytest.raises(RouterError, match="exhausted"):
            await router.generate(ModelRequest(), model_role="main")

    @pytest.mark.asyncio
    async def test_context_overflow_not_retried(self):
        provider = StubModelProvider([
            StubScenario(error=ErrorEnvelope(
                category=ErrorCategory.context_overflow,
                message="Context too long",
                retryable=False,
            )),
        ])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )

        with pytest.raises(RouterError, match="Context overflow"):
            await router.generate(ModelRequest(), model_role="main")

    @pytest.mark.asyncio
    async def test_non_retryable_error_not_fallback(self):
        provider = StubModelProvider([
            StubScenario(error=ErrorEnvelope(
                category=ErrorCategory.authentication,
                message="Auth failed",
                retryable=False,
            )),
        ])
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
            fallbacks={"stub": ["fallback"]},
        )

        with pytest.raises(RouterError, match="Non-retryable"):
            await router.generate(ModelRequest(), model_role="main")

    @pytest.mark.asyncio
    async def test_get_provider(self):
        provider = StubModelProvider()
        router = ModelRouter(
            providers={"stub": provider},
            role_map={"main": "stub"},
        )

        p = router.get_provider("main")
        assert p is provider

    def test_get_provider_unknown_role(self):
        router = ModelRouter(providers={}, role_map={})
        with pytest.raises(RouterError):
            router.get_provider("unknown")

    def test_get_provider_not_found(self):
        router = ModelRouter(
            providers={},
            role_map={"main": "nonexistent"},
        )
        with pytest.raises(RouterError):
            router.get_provider("main")
