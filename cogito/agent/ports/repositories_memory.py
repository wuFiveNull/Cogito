# cogito/agent/ports/repositories_memory.py
#
# Memory repository port for PersistencePhase.
#
# This port provides the fine-grained memory operations needed by the
# PersistencePhase transaction pipeline (get/insert/reinforce/supersede/
# soft-delete).  It is separate from the legacy MemoryRepositoryPort
# used by KnowledgeExtractionPhase (which used a bulk save_candidates API).

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class MemoryRepositoryPort(Protocol):
    """Fine-grained memory operations for PersistencePhase."""

    async def get_active_by_key(
        self,
        *,
        user_id: str,
        memory_key: str,
    ) -> dict | None:
        ...

    async def insert(self, memory: dict) -> dict:
        ...

    async def update_reinforcement(
        self,
        *,
        memory_id: str,
        confidence: float,
        importance: float,
        source_event_ids: tuple[str, ...],
        updated_by_span_id: str | None,
    ) -> None:
        ...

    async def mark_superseded(
        self,
        *,
        memory_id: str,
        valid_until: datetime,
        updated_by_span_id: str | None,
    ) -> None:
        ...

    async def soft_delete(
        self,
        *,
        memory_id: str,
        updated_by_span_id: str | None,
    ) -> None:
        ...
