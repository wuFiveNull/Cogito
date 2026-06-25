# cogito/infrastructure/sqlite/repositories/memories.py
#
# SQLite memory repository for PersistencePhase.
#
# Delegates to the existing MemoryRepository for low-level CRUD.
# Provides fine-grained operations needed by the PersistencePhase
# preference/memory policy services.

from __future__ import annotations

from datetime import datetime

from cogito.database.connection import AsyncDatabase
from cogito.database.repository.memories import MemoryRepository as ExistingMemoryRepo
from cogito.database.ids import new_uuid
from cogito.database.utils import json_list, json_obj


class SQLiteMemoryRepository:
    """SQLite-backed memory store for the PersistencePhase UoW."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db
        self._delegate = ExistingMemoryRepo(db)

    async def get_active_by_key(
        self,
        *,
        user_id: str,
        memory_key: str,
    ) -> dict | None:
        """Find a user's active memory by its natural key."""
        return await self._delegate.get_active_by_key(user_id, memory_key)

    async def insert(self, memory: dict) -> dict:
        """Insert a new memory row.

        The ``memory`` dict may include keys: id, user_id, memory_type,
        memory_key, content, value_json, embedding, embedding_dim,
        embedding_model, importance, confidence, status, valid_from,
        valid_until, source_group_id, source_event_ids_json,
        supersedes_id, created_by_span_id, updated_by_span_id.

        Returns the inserted row.
        """
        params = dict(memory)
        if "id" not in params or not params["id"]:
            params["id"] = new_uuid()
        # Ensure default values for required fields
        params.setdefault("memory_type", "fact")
        params.setdefault("status", "active")
        params.setdefault("content", "")
        params.setdefault("value_json", "{}")
        params.setdefault("importance", 0.5)
        params.setdefault("confidence", 0.8)
        params.setdefault("source_event_ids_json", "[]")
        return await self._delegate.insert(params)

    async def update_reinforcement(
        self,
        *,
        memory_id: str,
        confidence: float,
        importance: float,
        source_event_ids: tuple[str, ...],
        updated_by_span_id: str | None,
    ) -> None:
        """Reinforce an existing memory: bump confidence, merge sources."""
        source_ids_json = json_list(list(source_event_ids))
        updates = {
            "confidence": confidence,
            "importance": importance,
            "source_event_ids_json": source_ids_json,
            "updated_by_span_id": updated_by_span_id,
        }
        await self._delegate.update_status(memory_id, updates)

    async def mark_superseded(
        self,
        *,
        memory_id: str,
        valid_until: datetime,
        updated_by_span_id: str | None,
    ) -> None:
        """Mark a memory as superseded (replaced by newer information)."""
        now_str = valid_until.strftime("%Y-%m-%dT%H:%M:%fZ")
        updates = {
            "status": "superseded",
            "valid_until": now_str,
            "updated_by_span_id": updated_by_span_id,
        }
        await self._delegate.update_status(memory_id, updates)

    async def soft_delete(
        self,
        *,
        memory_id: str,
        updated_by_span_id: str | None,
    ) -> None:
        """Soft-delete a memory (set status='deleted')."""
        updates = {
            "status": "deleted",
            "updated_by_span_id": updated_by_span_id,
        }
        await self._delegate.update_status(memory_id, updates)
