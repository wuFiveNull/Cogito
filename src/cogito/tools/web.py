"""SSRF-resistant read-only Web fetch tool."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from cogito.capability.models import ToolContext, ToolDef
from cogito.infrastructure.safe_http import fetch_url


def create_web_fetch_def() -> ToolDef:
    async def handler(args: dict[str, Any], context: ToolContext) -> str:
        url = str(args["url"])
        method = str(args.get("method", "GET")).upper()
        constraints = context.constraints
        max_bytes = max(
            1_024,
            min(
                int(args.get("max_bytes", 500_000)),
                2_000_000,
                constraints.max_output_chars if constraints else 500_000,
            ),
        )
        timeout = max(
            1.0,
            min(
                float(args.get("timeout_seconds", 20)),
                60.0,
                float(constraints.timeout_seconds if constraints else 20),
            ),
        )
        allowed_hosts = constraints.allowed_hosts if constraints else ()
        response = await asyncio.to_thread(
            fetch_url,
            url,
            method=method,
            allowed_hosts=allowed_hosts,
            max_bytes=max_bytes,
            timeout_s=timeout,
        )
        content_type = response.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        return json.dumps(
            {
                "url": response.url,
                "status": response.status,
                "content_type": content_type,
                "content": response.body.decode(charset, errors="replace"),
                "truncated": response.truncated,
                "trust_label": "external_untrusted",
            },
            ensure_ascii=False,
        )

    return ToolDef(
        "web_fetch",
        "Fetch a public HTTP(S) resource with SSRF protections.",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "HEAD"]},
                "max_bytes": {"type": "integer"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["url"],
        },
        handler,
        toolset=("web",),
        permissions=("network.http",),
        risk_level="medium",
        side_effect_class="none",
        deferred=True,
        result_trust_label="external_untrusted",
        output_schema={
            "type": "object",
            "required": ["url", "status", "content", "trust_label"],
            "properties": {
                "url": {"type": "string"},
                "status": {"type": "integer"},
                "content_type": {"type": "string"},
                "content": {"type": "string"},
                "truncated": {"type": "boolean"},
                "trust_label": {"const": "external_untrusted"},
            },
        },
    )
