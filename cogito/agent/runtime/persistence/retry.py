# cogito/agent/runtime/persistence/retry.py
#
# PersistenceRetryPolicy — classifies exceptions as retryable or not,
# and provides retry-loop orchestration.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from cogito.agent.runtime.errors import RuntimeAgentError


class RetryDecision(StrEnum):
    """Decision after classifying an exception."""
    RETRY = "retry"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class PersistenceRetryConfig:
    """Configuration for the persistence retry loop.

    ``max_attempts``: total attempts including the first.
    ``delays_seconds``: delay BEFORE each retry attempt (index 0 = after
    first failure, index 1 = after second failure, etc.).  If the list
    is shorter than the number of retries, the last value is reused.
    """

    max_attempts: int = 3
    delays_seconds: tuple[float, ...] = (0.05, 0.15)


class PersistenceRetryError(RuntimeAgentError):
    """A persistence error that MAY be retryable."""
    code = "PERSISTENCE_RETRY"
    retryable = True

    def __init__(self, message: str, *, code: str = "PERSISTENCE_RETRY") -> None:
        super().__init__(message)
        self.code = code


class PersistenceAbortError(RuntimeAgentError):
    """A persistence error that MUST NOT be retried."""
    code = "PERSISTENCE_ABORT"
    retryable = False


class PersistenceRetryPolicy:
    """Classifies persistence exceptions and runs retry loops.

    Retryable:
      - SQLITE_BUSY / SQLITE_LOCKED (transient write contention)
      - Session version conflict (OptimisticConcurrencyError)
      - Summary version conflict (SummaryConcurrencyError)
      - Commit outcome unknown (PersistenceCommitOutcomeUnknownError)

    Non-retryable:
      - Context validation failures
      - Session ownership conflict
      - Idempotency fingerprint conflict
      - Candidate field validation errors
      - Schema version mismatch
      - Foreign key or CHECK constraint violations
      - CancelledError
    """

    def __init__(self, config: PersistenceRetryConfig | None = None) -> None:
        self._config = config or PersistenceRetryConfig()

    def classify(self, exc: BaseException) -> RetryDecision:
        """Classify an exception as retryable or not."""
        if isinstance(exc, asyncio.CancelledError):
            return RetryDecision.ABORT

        if isinstance(exc, PersistenceAbortError):
            return RetryDecision.ABORT

        if isinstance(exc, PersistenceRetryError):
            return RetryDecision.RETRY

        # Classify by error message patterns (catches SQLite errors
        # that bubble up from aiosqlite)
        msg = str(exc).lower()
        if any(pattern in msg for pattern in (
            "sqlite_busy", "sqlite_locked", "locked",
            "database is locked", "busy",
            "version conflict", "concurrency",
        )):
            return RetryDecision.RETRY

        return RetryDecision.ABORT

    def sleep_before_retry(self, attempt_number: int) -> None:
        """Sleep before a retry attempt (blocking call inside retry loop).

        ``attempt_number`` is 1-based (first call after first failure = 1).
        """
        delays = self._config.delays_seconds
        idx = min(attempt_number - 1, len(delays) - 1)
        delay = delays[max(0, idx)]
        import time
        time.sleep(delay)
