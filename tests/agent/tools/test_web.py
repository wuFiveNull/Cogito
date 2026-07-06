"""Tests for web tools — SSRF protection and HTTP fetching."""

from __future__ import annotations

import pytest

from cogito.agent.tools.builtin.web import WebFetchHandler, WebSearchHandler
from cogito.infrastructure.sandbox.network_policy import DefaultNetworkPolicy


class TestWebFetchHandler:
    def test_ssrf_localhost_denied(self) -> None:
        policy = DefaultNetworkPolicy()
        handler = WebFetchHandler(network_policy=policy)

        async def _test():
            result = await handler.execute(
                arguments={"url": "https://127.0.0.1/admin"},
                context={},
            )
            assert "error" in result
            assert "REJECTED" in result["error"]["code"]

        import asyncio
        asyncio.run(_test())

    def test_https_url_accepted(self) -> None:
        policy = DefaultNetworkPolicy()
        handler = WebFetchHandler(network_policy=policy)

        async def _test():
            result = await handler.execute(
                arguments={"url": "https://example.com"},
                context={},
            )
            # May fail due to network, but should not be SSRF rejection
            if "error" in result:
                assert "REJECTED" not in result["error"]["code"]

        import asyncio
        asyncio.run(_test())
