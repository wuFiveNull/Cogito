# cogito/agent/tools/result_processor.py
#
# DefaultToolResultProcessor — normalizes, redacts, truncates, and persists results.
#
# Design rules (see tool-system-spec §15):
#   - Results under soft limit → direct inline.
#   - Soft-to-hard limit → structured trim.
#   - Over hard limit → persist to artifact store, return reference.
#   - All secrets redacted before model injection.
#   - Empty results → unified completion placeholder.

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Mapping

from cogito.agent.domain.tools import (
    ArtifactRef,
    ImageContent,
    JsonContent,
    TextContent,
    ToolContent,
    ToolDefinition,
    ToolErrorInfo,
    ToolResult,
    ToolResultStatus,
)
from cogito.agent.ports.tools.artifacts import ToolArtifactStorePort

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResultProcessorConfig:
    inline_soft_limit_chars: int = 12_000
    inline_hard_limit_chars: int = 50_000
    preview_head_chars: int = 6_000
    preview_tail_chars: int = 3_000


class DefaultToolResultProcessor:
    """Processes raw tool outputs into canonical ToolResult objects.

    Pipeline:
      1. Normalize: convert raw dict/string to ToolContent list.
      2. Redact: strip secrets from content (via SecretRedactor).
      3. Truncate/Persist: write large results to artifact store.
      4. Build final ToolResult with llm_content, display_content, artifacts.
    """

    def __init__(
        self,
        *,
        artifact_store: ToolArtifactStorePort | None = None,
        config: ResultProcessorConfig | None = None,
        redactor: object | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._config = config or ResultProcessorConfig()
        self._redactor = redactor

    # ── Public API ──────────────────────────────────────────────────────

    async def process(
        self,
        *,
        definition: ToolDefinition,
        result: Mapping[str, object] | str | None,
        context: Mapping[str, object] | None = None,
    ) -> ToolResult:
        """Process a raw tool output into a canonical ToolResult."""
        call_id = (context or {}).get("call_id", "unknown")
        tool_name = definition.name

        # Step 1: Handle empty/null result
        if result is None or (isinstance(result, str) and not result.strip()):
            return ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                status=ToolResultStatus.SUCCEEDED,
                llm_content=(TextContent(text="(completed with no output)"),),
            )

        # Step 2: Normalize to ToolContent list
        contents = self._normalize(result)

        # Step 3: Redact secrets
        contents = self._redact_contents(contents)

        # Step 4: Compute total chars
        total_chars = sum(self._content_length(c) for c in contents)

        # Step 5: Truncate or persist
        truncated = False
        artifacts: list[ArtifactRef] = []
        llm_contents: list[ToolContent] = list(contents)

        if total_chars > self._config.inline_hard_limit_chars:
            # Persist to artifact store
            artifact_ref = await self._persist_result(result, definition)
            if artifact_ref:
                artifacts.append(artifact_ref)

            # Keep only preview for llm_content
            preview = self._build_preview(result)
            llm_contents = [TextContent(text=preview)]
            truncated = True

        elif total_chars > self._config.inline_soft_limit_chars:
            # Structured trim: keep head + tail + summary
            trimmed = self._trim_result(llm_contents, self._config.inline_hard_limit_chars)
            llm_contents = trimmed
            truncated = True

        return ToolResult(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.SUCCEEDED,
            llm_content=tuple(llm_contents),
            display_content=tuple(contents),
            artifacts=tuple(artifacts),
            truncated=truncated,
            persisted=bool(artifacts),
        )

    @staticmethod
    def build_error_result(
        *,
        call_id: str,
        tool_name: str,
        error_code: str,
        safe_message: str,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> ToolResult:
        """Build a ToolResult for a failed/denied tool call."""
        error = ToolErrorInfo(
            code=error_code,
            safe_message=safe_message,
            retryable=retryable,
            details=details or {},
        )
        content = json.dumps(
            {"error": {"code": error_code, "message": safe_message}},
            ensure_ascii=False,
        )
        return ToolResult(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.FAILED,
            llm_content=(TextContent(text=content),),
            error=error,
        )

    # ── Internal ────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(
        result: Mapping[str, object] | str,
    ) -> list[ToolContent]:
        """Convert a raw result to a list of ToolContent."""
        if isinstance(result, str):
            return [TextContent(text=result)]
        if isinstance(result, dict):
            # Check for common patterns
            if "text" in result:
                return [TextContent(text=str(result["text"]))]
            if "content" in result:
                content = result["content"]
                if isinstance(content, list):
                    return [
                        TextContent(text=str(item)) if isinstance(item, (str, int, float))
                        else JsonContent(value=item)
                        for item in content
                    ]
                return [TextContent(text=str(content))]
            return [JsonContent(value=result)]
        return [TextContent(text=str(result))]

    def _redact_contents(self, contents: list[ToolContent]) -> list[ToolContent]:
        """Redact secrets from tool contents."""
        if self._redactor is None:
            return contents

        redacted: list[ToolContent] = []
        for c in contents:
            if isinstance(c, TextContent):
                text = self._redactor.redact_text(c.text)
                redacted.append(TextContent(text=text))
            elif isinstance(c, JsonContent):
                if isinstance(c.value, dict):
                    redacted.append(JsonContent(value=self._redactor.redact_dict(c.value)))
                elif isinstance(c.value, str):
                    redacted.append(JsonContent(value=self._redactor.redact_text(c.value)))
                else:
                    redacted.append(c)
            else:
                redacted.append(c)
        return redacted

    async def _persist_result(
        self,
        result: Mapping[str, object] | str,
        definition: ToolDefinition,
    ) -> ArtifactRef | None:
        """Persist a large result to the artifact store."""
        if self._artifact_store is None:
            logger.warning(
                "Tool %r result exceeds hard limit but no artifact store configured",
                definition.name,
            )
            return None

        data = json.dumps(result, ensure_ascii=False).encode("utf-8") if isinstance(result, dict) else result.encode("utf-8")
        return await self._artifact_store.store(
            data=data,
            media_type="application/json" if isinstance(result, dict) else "text/plain",
            name=f"tool_result_{definition.name}",
        )

    @staticmethod
    def _build_preview(result: Mapping[str, object] | str) -> str:
        """Build a preview of a large result for inline context."""
        text = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else result
        return text[:6_000] + f"\n... [result persisted, total {len(text)} chars]"

    @staticmethod
    def _trim_result(
        contents: list[ToolContent],
        limit_chars: int,
    ) -> list[ToolContent]:
        """Trim result contents to fit within the character limit."""
        result: list[ToolContent] = []
        total = 0
        for c in contents:
            text = c.text if isinstance(c, TextContent) else json.dumps(c.value) if isinstance(c, JsonContent) else ""
            if total + len(text) > limit_chars:
                remaining = limit_chars - total
                if remaining > 100:
                    trimmed = text[:remaining] + "\n...[trimmed]"
                    result.append(TextContent(text=trimmed))
                break
            result.append(c)
            total += len(text)
        return result

    @staticmethod
    def _content_length(content: ToolContent) -> int:
        if isinstance(content, TextContent):
            return len(content.text)
        elif isinstance(content, JsonContent):
            return len(json.dumps(content.value, ensure_ascii=False))
        return 0
