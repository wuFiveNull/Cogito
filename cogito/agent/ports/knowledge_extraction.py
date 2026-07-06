# cogito/agent/ports/knowledge_extraction.py
#
# Ports for the knowledge-extraction subdomain.
#
# Design rules (see KnowledgeExtractionPhase-spec §8):
#   - KnowledgeExtractorPort is the abstract interface for the LLM-based
#     structured extractor.  Concrete adapters are in infrastructure/.
#   - RuntimeEventEmitter is a safe, turn-scoped event emitter that
#     never leaks candidate content or user text into events.
#   - Both are Protocols — the runtime phase only depends on these
#     abstractions.

from __future__ import annotations

from typing import Protocol

from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    KnowledgeExtractionResult,
    RawKnowledgeExtraction,
)
from cogito.agent.runtime.context import TurnContext


class KnowledgeExtractorPort(Protocol):
    """Port for structured knowledge extraction (LLM-based).

    Implementations receive a trimmed KnowledgeExtractionInput and
    return a RawKnowledgeExtraction.  The raw result is then parsed,
    validated, normalised and filtered by the local pipeline before
    it becomes a KnowledgeExtractionResult.

    Concrete adapters MUST:
      - Use a closed JSON Schema (additionalProperties: false).
      - Apply maxItems and maxLength constraints on arrays/strings.
      - Never return candidate content that includes API keys or
        authentication secrets.
      - Raise on programming errors (schema mismatch), not on model
        transient failures.
    """

    async def extract(
        self,
        extraction_input: KnowledgeExtractionInput,
    ) -> RawKnowledgeExtraction:
        """Run structured extraction on the prepared input.

        Args:
            extraction_input: Trimmed, safe input built from TurnContext.

        Returns:
            A RawKnowledgeExtraction with any candidates the model found.

        Raises:
            KnowledgeExtractionTimeoutError: On model timeout.
            InvalidExtractionOutputError: On unparseable output.
        """
        ...


class RuntimeEventEmitter(Protocol):
    """Safe event emitter for knowledge-extraction events.

    Event payloads contain only status, counts, duration and error
    codes — never candidate content, user text, or sensitive data.
    """

    async def emit_knowledge_extracted(
        self,
        *,
        ctx: TurnContext,
        result: KnowledgeExtractionResult,
    ) -> None:
        """Emit a ``knowledge_extracted`` lifecycle event.

        The event data MUST NOT include:
          - Any candidate body text
          - User or assistant message text
          - API keys or authentication tokens
          - Exception objects or full stack traces
        """
        ...


# ── Stub implementations for testing ──────────────────────────────────────


class StubKnowledgeExtractor:
    """Returns empty results.  Useful for unit-testing the phase."""

    async def extract(
        self,
        extraction_input: KnowledgeExtractionInput,
    ) -> RawKnowledgeExtraction:
        return RawKnowledgeExtraction()


class StubRuntimeEventEmitter:
    """Records emission counts for testing."""

    def __init__(self) -> None:
        self.calls: list[KnowledgeExtractionResult] = []

    async def emit_knowledge_extracted(
        self,
        *,
        ctx: TurnContext,
        result: KnowledgeExtractionResult,
    ) -> None:
        self.calls.append(result)
