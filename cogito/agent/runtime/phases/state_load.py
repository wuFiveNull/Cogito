# cogito/agent/runtime/phases/state_load.py

from __future__ import annotations

import logging
from dataclasses import dataclass

from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.agent.ports.repositories import (
    MessageRepositoryPort,
    SessionConfigRepositoryPort,
    SessionRepositoryPort,
    SummaryRepositoryPort,
    UserProfileRepositoryPort,
    UserSettingsRepositoryPort,
)
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    InvalidLoadedStateError,
    MissingSessionError,
    SessionActorMismatchError,
    StateLoadError,
)
from cogito.agent.runtime.phase import BasePhase

logger = logging.getLogger(__name__)


# ── Null repository implementations ──────────────────────────────────


class _NullSessionRepository:
    """Returns None for all session lookups — used when no real repo is wired."""

    async def get(self, session_id: str) -> None:
        return None


class _NullMessageRepository:
    """Returns empty list for all message lookups."""

    async def list_recent(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        return []


class _NullSummaryRepository:
    """Returns None for all summary lookups."""

    async def get(self, session_id: str) -> None:
        return None


class _NullUserProfileRepository:
    """Returns None for all profile lookups."""

    async def get(self, actor_id: str) -> None:
        return None


class _NullUserSettingsRepository:
    """Returns None for all settings lookups."""

    async def get(self, actor_id: str) -> None:
        return None


class _NullSessionConfigRepository:
    """Returns None for all config lookups."""

    async def get(self, session_id: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class StateLoadOptions:
    """Immutable configuration for StateLoadPhase.

    Attributes:
        recent_message_limit: Max number of recent messages to load (0-200).
        allow_missing_session: If False, raises MissingSessionError when no
            SessionState is found for the request's session_id.
    """

    recent_message_limit: int = 20
    allow_missing_session: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.recent_message_limit <= 200:
            raise ValueError(
                f"recent_message_limit must be between 0 and 200, "
                f"got {self.recent_message_limit}",
            )


class StateLoadPhase(BasePhase):
    """Phase 2: Load deterministic state for the current turn.

    Responsibilities:
      - Load Session by session_id.
      - Load SessionConfig by session_id.
      - Load recent history messages (fixed window, not relevance-based).
      - Load SessionSummary by session_id.
      - Load UserProfile by actor_id.
      - Load UserSettings by actor_id.
      - Validate session-actor ownership.
      - Validate loaded data integrity (message session_id, ordering).
      - Apply session-level config overrides (max_tool_rounds).

    Explicitly does NOT:
      - Execute keyword or vector retrieval.
      - Build model_messages or prompt.
      - Call model or execute tools.
      - Write to any repository.
      - Publish MessageBus events.
      - Extract preferences or knowledge.
    """

    name = "state_load"

    def __init__(
        self,
        *,
        sessions: SessionRepositoryPort | None = None,
        messages: MessageRepositoryPort | None = None,
        summaries: SummaryRepositoryPort | None = None,
        user_profiles: UserProfileRepositoryPort | None = None,
        user_settings_repo: UserSettingsRepositoryPort | None = None,
        session_configs: SessionConfigRepositoryPort | None = None,
        default_user_settings: UserSettings | None = None,
        default_session_config: SessionConfig | None = None,
        options: StateLoadOptions | None = None,
    ) -> None:
        self._sessions = sessions or _NullSessionRepository()
        self._messages = messages or _NullMessageRepository()
        self._summaries = summaries or _NullSummaryRepository()
        self._user_profiles = user_profiles or _NullUserProfileRepository()
        self._user_settings = user_settings_repo or _NullUserSettingsRepository()
        self._session_configs = session_configs or _NullSessionConfigRepository()
        self._default_user_settings = (
            default_user_settings or UserSettings()
        )
        self._default_session_config = (
            default_session_config or SessionConfig()
        )
        self._options = options or StateLoadOptions()

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(self, ctx: TurnContext) -> None:
        request = ctx.request

        # 1. Session — must be loaded first for actor validation
        session = await self._load_session(request.session_id)

        if session is not None and session.actor_id != request.actor_id:
            raise SessionActorMismatchError(
                session_id=request.session_id,
                actor_id=request.actor_id,
            )

        if session is None and not self._options.allow_missing_session:
            raise MissingSessionError(request.session_id)

        # 2. SessionConfig — may affect history_limit
        config = await self._load_session_config(request.session_id)
        resolved_config = config or self._default_session_config

        # 3. Recent messages — limit depends on resolved config
        history_limit = self._resolve_history_limit(resolved_config)
        recent_messages = await self._load_recent_messages(
            session_id=request.session_id,
            limit=history_limit,
        )

        # 4. Summary
        summary = await self._load_summary(request.session_id)

        # 5. UserProfile
        profile = await self._load_user_profile(request.actor_id)

        # 6. UserSettings
        settings = await self._load_user_settings(request.actor_id)
        resolved_settings = settings or self._default_user_settings

        # 7. Validate loaded data integrity
        self._validate_loaded_state(
            request_session_id=request.session_id,
            recent_messages=recent_messages,
            config=resolved_config,
        )

        # 8. Atomic context update — only after all reads and validations pass
        ctx.session = session
        ctx.recent_messages = recent_messages
        ctx.session_summary = summary
        ctx.user_profile = profile
        ctx.user_settings = resolved_settings
        ctx.session_config = resolved_config

        # Apply session-level config override for max_tool_rounds
        if resolved_config.max_tool_rounds is not None:
            ctx.max_tool_rounds = resolved_config.max_tool_rounds

    # ------------------------------------------------------------------
    # Individual loaders
    # ------------------------------------------------------------------

    async def _load_session(self, session_id: str) -> SessionState | None:
        try:
            return await self._sessions.get(session_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="session",
                retryable=True,
            ) from exc

    async def _load_session_config(
        self,
        session_id: str,
    ) -> SessionConfig | None:
        try:
            return await self._session_configs.get(session_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="session_config",
                retryable=True,
            ) from exc

    async def _load_recent_messages(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        try:
            return await self._messages.list_recent(
                session_id=session_id,
                limit=limit,
            )
        except Exception as exc:
            raise StateLoadError.for_component(
                component="recent_messages",
                retryable=True,
            ) from exc

    async def _load_summary(
        self,
        session_id: str,
    ) -> SessionSummary | None:
        try:
            return await self._summaries.get(session_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="session_summary",
                retryable=True,
            ) from exc

    async def _load_user_profile(
        self,
        actor_id: str,
    ) -> UserProfile | None:
        try:
            return await self._user_profiles.get(actor_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="user_profile",
                retryable=True,
            ) from exc

    async def _load_user_settings(
        self,
        actor_id: str,
    ) -> UserSettings | None:
        try:
            return await self._user_settings.get(actor_id)
        except Exception as exc:
            raise StateLoadError.for_component(
                component="user_settings",
                retryable=True,
            ) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_history_limit(self, config: SessionConfig) -> int:
        """Determine how many recent messages to load."""
        return self._options.recent_message_limit

    @staticmethod
    def _validate_loaded_state(
        *,
        request_session_id: str,
        recent_messages: list[ConversationMessage],
        config: SessionConfig,
    ) -> None:
        """Validate cross-field integrity of loaded data."""
        for message in recent_messages:
            if message.session_id != request_session_id:
                raise InvalidLoadedStateError(
                    f"Repository returned message from session "
                    f"{message.session_id}, expected {request_session_id}",
                )

        sequences = [m.sequence for m in recent_messages]
        if sequences != sorted(sequences):
            raise InvalidLoadedStateError(
                "Recent messages must be ordered by sequence ascending",
            )

        if config.max_tool_rounds is not None:
            if not 1 <= config.max_tool_rounds <= 32:
                raise InvalidLoadedStateError(
                    f"max_tool_rounds must be between 1 and 32, "
                    f"got {config.max_tool_rounds}",
                )
