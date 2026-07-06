# cogito/infrastructure/sqlite/repositories/events.py
#
# SQLite event repository for PersistencePhase.
#
# Delegates to the existing EventRepository for low-level CRUD.
# The ``add_many`` method inserts multiple events within the UoW
# transaction.

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from cogito.database.connection import AsyncDatabase
from cogito.database.repository.events import EventRepository
from cogito.agent.runtime.persistence.models import PersistedEvent


def _event_draft_to_params(draft: Mapping[str, Any], seq_no: int, now: datetime) -> dict:
    """Convert an EventDraft-like object to a flat parameter dict for EventRepository."""
    content_json = draft.get("content_json", {})
    if isinstance(content_json, Mapping) and not isinstance(content_json, str):
        import json
        content_json_str = json.dumps(content_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        content_json_str = str(content_json)

    return {
        "id": draft["event_id"],
        "user_id": draft["user_id"],
        "session_id": draft["session_id"],
        "seq_no": seq_no,
        "role": draft["role"],
        "event_type": draft["event_type"],
        "content": draft["content"],
        "content_json": content_json_str,
        "request_id": draft.get("request_id"),
        "turn_id": draft.get("turn_id"),
        "extraction_status": draft.get("extraction_status", "pending"),
        "trace_id": draft.get("trace_id"),
        "created_by_span_id": draft.get("created_by_span_id"),
    }


class SQLiteEventRepository:
    """SQLite-backed event store for the PersistencePhase UoW."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db
        self._delegate = EventRepository(db)

    async def add_many(
        self,
        events: tuple[PersistedEvent, ...],
    ) -> None:
        """Insert multiple events within the current transaction.

        Each event must already have its ``seq_no`` assigned by the
        caller (PersistencePhase._persist_once).  We delegate to
        EventRepository.insert per event.
        """
        for event in events:
            import json
            content_json_str = (
                json.dumps(event.content_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if isinstance(event.content_json, dict)
                else str(event.content_json)
            )
            params = {
                "id": event.event_id,
                "user_id": event.user_id,
                "session_id": event.session_id,
                "seq_no": event.seq_no,
                "role": event.role,
                "event_type": event.event_type,
                "content": event.content,
                "content_json": content_json_str,
                "request_id": event.request_id,
                "turn_id": event.turn_id,
                "extraction_status": event.extraction_status,
            }
            await self._delegate.insert(params)

    async def get_by_id(self, event_id: str) -> PersistedEvent | None:
        """Lookup an event by its primary key."""
        row = await self._delegate.get_by_id(event_id)
        if row is None:
            return None
        return PersistedEvent(
            event_id=row["id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            seq_no=row["seq_no"],
            role=row["role"],
            event_type=row["event_type"],
            content=str(row.get("content", "")),
            content_json=row.get("content_json", {}),
            request_id=row.get("request_id"),
            turn_id=row.get("turn_id"),
            extraction_status=row.get("extraction_status", "pending"),
            created_at=datetime.fromisoformat(row["created_at"]) if row.get("created_at") else datetime.now(),
        )
