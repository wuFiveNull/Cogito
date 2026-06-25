# cogito/agent/tools/builtin/web.py
#
# Built-in tools: web_search, web_fetch — web access with SSRF protection.

from __future__ import annotations

import logging
from typing import Mapping

from cogito.agent.domain.tools import (
    ToolConcurrencyMode,
    ToolDefinition,
    ToolKind,
    ToolLimits,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)
from cogito.infrastructure.sandbox.network_policy import DefaultNetworkPolicy

logger = logging.getLogger(__name__)


class WebSearchHandler:
    """Handler for web_search — searches the web via DuckDuckGo or custom API."""

    def __init__(
        self,
        *,
        network_policy: DefaultNetworkPolicy | None = None,
        search_provider: str = "duckduckgo",
        search_api_key: str | None = None,
    ) -> None:
        self._network_policy = network_policy
        self._search_provider = search_provider
        self._search_api_key = search_api_key

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web_search",
            description="Search the web for information. Returns a list of relevant links and snippets.",
            input_schema={
                "type": "object", "properties": {
                    "query": {"type": "string", "minLength": 1, "description": "Search query"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Number of results"},
                },
                "required": ["query"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE, risk_level=ToolRiskLevel.LOW,
            timeout_seconds=15.0, idempotent=False, parallel_safe=True,
            kind=ToolKind.SEARCH, risk=ToolRisk.EXTERNAL_READ,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        query = str(arguments.get("query", ""))
        count = int(arguments.get("count", 5))

        if self._search_provider == "duckduckgo":
            return await self._search_duckduckgo(query, count)
        elif self._search_provider == "custom":
            return await self._search_custom(query, count)
        else:
            return {"results": [], "query": query,
                    "error": f"Unknown search provider: {self._search_provider}"}

    async def _search_duckduckgo(self, query: str, count: int) -> dict:
        """Search using DuckDuckGo's HTML API (no API key needed)."""
        try:
            import httpx
            from urllib.parse import quote

            url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; Cogito-Agent/1.0)"},
                )
                response.raise_for_status()

            # Parse HTML results
            results = self._parse_duckduckgo_html(response.text, count)
            return {"results": results, "query": query, "total": len(results)}

        except ImportError:
            return {"results": [], "query": query,
                    "error": "httpx is required for web search: pip install httpx"}
        except Exception as exc:
            logger.warning("DuckDuckGo search failed: %s", exc)
            return {"results": [], "query": query, "error": str(exc)}

    @staticmethod
    def _parse_duckduckgo_html(html: str, max_results: int) -> list[dict]:
        """Parse DuckDuckGo HTML search results."""
        results = []
        try:
            import re
            # Extract result blocks: <a rel="nofollow" class="result__a" href="...">
            # The results are in <div class="result results_links results_links_deep">
            result_blocks = re.findall(
                r'<a\s+rel="nofollow"\s+class="result__a"\s+href="([^"]+)".*?>(.*?)</a>',
                html,
                re.DOTALL,
            )
            for href, title_html in result_blocks[:max_results]:
                # Clean title from HTML tags
                title = re.sub(r"<[^>]+>", "", title_html).strip()
                results.append({"title": title, "url": href})

            # If no results found with primary pattern, try fallback
            if not results:
                snippets = re.findall(
                    r'<a\s+class="result__a"\s+href="([^"]+)".*?class="result__snippet">(.*?)</a>',
                    html,
                    re.DOTALL,
                )
                for href, snippet_html in snippets[:max_results]:
                    snippet = re.sub(r"<[^>]+>", "", snippet_html).strip()
                    results.append({"url": href, "snippet": snippet})
        except Exception as exc:
            logger.warning("Failed to parse DuckDuckGo results: %s", exc)

        return results

    async def _search_custom(self, query: str, count: int) -> dict:
        """Search using a custom search API (e.g., Google Programmable Search, Bing)."""
        if not self._search_api_key:
            return {"results": [], "query": query,
                    "error": "Custom search requires a configured API key"}
        return {"results": [], "query": query,
                "note": "Custom search provider not fully configured"}


class WebFetchHandler:
    """Handler for web_fetch — fetches a URL with SSRF protection and HTML→Markdown conversion."""

    def __init__(self, *, network_policy: DefaultNetworkPolicy | None = None) -> None:
        self._network_policy = network_policy

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web_fetch",
            description="Fetch a URL and return its content as formatted text. Automatically converts HTML to Markdown.",
            input_schema={
                "type": "object", "properties": {
                    "url": {"type": "string", "minLength": 1, "description": "URL to fetch (https:// only)"},
                    "max_chars": {"type": "integer", "minimum": 100, "maximum": 50_000, "description": "Max chars to return"},
                },
                "required": ["url"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE, risk_level=ToolRiskLevel.LOW,
            timeout_seconds=30.0, idempotent=False, parallel_safe=True,
            kind=ToolKind.FETCH, risk=ToolRisk.EXTERNAL_READ,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        url = str(arguments.get("url", ""))
        max_chars = int(arguments.get("max_chars", 10_000))

        # SSRF check via network policy
        if self._network_policy is not None:
            result = self._network_policy.check_url(url)
            if not result.allowed:
                return {"error": {"code": "URL_REJECTED", "message": result.reason}}

        # Actual HTTP fetch
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(url, headers={"User-Agent": "Cogito-Agent/1.0"})
                response.raise_for_status()
                raw_text = response.text

                # Detect content type and convert accordingly
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type or "html" in content_type:
                    content = self._html_to_markdown(raw_text)
                else:
                    content = raw_text

                truncated = len(content) > max_chars
                if truncated:
                    content = content[:max_chars] + f"\n[... truncated from {len(content)} chars]"

                return {"content": content, "url": url, "status": response.status_code,
                        "truncated": truncated, "content_type": content_type}

        except httpx.HTTPError as exc:
            return {"error": {"code": "HTTP_ERROR", "message": str(exc)}}
        except Exception as exc:
            return {"error": {"code": "FETCH_ERROR", "message": str(exc)}}

    @staticmethod
    def _html_to_markdown(html: str) -> str:
        """Convert HTML to Markdown for better LLM readability.

        Uses html2text if available; falls back to basic HTML tag stripping.
        """
        try:
            import html2text
            converter = html2text.HTML2Text()
            converter.body_width = 0       # No line wrapping
            converter.ignore_links = False
            converter.ignore_images = True
            converter.ignore_emphasis = False
            converter.protect_links = True
            converter.unicode_snob = True
            return converter.handle(html)
        except ImportError:
            pass

        # Fallback: basic HTML tag stripping + entity decoding
        try:
            import re
            from html import unescape
            # Remove script and style blocks
            text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            # Strip tags
            text = re.sub(r"<[^>]+>", " ", text)
            # Decode entities
            text = unescape(text)
            # Collapse whitespace
            text = re.sub(r"\s+", " ", text).strip()
            return text
        except Exception:
            return html
