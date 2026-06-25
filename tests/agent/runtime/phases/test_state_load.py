# tests/agent/runtime/phases/test_state_load.py

"""Unit tests for StateLoadPhase.

Covers spec requirements:
  - Complete state loading with all repositories.
  - New session / missing optional state uses defaults.
  - Session not found when allow_missing_session=False.
  - Session actor mismatch raises access denied error.
  - Each repository failure maps to StateLoadError.
  - Atomic context update on partial failure.
  - Message session_id and ordering validation.
  - SessionConfig overrides max_tool_rounds.
  - Phase does not modify downstream fields (retrieval, model, output).
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

import pytest

from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionLifecycle,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    InvalidLoadedStateError,
    MissingSessionError,
    SessionActorMismatchError,
    StateLoadError,
)
from cogito.agent.runtime.models import AgentRequest, TurnStatus
from cogito.agent.runtime.phases.state_load import (
    StateLoadOptions,
    StateLoadPhase,
)

T = TypeVar("T")

FIXED_TIME = datetime(2026, 6, 24, 12, 0, 0)


# ── Fake repositories ────────────────────────────────────────────────


class FakeGetRepository(Generic[T]):
    """Fake for single-get repositories (session, summary, profile, settings, config)."""

    def __init__(
        self,
        value: T | None = None,
        error: Exception | None = None,
    ) -> None:
        self.value = value
        self.error = error
        self.calls: list[str] = []

    async def get(self, key: str) -> T | None:
        self.calls.append(key)
        if self.error is not None:
            raise self.error
        return self.value


class FakeMessageRepository:
    """Fake for MessageRepositoryPort.list_recent."""

    def __init__(
        self,
        messages: list[ConversationMessage] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.messages = messages or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def list_recent(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        self.calls.append((session_id, limit))
        if self.error is not None:
            raise self.error
        return list(self.messages)


# ── Helpers ──────────────────────────────────────────────────────────


def make_context(
    *,
    request: AgentRequest | None = None,
    turn_id: str | None = "turn-001",
    started_at: datetime | None = FIXED_TIME,
    status: TurnStatus = TurnStatus.RUNNING,
    **overrides: object,
) -> TurnContext:
    """Build a TurnContext as it would be after TurnInitPhase."""
    req = request or AgentRequest(
        request_id="req-001",
        session_id="s-001",
        actor_id="u-001",
        text="hello",
    )
    ctx = TurnContext(
        request=req,
        turn_id=turn_id,
        started_at=started_at,
        status=status,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def make_phase(
    *,
    session: object | None = None,
    messages: list[ConversationMessage] | None = None,
    summary: object | None = None,
    profile: object | None = None,
    settings: object | None = None,
    config: object | None = None,
    default_user_settings: UserSettings | None = None,
    default_session_config: SessionConfig | None = None,
    options: StateLoadOptions | None = None,
    session_error: Exception | None = None,
    messages_error: Exception | None = None,
    summary_error: Exception | None = None,
    profile_error: Exception | None = None,
    settings_error: Exception | None = None,
    config_error: Exception | None = None,
) -> StateLoadPhase:
    """Build a StateLoadPhase with the given repository fixtures."""
    return StateLoadPhase(
        sessions=FakeGetRepository(value=session, error=session_error),
        messages=FakeMessageRepository(messages=messages, error=messages_error),
        summaries=FakeGetRepository(value=summary, error=summary_error),
        user_profiles=FakeGetRepository(value=profile, error=profile_error),
        user_settings_repo=FakeGetRepository(value=settings, error=settings_error),
        session_configs=FakeGetRepository(value=config, error=config_error),
        default_user_settings=default_user_settings,
        default_session_config=default_session_config,
        options=options,
    )


def make_session(*, session_id: str = "s-001", actor_id: str = "u-001") -> SessionState:
    return SessionState(session_id=session_id, actor_id=actor_id)


def make_config(*, max_tool_rounds: int | None = None) -> SessionConfig:
    return SessionConfig(max_tool_rounds=max_tool_rounds)


def make_message(
    *,
    message_id: str = "m-001",
    session_id: str = "s-001",
    sequence: int = 0,
) -> ConversationMessage:
    return ConversationMessage(
        message_id=message_id,
        session_id=session_id,
        actor_id="u-001",
        role="user",
        content="test",
        sequence=sequence,
        created_at=FIXED_TIME,
    )


# ── Normal path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loads_deterministic_state_into_context() -> None:
    """All repositories provide data; context gets fully populated."""
    session = make_session()
    messages = [
        make_message(message_id="m-001", sequence=0),
        make_message(message_id="m-002", sequence=1),
    ]
    summary = SessionSummary(session_id="s-001", content="summary text", version=1)
    profile = UserProfile(actor_id="u-001", display_name="Alice", locale="zh-CN")
    settings = UserSettings(locale="zh-CN", timezone="Asia/Tokyo")
    config = SessionConfig(history_limit=20, max_tool_rounds=4)

    phase = make_phase(
        session=session,
        messages=messages,
        summary=summary,
        profile=profile,
        settings=settings,
        config=config,
    )
    ctx = make_context()

    await phase.execute(ctx)

    assert ctx.session == session
    assert ctx.recent_messages == messages
    assert ctx.session_summary == summary
    assert ctx.user_profile == profile
    assert ctx.user_settings == settings
    assert ctx.session_config == config
    assert ctx.max_tool_rounds == 4

    # Must not modify downstream fields
    assert ctx.retrieved_items == []
    assert ctx.model_messages == []
    assert ctx.output_text is None
    assert ctx.preference_candidates == []
    assert ctx.memory_candidates == []


@pytest.mark.asyncio
async def test_does_not_override_max_tool_rounds_when_config_not_set() -> None:
    """When SessionConfig.max_tool_rounds is None, max_tool_rounds stays at default."""
    session = make_session()
    config = SessionConfig(max_tool_rounds=None)  # not set
    phase = make_phase(session=session, config=config)
    ctx = make_context()

    await phase.execute(ctx)

    # Default from TurnContext is 8; should not be overridden
    assert ctx.max_tool_rounds == 8


# ── Missing optional state ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_missing_state_uses_defaults() -> None:
    """When all repositories return None, context uses defaults."""
    phase = make_phase(
        session=None,
        messages=[],
        summary=None,
        profile=None,
        settings=None,
        config=None,
    )
    ctx = make_context()

    await phase.execute(ctx)

    assert ctx.session is None
    assert ctx.recent_messages == []
    assert ctx.session_summary is None
    assert ctx.user_profile is None
    assert ctx.user_settings == UserSettings()
    assert ctx.session_config == SessionConfig()
    assert ctx.max_tool_rounds == 8  # unchanged from context default


@pytest.mark.asyncio
async def test_allow_missing_session_true_is_default() -> None:
    """Default options allow missing session; no error."""
    phase = make_phase(session=None)
    ctx = make_context()

    await phase.execute(ctx)  # should not raise

    assert ctx.session is None


@pytest.mark.asyncio
async def test_allow_missing_session_false_raises() -> None:
    """When allow_missing_session=False and no session, MissingSessionError."""
    phase = make_phase(
        session=None,
        options=StateLoadOptions(allow_missing_session=False),
    )
    ctx = make_context()

    with pytest.raises(MissingSessionError) as excinfo:
        await phase.execute(ctx)

    assert "s-001" in str(excinfo.value)


# ── Session actor mismatch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_actor_mismatch_raises() -> None:
    """Session belonging to another actor raises SessionActorMismatchError."""
    session = make_session(session_id="s-001", actor_id="other-user")
    phase = make_phase(session=session)
    ctx = make_context()

    with pytest.raises(SessionActorMismatchError) as excinfo:
        await phase.execute(ctx)

    # Safe message must not leak the actual actor
    safe = excinfo.value.safe_message
    assert "other-user" not in (safe or "")
    assert "不可访问" in (safe or "")


# ── Repository failures ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_repository_failure_raises_state_load_error() -> None:
    """Session repository failure maps to StateLoadError."""
    phase = make_phase(session_error=RuntimeError("db down"))
    ctx = make_context()

    with pytest.raises(StateLoadError) as excinfo:
        await phase.execute(ctx)

    assert excinfo.value.component == "session"
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_message_repository_failure_raises_state_load_error() -> None:
    """Message repository failure maps to StateLoadError."""
    session = make_session()
    phase = make_phase(session=session, messages_error=RuntimeError("db down"))
    ctx = make_context()

    with pytest.raises(StateLoadError) as excinfo:
        await phase.execute(ctx)

    assert excinfo.value.component == "recent_messages"
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_summary_repository_failure_raises_state_load_error() -> None:
    """Summary repository failure maps to StateLoadError."""
    session = make_session()
    phase = make_phase(session=session, summary_error=RuntimeError("db down"))
    ctx = make_context()

    with pytest.raises(StateLoadError) as excinfo:
        await phase.execute(ctx)

    assert excinfo.value.component == "session_summary"
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_profile_repository_failure_raises_state_load_error() -> None:
    """UserProfile repository failure maps to StateLoadError."""
    session = make_session()
    phase = make_phase(session=session, profile_error=RuntimeError("db down"))
    ctx = make_context()

    with pytest.raises(StateLoadError) as excinfo:
        await phase.execute(ctx)

    assert excinfo.value.component == "user_profile"
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_settings_repository_failure_raises_state_load_error() -> None:
    """UserSettings repository failure maps to StateLoadError."""
    session = make_session()
    phase = make_phase(session=session, settings_error=RuntimeError("db down"))
    ctx = make_context()

    with pytest.raises(StateLoadError) as excinfo:
        await phase.execute(ctx)

    assert excinfo.value.component == "user_settings"
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_config_repository_failure_raises_state_load_error() -> None:
    """SessionConfig repository failure maps to StateLoadError."""
    session = make_session()
    phase = make_phase(session=session, config_error=RuntimeError("db down"))
    ctx = make_context()

    with pytest.raises(StateLoadError) as excinfo:
        await phase.execute(ctx)

    assert excinfo.value.component == "session_config"
    assert excinfo.value.retryable is True


# ── Atomic context update ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_last_repository_failure_preserves_context() -> None:
    """When the last repository fails, context fields remain at their pre-phase values."""
    session = make_session()
    ctx = make_context(
        session=None,
        recent_messages=[],
        session_summary=None,
        user_profile=None,
        user_settings=UserSettings(locale="en"),
    )

    # Set up the last loader (user_settings) to fail
    phase = make_phase(
        session=session,
        messages=[],
        summary=None,
        profile=None,
        settings_error=RuntimeError("db down"),
        config=SessionConfig(),
    )

    with pytest.raises(StateLoadError):
        await phase.execute(ctx)

    # Context must retain the pre-phase values
    assert ctx.session is None
    assert ctx.recent_messages == []
    assert ctx.session_summary is None
    assert ctx.user_profile is None
    assert ctx.user_settings == UserSettings(locale="en")


# ── Data validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_session_id_mismatch_raises() -> None:
    """Message from another session raises InvalidLoadedStateError."""
    session = make_session()
    messages = [make_message(session_id="wrong-session", sequence=0)]
    phase = make_phase(session=session, messages=messages)
    ctx = make_context()

    with pytest.raises(InvalidLoadedStateError) as excinfo:
        await phase.execute(ctx)

    assert "wrong-session" in str(excinfo.value)


@pytest.mark.asyncio
async def test_message_out_of_order_raises() -> None:
    """Messages not sorted by sequence ascending raise InvalidLoadedStateError."""
    session = make_session()
    messages = [
        make_message(message_id="m-003", sequence=3),
        make_message(message_id="m-001", sequence=1),
        make_message(message_id="m-002", sequence=2),
    ]
    phase = make_phase(session=session, messages=messages)
    ctx = make_context()

    with pytest.raises(InvalidLoadedStateError) as excinfo:
        await phase.execute(ctx)

    assert "ascending" in str(excinfo.value)


@pytest.mark.asyncio
async def test_config_max_tool_rounds_out_of_range_raises() -> None:
    """max_tool_rounds outside 1-32 raises InvalidLoadedStateError."""
    session = make_session()
    config = SessionConfig(max_tool_rounds=99)
    phase = make_phase(session=session, config=config)
    ctx = make_context()

    with pytest.raises(InvalidLoadedStateError) as excinfo:
        await phase.execute(ctx)

    assert "max_tool_rounds" in str(excinfo.value)


@pytest.mark.asyncio
async def test_config_max_tool_rounds_too_low_raises() -> None:
    """max_tool_rounds of 0 raises InvalidLoadedStateError."""
    session = make_session()
    config = SessionConfig(max_tool_rounds=0)
    phase = make_phase(session=session, config=config)
    ctx = make_context()

    with pytest.raises(InvalidLoadedStateError):
        await phase.execute(ctx)


# ── Phase boundary enforcement ───────────────────────────────────────


@pytest.mark.asyncio
async def test_does_not_modify_downstream_fields() -> None:
    """Phase must not touch retriever, model, or output fields."""
    session = make_session()
    phase = make_phase(session=session)
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


@pytest.mark.asyncio
async def test_phase_requires_only_read_methods() -> None:
    """Phase should be testable with repos that only have get/list_recent (no save/commit)."""
    session = make_session()

    phase = StateLoadPhase(
        sessions=FakeGetRepository(value=session),
        messages=FakeMessageRepository(messages=[]),
        summaries=FakeGetRepository(value=None),
        user_profiles=FakeGetRepository(value=None),
        user_settings_repo=FakeGetRepository(value=None),
        session_configs=FakeGetRepository(value=None),
    )
    ctx = make_context()

    await phase.execute(ctx)

    assert ctx.session == session


# ── StateLoadOptions validation ──────────────────────────────────────


def test_state_load_options_invalid_limit_negative_raises() -> None:
    """Negative recent_message_limit raises ValueError."""
    with pytest.raises(ValueError, match="recent_message_limit"):
        StateLoadOptions(recent_message_limit=-1)


def test_state_load_options_invalid_limit_too_high_raises() -> None:
    """recent_message_limit above 200 raises ValueError."""
    with pytest.raises(ValueError, match="recent_message_limit"):
        StateLoadOptions(recent_message_limit=201)


def test_state_load_options_defaults() -> None:
    """Default options should have sensible values."""
    opts = StateLoadOptions()
    assert opts.recent_message_limit == 20
    assert opts.allow_missing_session is True


def test_state_load_options_custom_values() -> None:
    """Custom options values should be accepted."""
    opts = StateLoadOptions(recent_message_limit=50, allow_missing_session=False)
    assert opts.recent_message_limit == 50
    assert opts.allow_missing_session is False


# ── Repository call tracking ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_repositories_called_with_correct_ids() -> None:
    """Each repository is called with the correct identifier."""
    session = make_session()
    msg = make_message()
    summary = SessionSummary(session_id="s-001", content="s", version=1)
    profile = UserProfile(actor_id="u-001")
    settings = UserSettings()
    config = SessionConfig()

    sessions_repo = FakeGetRepository(value=session)
    messages_repo = FakeMessageRepository(messages=[msg])
    summaries_repo = FakeGetRepository(value=summary)
    profiles_repo = FakeGetRepository(value=profile)
    settings_repo = FakeGetRepository(value=settings)
    configs_repo = FakeGetRepository(value=config)

    phase = StateLoadPhase(
        sessions=sessions_repo,
        messages=messages_repo,
        summaries=summaries_repo,
        user_profiles=profiles_repo,
        user_settings_repo=settings_repo,
        session_configs=configs_repo,
    )
    ctx = make_context()

    await phase.execute(ctx)

    assert sessions_repo.calls == ["s-001"]
    assert messages_repo.calls == [("s-001", 20)]
    assert summaries_repo.calls == ["s-001"]
    assert profiles_repo.calls == ["u-001"]
    assert settings_repo.calls == ["u-001"]
    assert configs_repo.calls == ["s-001"]
