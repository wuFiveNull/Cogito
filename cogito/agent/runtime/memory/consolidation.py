# cogito/agent/runtime/memory/consolidation.py
#
# ConsolidationService — extracts structured memory from conversation turns.
#
# Triggered after enough new messages accumulate (determined by the
# minimum-new-messages threshold).  Calls the LLM to:
#   1. Extract timeline events (history_entries) → appends to HISTORY.md
#   2. Extract pending facts (pending_items) → appends to PENDING.md
#   3. Generate recent context summary → writes RECENT_CONTEXT.md
#
# All writes are idempotent: source_ref (message IDs) prevents duplicates.

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from dataclasses import dataclass

from cogito.agent.domain.messages import (
    ModelMessage,
    SystemMessage,
    UserMessage,
)
from cogito.agent.domain.model import (
    ModelCompleted,
    ModelFinishReason,
    ModelInvocationRequest,
    ModelStreamEvent,
    ModelTextDelta,
)
from cogito.agent.domain.state import ConversationMessage
from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.model import ModelPort
from cogito.agent.runtime.memory.files import MemoryFileManager

logger = logging.getLogger(__name__)

# Minimum new messages to trigger consolidation
DEFAULT_MIN_NEW_MESSAGES = 5

# Number of most-recent messages to keep intact (not consolidated)
DEFAULT_KEEP_RECENT = 8

# System prompt for the consolidation LLM call
CONSOLIDATION_SYSTEM_PROMPT = """You are a memory extraction system. Your job is to read conversation messages and extract structured memories.

Extract three things from the conversation:

## 1. Timeline Events (history_entries)
Important events or decisions that happened in this conversation.
Each entry: {summary: "1-2 sentence summary", emotional_weight: 0-10}

## 2. Stable Facts (pending_items)
Long-term facts about the user that should be remembered.
Each item: {tag: "identity|preference|key_info|health_long_term|requested_memory|correction", content: "fact description"}

## 3. Recent Context Summary
A brief summary of what was discussed, what the user is interested in, what needs follow-up.

Rules:
- Only extract facts that are explicitly stated or clearly implied.
- Do NOT make up or infer facts without evidence.
- Extract USER facts only, not assistant suggestions.
- Emotional weight: 0 = neutral, 10 = extremely emotional.
- Write summaries in Chinese.
- Output ONLY valid JSON with no additional text."""

EXTRACT_PROMPT_TEMPLATE = """Extract memories from the following conversation messages:

{messages}

Respond with a JSON object exactly in this format:
{{
  "history_entries": [
    {{"summary": "...", "emotional_weight": 0}}
  ],
  "pending_items": [
    {{"tag": "identity|preference|key_info|requested_memory", "content": "..."}}
  ],
  "recent_context_summary": "2-3 sentence summary of what happened"
}}

If there is nothing to extract, return empty arrays and a minimal summary."""

COMPRESS_PROMPT_TEMPLATE = """Below is the current recent context and some new conversation turns. Merge the new content into the existing context:

=== Existing Context ===
{existing_context}

=== New Conversation ===
{new_messages}

=== Existing Recent Context ===
{existing_recent_context}

Produce an updated "Recent Context" section with:
## Compression
- Key topics discussed
- User's interests/preferences shown
- Things to follow up on

## Ongoing Threads
- Topics that are not resolved yet

Write in Chinese. Output the updated Recent Context markdown directly (no JSON wrapper).
"""


@dataclass
class ConsolidationResult:
    """What the consolidation produced."""

    history_entries: list[dict]
    pending_items: list[dict]
    recent_context_summary: str
    message_ids_consolidated: list[str]

    @property
    def has_content(self) -> bool:
        return bool(self.history_entries or self.pending_items)


class ConsolidationService:
    """Extract memories from conversation and write to memory files.

    Args:
        model: ModelPort for LLM extraction calls.
        files: MemoryFileManager for disk I/O.
        clock: Time source.
        min_new_messages: Minimum unconsolidated messages to trigger.
        keep_recent: Number of most-recent messages to exclude from consolidation.
        extraction_timeout_seconds: Timeout per LLM call.
    """

    def __init__(
        self,
        model: ModelPort,
        files: MemoryFileManager,
        clock: ClockPort,
        *,
        min_new_messages: int = DEFAULT_MIN_NEW_MESSAGES,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        extraction_timeout_seconds: float = 60.0,
    ) -> None:
        self._model = model
        self._files = files
        self._clock = clock
        self._min_new = min_new_messages
        self._keep_recent = keep_recent
        self._timeout = extraction_timeout_seconds

    # ── Public API ────────────────────────────────────────────────────

    async def should_consolidate(
        self,
        session_id: str,
        all_messages: list[ConversationMessage],
    ) -> bool:
        """Check if enough new messages have accumulated."""
        last_seq = self._files.get_consolidation_state(session_id)
        new_count = sum(1 for m in all_messages if m.sequence > last_seq)
        threshold = max(self._min_new, self._keep_recent // 2)
        return new_count >= threshold

    async def consolidate(
        self,
        session_id: str,
        messages: list[ConversationMessage],
    ) -> ConsolidationResult:
        """Run full consolidation: extract, write, update state."""
        last_seq = self._files.get_consolidation_state(session_id)

        # Filter to unconsolidated messages
        new_msgs = [m for m in messages if m.sequence > last_seq]
        if not new_msgs:
            return ConsolidationResult([], [], "", [])

        # Keep the most recent ones for "recent turns" — use older ones for extraction
        if len(new_msgs) > self._keep_recent:
            extract_msgs = new_msgs[:-self._keep_recent]
        else:
            extract_msgs = new_msgs

        # Build text from messages
        text = self._messages_to_text(new_msgs)

        # LLM extraction
        result = await self._extract(text)

        if not result.has_content and not result.recent_context_summary:
            # Still update the consolidation state to mark them as processed
            max_seq = max(m.sequence for m in new_msgs)
            self._files.set_consolidation_state(session_id, max_seq)
            return result

        # Get message IDs for source_ref
        msg_ids = [m.message_id for m in new_msgs]
        result.message_ids_consolidated = msg_ids

        # ── Write to files ────────────────────────────────────────────
        await self._write_results(session_id, result, new_msgs)

        # Update consolidation state
        max_seq = max(m.sequence for m in new_msgs)
        self._files.set_consolidation_state(session_id, max_seq)

        return result

    # ── LLM extraction ────────────────────────────────────────────────

    async def _extract(self, text: str) -> ConsolidationResult:
        """Call LLM to extract structured memories from text."""
        prompt = EXTRACT_PROMPT_TEMPLATE.format(messages=text)

        request = ModelInvocationRequest(
            turn_id="consolidation",
            request_id="consolidation",
            round_index=0,
            messages=(
                SystemMessage(content=CONSOLIDATION_SYSTEM_PROMPT),
                UserMessage(content=prompt),
            ),
            tools=(),
            timeout_seconds=self._timeout,
            max_output_tokens=2048,
        )

        raw = await self._call_model(request)
        if not raw:
            return ConsolidationResult([], [], "", [])

        return self._parse_extraction(raw)

    def _parse_extraction(self, raw: str) -> ConsolidationResult:
        """Parse LLM JSON output into structured result."""
        # Try to extract JSON from the response (it might have markdown fences)
        import re

        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            logger.warning("No JSON found in extraction response")
            return ConsolidationResult([], [], "", [])

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse extraction JSON: %s", exc)
            return ConsolidationResult([], [], "", [])

        history = data.get("history_entries", [])
        pending = data.get("pending_items", [])
        summary = data.get("recent_context_summary", "")

        return ConsolidationResult(
            history_entries=history,
            pending_items=pending,
            recent_context_summary=summary,
            message_ids_consolidated=[],
        )

    # ── File writing ──────────────────────────────────────────────────

    async def _write_results(
        self,
        session_id: str,
        result: ConsolidationResult,
        messages: list[ConversationMessage],
    ) -> None:
        """Write extraction results to memory files."""
        now = self._clock.now()

        # HISTORY.md — append timeline events
        history_lines: list[str] = []
        for entry in result.history_entries:
            ts = now.strftime("%Y-%m-%d %H:%M")
            summary = entry.get("summary", "")
            weight = entry.get("emotional_weight", 0)
            marker = self._make_marker(result.message_ids_consolidated, "history_entry")
            history_lines.append(f"{marker}\n[{ts}] {summary} (weight={weight})")

        if history_lines:
            self._files.append("HISTORY.md", "\n".join(history_lines) + "\n")

        # PENDING.md — append pending items
        pending_lines: list[str] = []
        for item in result.pending_items:
            tag = item.get("tag", "identity")
            content = item.get("content", "")
            marker = self._make_marker(result.message_ids_consolidated, "pending_item")
            pending_lines.append(f"{marker}\n- [{tag}] {content}")

        if pending_lines:
            self._files.append("PENDING.md", "\n".join(pending_lines) + "\n")

        # RECENT_CONTEXT.md — generate compressed summary
        recent_text = self._messages_to_text(messages[-self._keep_recent:])
        existing_recent = self._files.read("RECENT_CONTEXT.md")

        if result.recent_context_summary or recent_text:
            await self._update_recent_context(
                existing_recent,
                result.recent_context_summary,
                recent_text,
            )

    async def _update_recent_context(
        self,
        existing: str,
        compression: str,
        recent_turns: str,
    ) -> None:
        """Write updated RECENT_CONTEXT.md."""
        parts: list[str] = ["# 近期上下文\n"]

        if compression:
            parts.append("## Compression")
            parts.append(compression)
            parts.append("")

        parts.append("## Recent Turns")
        # Only keep the most recent turns (not full history)
        lines = recent_turns.strip().split("\n")
        MAX_RECENT_LINES = 20
        if len(lines) > MAX_RECENT_LINES:
            lines = lines[-MAX_RECENT_LINES:]
        parts.append("\n".join(lines))

        self._files.write("RECENT_CONTEXT.md", "\n".join(parts))

    # ── Helpers ───────────────────────────────────────────────────────

    async def _call_model(self, request: ModelInvocationRequest) -> str | None:
        """Stream the model and collect text output."""
        collected: list[str] = []
        try:
            async with asyncio.timeout(self._timeout):
                async with aclosing(self._model.stream(request)) as stream:
                    async for event in stream:
                        if isinstance(event, ModelTextDelta):
                            collected.append(event.text)
                        elif isinstance(event, ModelCompleted):
                            if event.finish_reason in (
                                ModelFinishReason.STOP,
                                ModelFinishReason.LENGTH,
                            ):
                                break
                            return None
        except asyncio.TimeoutError:
            logger.warning("Consolidation LLM call timed out")
            return None
        except Exception as exc:
            logger.warning("Consolidation LLM call failed: %s", exc)
            return None

        return "".join(collected) if collected else None

    @staticmethod
    def _messages_to_text(messages: list[ConversationMessage]) -> str:
        parts: list[str] = []
        for m in messages:
            role = m.role or "user"
            content = (m.content or "").strip()
            if content:
                parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)

    @staticmethod
    def _make_marker(msg_ids: list[str], kind: str) -> str:
        """Create an HTML comment marker for idempotent appends."""
        ids_json = json.dumps(msg_ids, ensure_ascii=False)
        return f"<!-- consolidation:{ids_json}:{kind} -->"
