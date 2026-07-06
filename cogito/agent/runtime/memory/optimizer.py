# cogito/agent/runtime/memory/optimizer.py
#
# OptimizerService — background periodic task that merges PENDING.md
# into MEMORY.md, controlling the update frequency to preserve prompt
# cache stability.
#
# Rationale (see memory-markdown.md §PENDING → MEMORY):
#   MEMORY.md is injected into the system prompt every turn.  If every
#   consolidation rewrote MEMORY.md, the prompt cache would miss on
#   every write.  Instead, consolidation writes to PENDING.md (not
#   injected into prompts), and the Optimizer periodically batch-merges
#   PENDING into MEMORY.
#
# Default interval: 18 hours (64800 seconds), matching Akashic.

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import aclosing

from cogito.agent.domain.messages import SystemMessage, UserMessage
from cogito.agent.domain.model import (
    ModelCompleted,
    ModelFinishReason,
    ModelInvocationRequest,
    ModelStreamEvent,
    ModelTextDelta,
)
from cogito.agent.ports.model import ModelPort
from cogito.agent.runtime.memory.files import MemoryFileManager

logger = logging.getLogger(__name__)

OPTIMIZER_SYSTEM_PROMPT = """You are a memory optimizer. Your job is to merge new pending facts into an existing memory file.

Given:
1. The current MEMORY.md content (existing long-term memory)
2. The PENDING.md content (new facts to integrate)

Produce an updated MEMORY.md that:
1. Adds new facts under the correct sections (identity, preference, key_info, etc.)
2. Updates or replaces existing entries when a correction is found
3. Removes duplicate entries
4. Keeps the same markdown format

Rules:
- Keep all existing entries unless replaced by a correction.
- Write in Chinese.
- Output ONLY the updated MEMORY.md content, no additional text.
"""

OPTIMIZER_PROMPT_TEMPLATE = """Merge the pending items into the existing memory:

=== Existing MEMORY.md ===
{memory_content}

=== PENDING.md (new items to merge) ===
{pending_content}

Output the updated MEMORY.md content only (no JSON, no explanation)."""


class OptimizerService:
    """Background periodic optimizer that merges PENDING.md into MEMORY.md.

    Args:
        model: ModelPort for the merge LLM call.
        files: MemoryFileManager for disk I/O.
        interval_seconds: How often to run the optimizer (default 18h).
        timeout_seconds: Timeout per LLM call.
    """

    def __init__(
        self,
        model: ModelPort,
        files: MemoryFileManager,
        *,
        interval_seconds: int = 64800,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._model = model
        self._files = files
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._task: asyncio.Task[None] | None = None

    # ── Background lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        """Start the background optimizer loop (fire-and-forget)."""
        if self._task is not None and not self._task.done():
            logger.debug("Optimizer already running")
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Optimizer started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        """Stop the background loop."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("Optimizer stopped")

    # ── Single optimization cycle ─────────────────────────────────────

    async def optimize_once(self) -> bool:
        """Run one optimization cycle: PENDING.md → MEMORY.md merge.

        Returns:
            True if MEMORY.md was updated, False otherwise.
        """
        pending = self._files.read("PENDING.md").strip()
        memory = self._files.read("MEMORY.md").strip()

        if not pending:
            logger.debug("Optimizer: nothing pending, skipping")
            return False

        # Call LLM to merge
        prompt = OPTIMIZER_PROMPT_TEMPLATE.format(
            memory_content=memory or "(空)",
            pending_content=pending,
        )

        request = ModelInvocationRequest(
            turn_id="optimizer",
            request_id="optimizer",
            round_index=0,
            messages=(
                SystemMessage(content=OPTIMIZER_SYSTEM_PROMPT),
                UserMessage(content=prompt),
            ),
            tools=(),
            timeout_seconds=self._timeout,
            max_output_tokens=4096,
        )

        updated = await self._call_model(request)
        if not updated or not updated.strip():
            logger.warning("Optimizer: LLM returned empty, skipping")
            return False

        # Write updated MEMORY.md
        self._files.write("MEMORY.md", updated.strip())

        # Clear PENDING.md (snapshot for rollback safety — simple overwrite)
        self._files.write("PENDING.md", "")

        logger.info("Optimizer: merged pending into MEMORY.md (%d chars)", len(updated))
        return True

    # ── Internal loop ─────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Run optimization periodically."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self.optimize_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Optimizer cycle failed: %s", exc)

    async def _call_model(self, request: ModelInvocationRequest) -> str | None:
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
            logger.warning("Optimizer LLM call timed out")
            return None
        except Exception as exc:
            logger.warning("Optimizer LLM call failed: %s", exc)
            return None
        return "".join(collected) if collected else None
