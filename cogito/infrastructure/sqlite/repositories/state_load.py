# cogito/infrastructure/sqlite/repositories/state_load.py
#
# Read-only SQLite adapters for StateLoadPhase.
#
# Each adapter implements one of the 6 repository ports defined in
# ``cogito.agent.ports.repositories``, reading from the corresponding
# database table(s).  All adapters are injected via ``AsyncDatabase``
# and are designed for single-connection / low-concurrency personal-agent
# use.
#
# Design rules (see state-load-phase-implementation-guide §6):
#   - Each adapter is a narrow, single-purpose class (not a fat aggregate).
#   - Row → DTO conversion happens inside the adapter.
#   - Storage failures propagate as raw exceptions (no swallowing).
#   - Missing rows return None (for get) or [] (for list), as the port
#     contract requires the caller (StateLoadPhase) to distinguish
#     between "no data" and "storage failed".

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionLifecycle,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.database.connection import AsyncDatabase


# ═══════════════════════════════════════════════════════════════════════
# Session
# ═══════════════════════════════════════════════════════════════════════


class SQLiteSessionReadAdapter:
    """Read-only adapter for ``SessionRepositoryPort.get()``.

    Reads from the ``sessions`` control table.  The ``user_id`` column
    in the database maps to ``actor_id`` in the domain model.
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, session_id: str) -> SessionState | None:
        row = await self._db.fetchone(
            "SELECT * FROM sessions WHERE session_id = :sid",
            {"sid": session_id},
        )
        if row is None:
            return None
        return SessionState(
            session_id=row["session_id"],
            actor_id=row["user_id"],
            lifecycle=SessionLifecycle.ACTIVE,
            created_at=_parse_iso_or_none(row.get("created_at")),
            updated_at=_parse_iso_or_none(row.get("updated_at")),
            metadata={},
        )


# ═══════════════════════════════════════════════════════════════════════
# Messages
# ═══════════════════════════════════════════════════════════════════════


class SQLiteMessageReadAdapter:
    """Read-only adapter for ``MessageRepositoryPort.list_recent()``.

    Reads from the ``events`` table, filtering by session_id and
    ordering by seq_no ascending.  Only user and assistant role events
    are returned (tool events and system events are filtered out at
    the query level to keep the message window focused on conversation).
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def list_recent(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        rows = await self._db.fetchall(
            """SELECT id, user_id, session_id, role, event_type,
                      content, content_json, seq_no, created_at
               FROM events
               WHERE session_id = :sid
                 AND role IN ('user', 'assistant')
               ORDER BY seq_no DESC
               LIMIT :lim""",
            {"sid": session_id, "lim": limit},
        )
        # Reverse to ascending order (oldest first)
        rows.reverse()
        return [_row_to_message(r) for r in rows]


def _row_to_message(row: dict[str, Any]) -> ConversationMessage:
    """Convert an events row to a ConversationMessage."""
    metadata: dict[str, object] = {}
    if row.get("content_json") and row["content_json"] != "{}":
        try:
            parsed = json.loads(row["content_json"])
            if isinstance(parsed, dict):
                metadata = {k: v for k, v in parsed.items() if isinstance(v, (str, int, float, bool, list, dict))}
        except (json.JSONDecodeError, TypeError):
            pass

    if row.get("event_type"):
        metadata["event_type"] = row["event_type"]

    return ConversationMessage(
        message_id=row["id"],
        session_id=row["session_id"],
        actor_id=row.get("user_id"),
        role=row["role"],
        content=row.get("content", ""),
        sequence=row["seq_no"],
        created_at=_parse_iso(row["created_at"]),
        metadata=metadata,
    )


# ═══════════════════════════════════════════════════════════════════════
# Session Summary
# ═══════════════════════════════════════════════════════════════════════


class SQLiteSummaryReadAdapter:
    """Read-only adapter for ``SummaryRepositoryPort.get()``.

    Reads summary data from the ``sessions`` table
    (summary_text / summary_version columns).
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, session_id: str) -> SessionSummary | None:
        row = await self._db.fetchone(
            "SELECT summary_text, summary_version, summary_updated_at "
            "FROM sessions WHERE session_id = :sid",
            {"sid": session_id},
        )
        if row is None:
            return None
        content = row.get("summary_text")
        version = row.get("summary_version", 0)
        if not content:
            return None
        return SessionSummary(
            session_id=session_id,
            content=content,
            version=version,
            updated_at=_parse_iso_or_none(row.get("summary_updated_at")),
            metadata={},
        )


# ═══════════════════════════════════════════════════════════════════════
# User Profile
# ═══════════════════════════════════════════════════════════════════════


class SQLiteUserProfileReadAdapter:
    """Read-only adapter for ``UserProfileRepositoryPort.get()``."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, actor_id: str) -> UserProfile | None:
        row = await self._db.fetchone(
            "SELECT * FROM user_profiles WHERE actor_id = :aid",
            {"aid": actor_id},
        )
        if row is None:
            return None
        return UserProfile(
            actor_id=row["actor_id"],
            display_name=row.get("display_name"),
            locale=row.get("locale"),
            timezone=row.get("timezone"),
            metadata=_parse_metadata_json(row.get("metadata_json")),
        )


# ═══════════════════════════════════════════════════════════════════════
# User Settings
# ═══════════════════════════════════════════════════════════════════════


class SQLiteUserSettingsReadAdapter:
    """Read-only adapter for ``UserSettingsRepositoryPort.get()``."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, actor_id: str) -> UserSettings | None:
        row = await self._db.fetchone(
            "SELECT * FROM user_settings WHERE actor_id = :aid",
            {"aid": actor_id},
        )
        if row is None:
            return None
        return UserSettings(
            locale=row.get("locale", "zh-CN"),
            timezone=row.get("timezone", "UTC"),
            response_style=row.get("response_style"),
            tool_approval_mode=row.get("tool_approval_mode", "default"),
            metadata=_parse_metadata_json(row.get("metadata_json")),
        )


# ═══════════════════════════════════════════════════════════════════════
# Session Config
# ═══════════════════════════════════════════════════════════════════════


class SQLiteSessionConfigReadAdapter:
    """Read-only adapter for ``SessionConfigRepositoryPort.get()``."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, session_id: str) -> SessionConfig | None:
        row = await self._db.fetchone(
            "SELECT * FROM session_configs WHERE session_id = :sid",
            {"sid": session_id},
        )
        if row is None:
            return None
        return SessionConfig(
            history_limit=row.get("history_limit", 20),
            max_tool_rounds=row.get("max_tool_rounds"),
            model_profile=row.get("model_profile"),
            metadata=_parse_metadata_json(row.get("metadata_json")),
        )


# ═══════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string from the DB (e.g. ``2026-06-24T12:00:00.000Z``)."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return datetime(2026, 1, 1, 0, 0, 0)


def _parse_iso_or_none(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_iso(value)


def _parse_metadata_json(value: str | None) -> Mapping[str, object]:
    if not value or value == "{}":
        return {}
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return {k: v for k, v in parsed.items() if isinstance(v, (str, int, float, bool))}
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


__all__ = [
    "SQLiteMessageReadAdapter",
    "SQLiteSessionConfigReadAdapter",
    "SQLiteSessionReadAdapter",
    "SQLiteSummaryReadAdapter",
    "SQLiteUserProfileReadAdapter",
    "SQLiteUserSettingsReadAdapter",
]
