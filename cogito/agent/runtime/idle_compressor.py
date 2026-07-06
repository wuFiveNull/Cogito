# cogito/agent/runtime/idle_compressor.py
#
# IdleSessionCompressor — background compression of long-idle sessions
# (Mode 11 from the context-management research).
#
# When a session has been idle for longer than ``idle_threshold_minutes``,
# the compressor loads the most recent messages, summarizes the older ones
# via LLM, and updates the session summary in the database.
#
# This module is designed to be called either:
#   - From a background scheduler / maintenance cron task.
#   - From RuntimeCleanup as a lightweight check after each turn.
#   - Explicitly by the user via a command.

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from cogito.agent.domain.memory import SummaryCandidate
from cogito.agent.domain.state import ConversationMessage, SessionState, SessionSummary
from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.repositories import (
    MessageRepositoryPort,
    SessionRepositoryPort,
    SummaryRepositoryPort,
)
from cogito.agent.ports.summarizer import SummarizerPort

logger = logging.getLogger(__name__)


class IdleSessionCompressor:
    """Compress long-idle sessions by summarizing old messages.

    Args:
        sessions: Repository for reading session state.
        summaries: Repository for reading/updating session summaries.
        messages: Repository for reading conversation messages.
        summarizer: LLM-based summarizer.
        clock: Time source for idle-threshold comparison.
        idle_threshold_minutes: Session idle time before compression triggers.
        keep_recent_messages: Number of most-recent messages to keep intact.
        max_summary_input_chars: Truncation limit for summarizer input.
    """

    def __init__(
        self,
        *,
        sessions: SessionRepositoryPort,
        summaries: SummaryRepositoryPort,
        messages: MessageRepositoryPort,
        summarizer: SummarizerPort,
        clock: ClockPort,
        idle_threshold_minutes: int = 30,
        keep_recent_messages: int = 8,
        max_summary_input_chars: int = 10_000,
    ) -> None:
        self._sessions = sessions
        self._summaries = summaries
        self._messages = messages
        self._summarizer = summarizer
        self._clock = clock
        self._idle_threshold = timedelta(minutes=idle_threshold_minutes)
        self._keep_recent = keep_recent_messages
        self._max_input_chars = max_summary_input_chars

    # ── Public API ────────────────────────────────────────────────────

    async def compress_session(self, session_id: str) -> bool:
        """Check a single session and compress it if idle.

        Returns:
            True if compression was performed, False otherwise.
        """
        try:
            session = await self._sessions.get(session_id)
            if session is None:
                logger.debug("Session %s not found, skipping", session_id)
                return False
        except Exception as exc:
            logger.warning("Failed to read session %s: %s", session_id, exc)
            return False

        if not self._is_idle(session):
            return False

        logger.info("Compressing idle session %s", session_id)
        return await self._do_compress(session)

    async def compress_all_idle(
        self,
        session_ids: list[str],
    ) -> int:
        """Check all given session IDs and compress those that are idle.

        Returns:
            Number of sessions compressed.
        """
        compressed = 0
        for sid in session_ids:
            if await self.compress_session(sid):
                compressed += 1
        return compressed

    # ── Idle check ────────────────────────────────────────────────────

    def _is_idle(self, session: SessionState) -> bool:
        """Check whether a session has been idle long enough to compress."""
        updated = session.updated_at
        if updated is None:
            return False
        idle_time = self._clock.now() - updated
        return idle_time >= self._idle_threshold

    # ── Core compression logic ────────────────────────────────────────

    async def _do_compress(self, session: SessionState) -> bool:
        """Perform compression for one session."""
        session_id = session.session_id

        try:
            # Load existing summary
            existing_summary = await self._summaries.get(session_id)

            # Load recent messages (with extra to account for the keep window)
            load_count = self._keep_recent + 20  # some margin
            all_messages = await self._messages.list_recent(
                session_id=session_id,
                limit=load_count,
            )

            if len(all_messages) <= self._keep_recent:
                logger.debug(
                    "Session %s has %d messages, below %d threshold, skipping",
                    session_id,
                    len(all_messages),
                    self._keep_recent,
                )
                return False

            # Split into "old" (compress) and "recent" (keep)
            old_msgs = all_messages[:-self._keep_recent]
            # recent_msgs = all_messages[-self._keep_recent:]  # kept intact

            # Build text from old messages
            text = self._messages_to_text(old_msgs)
            if len(text) > self._max_input_chars:
                text = text[:self._max_input_chars]

            # Summarize
            new_summary_text = await self._summarizer.summarize(
                text=text,
                existing_summary=existing_summary.content if existing_summary else None,
                max_output_tokens=512,
                timeout_seconds=30.0,
            )

            if not new_summary_text.strip():
                logger.debug("Summarizer returned empty for session %s", session_id)
                return False

            # Update session summary via repository
            candidate = SummaryCandidate(
                candidate_id=f"idle_compress_{session_id}",
                session_id=session_id,
                content=new_summary_text,
                expected_version=(existing_summary.version if existing_summary else 0),
                source_refs=(),
                confidence=1.0,
            )

            await self._summaries.update(
                session_id=session_id,
                candidate=candidate,
            )

            logger.info(
                "Idle compression complete for session %s "
                "(summarized %d messages, %d chars → %d chars)",
                session_id,
                len(old_msgs),
                len(text),
                len(new_summary_text),
            )
            return True

        except Exception as exc:
            logger.error(
                "Idle compression failed for session %s: %s",
                session_id,
                exc,
            )
            return False

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _messages_to_text(messages: list[ConversationMessage]) -> str:
        """Convert ConversationMessages to a text block for summarization."""
        parts: list[str] = []
        for msg in messages:
            role = msg.role or "user"
            content = (msg.content or "").strip()
            if content:
                parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)
