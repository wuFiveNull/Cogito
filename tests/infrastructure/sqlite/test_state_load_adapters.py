"""
Unit tests for StateLoadPhase SQLite read adapters.

Verifies that each of the 6 adapters correctly maps database rows to
domain DTOs, handles missing data, and propagates storage errors.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.database import AsyncDatabase, run_migrations
from cogito.infrastructure.sqlite.repositories.state_load import (
    SQLiteMessageReadAdapter,
    SQLiteSessionConfigReadAdapter,
    SQLiteSessionReadAdapter,
    SQLiteSummaryReadAdapter,
    SQLiteUserProfileReadAdapter,
    SQLiteUserSettingsReadAdapter,
)

NOW_ISO = "2026-06-24T12:00:00.000Z"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    """Create a fresh migrated database for each test."""
    tmp = tempfile.mktemp(suffix=".db")
    db = AsyncDatabase(tmp)
    await db.open()
    await run_migrations(db)
    yield db
    await db.close()
    try:
        os.unlink(tmp)
    except OSError:
        pass


@pytest.fixture
async def seeded_db(db):
    """Database with seed data for all 3 new tables + sessions + events."""
    now = NOW_ISO

    # sessions — core control table (from migration v2)
    await db.execute(
        """INSERT INTO sessions (session_id, user_id, version, next_seq_no,
                                 summary_text, summary_version, summary_updated_at,
                                 last_turn_id, last_request_id, last_message_at,
                                 created_at, updated_at)
           VALUES (:sid, :uid, :v, :seq, :st, :sv, :su, :lt, :lr, :lm, :ca, :ua)""",
        {
            "sid": "s-001",
            "uid": "u-001",
            "v": 5,
            "seq": 42,
            "st": "User asked about weather",
            "sv": 3,
            "su": now,
            "lt": "turn-005",
            "lr": "req-005",
            "lm": now,
            "ca": now,
            "ua": now,
        },
    )

    # events — conversation messages
    for i in range(3):
        await db.execute(
            """INSERT INTO events (id, user_id, session_id, seq_no, role,
                                   event_type, content, created_at)
               VALUES (:id, :uid, :sid, :seq, :role, :etype, :content, :ca)""",
            {
                "id": f"msg-{i}",
                "uid": "u-001",
                "sid": "s-001",
                "seq": i + 1,
                "role": "user" if i % 2 == 0 else "assistant",
                "etype": "user_message" if i % 2 == 0 else "assistant_message",
                "content": f"Message {i}",
                "ca": now,
            },
        )

    # user_profiles
    await db.execute(
        """INSERT INTO user_profiles (actor_id, display_name, locale, timezone,
                                      metadata_json, created_at, updated_at)
           VALUES (:aid, :name, :loc, :tz, :meta, :ca, :ua)""",
        {
            "aid": "u-001",
            "name": "Alice",
            "loc": "zh-CN",
            "tz": "Asia/Shanghai",
            "meta": '{"theme": "dark"}',
            "ca": now,
            "ua": now,
        },
    )

    # user_settings
    await db.execute(
        """INSERT INTO user_settings (actor_id, locale, timezone, response_style,
                                      tool_approval_mode, metadata_json, created_at, updated_at)
           VALUES (:aid, :loc, :tz, :rs, :tam, :meta, :ca, :ua)""",
        {
            "aid": "u-001",
            "loc": "zh-CN",
            "tz": "Asia/Tokyo",
            "rs": "concise",
            "tam": "default",
            "meta": "{}",
            "ca": now,
            "ua": now,
        },
    )

    # session_configs
    await db.execute(
        """INSERT INTO session_configs (session_id, history_limit, max_tool_rounds,
                                        model_profile, metadata_json, created_at, updated_at)
           VALUES (:sid, :hl, :mtr, :mp, :meta, :ca, :ua)""",
        {
            "sid": "s-001",
            "hl": 25,
            "mtr": 6,
            "mp": "fast-model",
            "meta": "{}",
            "ca": now,
            "ua": now,
        },
    )

    return db


# ═══════════════════════════════════════════════════════════════════════
# SessionAdapter
# ═══════════════════════════════════════════════════════════════════════


class TestSQLiteSessionReadAdapter:
    async def test_get_returns_session_state(self, seeded_db) -> None:
        adapter = SQLiteSessionReadAdapter(seeded_db)
        result = await adapter.get("s-001")

        assert isinstance(result, SessionState)
        assert result.session_id == "s-001"
        assert result.actor_id == "u-001"
        assert result.created_at is not None
        assert result.updated_at is not None

    async def test_get_returns_none_for_missing(self, db) -> None:
        adapter = SQLiteSessionReadAdapter(db)
        result = await adapter.get("nonexistent")
        assert result is None

    async def test_get_raises_on_closed_db(self) -> None:
        adapter = SQLiteSessionReadAdapter(
            AsyncDatabase(tempfile.mktemp(suffix=".db")),
        )
        with pytest.raises(Exception):
            await adapter.get("s-001")


# ═══════════════════════════════════════════════════════════════════════
# MessageAdapter
# ═══════════════════════════════════════════════════════════════════════


class TestSQLiteMessageReadAdapter:
    async def test_list_recent_returns_messages(self, seeded_db) -> None:
        adapter = SQLiteMessageReadAdapter(seeded_db)
        results = await adapter.list_recent(session_id="s-001", limit=10)

        assert len(results) == 3
        assert all(isinstance(m, ConversationMessage) for m in results)

        # Must be ordered oldest first
        sequences = [m.sequence for m in results]
        assert sequences == sorted(sequences)

        # Check field mapping
        first = results[0]
        assert first.message_id == "msg-0"
        assert first.session_id == "s-001"
        assert first.role in ("user", "assistant")
        assert first.actor_id == "u-001"

    async def test_list_recent_limits_correctly(self, seeded_db) -> None:
        adapter = SQLiteMessageReadAdapter(seeded_db)
        results = await adapter.list_recent(session_id="s-001", limit=2)
        assert len(results) == 2

    async def test_list_recent_empty_for_missing_session(self, db) -> None:
        adapter = SQLiteMessageReadAdapter(db)
        results = await adapter.list_recent(session_id="nonexistent", limit=10)
        assert results == []


# ═══════════════════════════════════════════════════════════════════════
# SummaryAdapter
# ═══════════════════════════════════════════════════════════════════════


class TestSQLiteSummaryReadAdapter:
    async def test_get_returns_summary(self, seeded_db) -> None:
        adapter = SQLiteSummaryReadAdapter(seeded_db)
        result = await adapter.get("s-001")

        assert isinstance(result, SessionSummary)
        assert result.session_id == "s-001"
        assert result.content == "User asked about weather"
        assert result.version == 3
        assert result.updated_at is not None

    async def test_get_returns_none_when_no_summary(self, db) -> None:
        # Ensure a session exists but without summary
        await db.execute(
            """INSERT INTO sessions (session_id, user_id, version, next_seq_no,
                                     created_at, updated_at)
               VALUES ('s-empty', 'u-001', 0, 1, :ca, :ca)""",
            {"ca": "2026-06-24T12:00:00.000Z"},
        )
        adapter = SQLiteSummaryReadAdapter(db)
        result = await adapter.get("s-empty")
        assert result is None

    async def test_get_returns_none_for_missing_session(self, db) -> None:
        adapter = SQLiteSummaryReadAdapter(db)
        result = await adapter.get("nonexistent")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# UserProfileAdapter
# ═══════════════════════════════════════════════════════════════════════


class TestSQLiteUserProfileReadAdapter:
    async def test_get_returns_profile(self, seeded_db) -> None:
        adapter = SQLiteUserProfileReadAdapter(seeded_db)
        result = await adapter.get("u-001")

        assert isinstance(result, UserProfile)
        assert result.actor_id == "u-001"
        assert result.display_name == "Alice"
        assert result.locale == "zh-CN"
        assert result.timezone == "Asia/Shanghai"
        assert result.metadata.get("theme") == "dark"

    async def test_get_returns_none_for_missing(self, db) -> None:
        adapter = SQLiteUserProfileReadAdapter(db)
        result = await adapter.get("nonexistent")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# UserSettingsAdapter
# ═══════════════════════════════════════════════════════════════════════


class TestSQLiteUserSettingsReadAdapter:
    async def test_get_returns_settings(self, seeded_db) -> None:
        adapter = SQLiteUserSettingsReadAdapter(seeded_db)
        result = await adapter.get("u-001")

        assert isinstance(result, UserSettings)
        assert result.locale == "zh-CN"
        assert result.timezone == "Asia/Tokyo"
        assert result.response_style == "concise"
        assert result.tool_approval_mode == "default"

    async def test_get_returns_none_for_missing(self, db) -> None:
        adapter = SQLiteUserSettingsReadAdapter(db)
        result = await adapter.get("nonexistent")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# SessionConfigAdapter
# ═══════════════════════════════════════════════════════════════════════


class TestSQLiteSessionConfigReadAdapter:
    async def test_get_returns_config(self, seeded_db) -> None:
        adapter = SQLiteSessionConfigReadAdapter(seeded_db)
        result = await adapter.get("s-001")

        assert isinstance(result, SessionConfig)
        assert result.history_limit == 25
        assert result.max_tool_rounds == 6
        assert result.model_profile == "fast-model"

    async def test_get_returns_none_for_missing_session(self, db) -> None:
        adapter = SQLiteSessionConfigReadAdapter(db)
        result = await adapter.get("nonexistent")
        assert result is None
