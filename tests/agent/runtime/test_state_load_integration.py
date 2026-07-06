"""
End-to-end integration tests for StateLoadPhase + real SQLite adapters.

Verifies that the full StateLoadPhase correctly loads deterministic state
from a real SQLite database when wired with the 6 read adapters.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.models import AgentRequest, TurnStatus
from cogito.agent.runtime.phases.state_load import (
    StateLoadOptions,
    StateLoadPhase,
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


def make_phase(db) -> StateLoadPhase:
    """Build a StateLoadPhase wired to real SQLite adapters."""
    return StateLoadPhase(
        sessions=SQLiteSessionReadAdapter(db),
        messages=SQLiteMessageReadAdapter(db),
        summaries=SQLiteSummaryReadAdapter(db),
        user_profiles=SQLiteUserProfileReadAdapter(db),
        user_settings_repo=SQLiteUserSettingsReadAdapter(db),
        session_configs=SQLiteSessionConfigReadAdapter(db),
        default_user_settings=UserSettings(locale="zh-CN", timezone="Asia/Tokyo"),
        default_session_config=SessionConfig(history_limit=20, max_tool_rounds=8),
        options=StateLoadOptions(recent_message_limit=20, allow_missing_session=True),
    )


async def seed_full_data(db) -> None:
    """Insert a complete set of related data for a known session."""
    now = NOW_ISO
    uid = "u-001"
    sid = "s-001"

    # Session
    await db.execute(
        """INSERT INTO sessions (session_id, user_id, version, next_seq_no,
                                 summary_text, summary_version, summary_updated_at,
                                 last_turn_id, last_request_id, last_message_at,
                                 created_at, updated_at)
           VALUES (:sid, :uid, 3, 15, :st, 2, :now, 'turn-002', 'req-002', :now, :now, :now)""",
        {"sid": sid, "uid": uid, "st": "User discussed restaurant preferences", "now": now},
    )

    # Messages
    for i in range(4):
        await db.execute(
            """INSERT INTO events (id, user_id, session_id, seq_no, role,
                                   event_type, content, created_at)
               VALUES (:id, :uid, :sid, :seq, :role, :etype, :content, :now)""",
            {
                "id": f"e-{i}",
                "uid": uid,
                "sid": sid,
                "seq": i + 1,
                "role": "user" if i % 2 == 0 else "assistant",
                "etype": "user_message" if i % 2 == 0 else "assistant_message",
                "content": f"Hello {i}" if i % 2 == 0 else f"Response {i}",
                "now": now,
            },
        )

    # Profile
    await db.execute(
        """INSERT INTO user_profiles (actor_id, display_name, locale, timezone, metadata_json, created_at, updated_at)
           VALUES (:uid, 'Alice', 'zh-CN', 'Asia/Shanghai', '{}', :now, :now)""",
        {"uid": uid, "now": now},
    )

    # Settings
    await db.execute(
        """INSERT INTO user_settings (actor_id, locale, timezone, response_style, tool_approval_mode, metadata_json, created_at, updated_at)
           VALUES (:uid, 'en', 'America/New_York', 'detailed', 'manual', '{}', :now, :now)""",
        {"uid": uid, "now": now},
    )

    # Config
    await db.execute(
        """INSERT INTO session_configs (session_id, history_limit, max_tool_rounds, model_profile, metadata_json, created_at, updated_at)
           VALUES (:sid, 30, 4, 'precise', '{}', :now, :now)""",
        {"sid": sid, "now": now},
    )


async def seed_session_only(db) -> None:
    """Insert only a session row — all other data is missing."""
    await db.execute(
        """INSERT INTO sessions (session_id, user_id, version, next_seq_no,
                                 created_at, updated_at)
           VALUES ('s-001', 'u-001', 0, 1, :now, :now)""",
        {"now": NOW_ISO},
    )


def make_context(*, session_id: str = "s-001", actor_id: str = "u-001") -> TurnContext:
    return TurnContext(
        request=AgentRequest(
            request_id="req-integration",
            session_id=session_id,
            actor_id=actor_id,
            text="test",
        ),
        turn_id="turn-integration",
        status=TurnStatus.RUNNING,
    )


# ═══════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════


class TestStateLoadPhaseFullIntegration:
    """End-to-end: StateLoadPhase reads all data from SQLite."""

    async def test_loads_full_state(self, db) -> None:
        await seed_full_data(db)
        phase = make_phase(db)
        ctx = make_context()

        await phase.execute(ctx)

        # Session
        assert isinstance(ctx.session, SessionState)
        assert ctx.session.session_id == "s-001"
        assert ctx.session.actor_id == "u-001"

        # Messages
        assert len(ctx.recent_messages) == 4
        assert all(isinstance(m, ConversationMessage) for m in ctx.recent_messages)
        sequences = [m.sequence for m in ctx.recent_messages]
        assert sequences == [1, 2, 3, 4]

        # Summary
        assert isinstance(ctx.session_summary, SessionSummary)
        assert "restaurant" in ctx.session_summary.content

        # Profile
        assert isinstance(ctx.user_profile, UserProfile)
        assert ctx.user_profile.display_name == "Alice"

        # Settings
        assert isinstance(ctx.user_settings, UserSettings)
        assert ctx.user_settings.locale == "en"
        assert ctx.user_settings.response_style == "detailed"

        # Config
        assert isinstance(ctx.session_config, SessionConfig)
        assert ctx.session_config.history_limit == 30
        assert ctx.session_config.max_tool_rounds == 4

        # max_tool_rounds override applied
        assert ctx.max_tool_rounds == 4

    async def test_loads_minimal_state_with_defaults(self, db) -> None:
        """Session exists but everything else is missing → defaults are used."""
        await seed_session_only(db)
        phase = make_phase(db)
        ctx = make_context()

        await phase.execute(ctx)

        assert ctx.session is not None
        assert ctx.recent_messages == []
        assert ctx.session_summary is None
        assert ctx.user_profile is None
        assert ctx.user_settings == UserSettings(locale="zh-CN", timezone="Asia/Tokyo")
        assert ctx.session_config == SessionConfig(history_limit=20, max_tool_rounds=8)
        assert ctx.max_tool_rounds == 8  # default from context, not overridden

    async def test_does_not_modify_downstream_fields(self, db) -> None:
        await seed_full_data(db)
        phase = make_phase(db)
        ctx = make_context()

        await phase.execute(ctx)

        assert ctx.retrieved_items == []
        assert ctx.model_messages == []
        assert ctx.model_responses == []
        assert ctx.output_text is None
        assert ctx.tool_records == []
        assert ctx.preference_candidates == []
        assert ctx.memory_candidates == []
        assert ctx.result is None
        assert ctx.persistence_completed is False
