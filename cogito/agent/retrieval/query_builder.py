# cogito/agent/retrieval/query_builder.py
#
# RetrievalQueryBuilder — builds a strongly typed RetrievalQuery
# from the current TurnContext.
#
# This is a pure component with no I/O.  It performs deterministic
# text normalisation and access-context construction.

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from cogito.agent.domain.retrieval import (
    RetrievalAccessContext,
    RetrievalFilters,
    RetrievalQuery,
)
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import InvalidRetrievalContextError

_MAX_QUERY_CHARS = 4096
_NFKC_TRANSLATE_TABLE = str.maketrans(
    {
        "　": " ",  # ideographic space → regular space
        " ": " ",  # non-breaking space → regular space
    }
)


@dataclass(frozen=True, slots=True)
class RetrievalQueryBuilder:
    """Builds a RetrievalQuery from the current TurnContext.

    This component is stateless and safe to reuse across turns.
    """

    default_limit: int = 20

    def build(self, ctx: TurnContext) -> RetrievalQuery:
        """Construct a RetrievalQuery from context.

        Raises:
            InvalidRetrievalContextError: if ``turn_id`` is not set
                (TurnInitPhase must run first).
        """
        if ctx.turn_id is None:
            raise InvalidRetrievalContextError(
                "turn_id is required before retrieval",
            )

        text = self._normalize_text(ctx.request.text)
        access = self._build_access_context(ctx)
        filters = self._build_filters(ctx)
        locale = self._resolve_locale(ctx)

        return RetrievalQuery(
            request_id=ctx.request.request_id,
            turn_id=ctx.turn_id,
            text=text,
            access=access,
            filters=filters,
            limit=self.default_limit,
            locale=locale,
        )

    # ── Text normalisation ────────────────────────────────────────────

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Deterministic text normalisation.

        Rules:
          1. Unicode NFKC normalisation.
          2. Strip leading/trailing whitespace.
          3. Fold consecutive whitespace into one space.
          4. Cap at ``_MAX_QUERY_CHARS``.
        """
        if not text:
            return ""

        # Normalise Unicode
        text = unicodedata.normalize("NFKC", text)

        # Translate special spaces
        text = text.translate(_NFKC_TRANSLATE_TABLE)

        # Strip and collapse whitespace
        text = text.strip()
        parts = text.split()
        text = " ".join(parts)

        # Cap length
        if len(text) > _MAX_QUERY_CHARS:
            text = text[:_MAX_QUERY_CHARS]

        return text

    # ── Access context ────────────────────────────────────────────────

    @staticmethod
    def _build_access_context(ctx: TurnContext) -> RetrievalAccessContext:
        """Build access context from the request and loaded state.

        Uses trusted state (session, user_profile) rather than raw
        user text to determine tenant/namespace.
        """
        tenant_id: str | None = None
        namespace: str | None = None

        if ctx.session is not None:
            metadata = ctx.session.metadata
            tenant_id = str(metadata.get("tenant_id")) if isinstance(metadata, dict) else None
            namespace = str(metadata.get("namespace")) if isinstance(metadata, dict) else None

        return RetrievalAccessContext(
            actor_id=ctx.request.actor_id,
            session_id=ctx.request.session_id,
            tenant_id=tenant_id,
            namespace=namespace,
        )

    # ── Filters ───────────────────────────────────────────────────────

    @staticmethod
    def _build_filters(ctx: TurnContext) -> RetrievalFilters:
        """Build filters from trusted context metadata.

        Only metadata that has been validated by StateLoadPhase or
        TurnInitPhase is used here.  Raw user text is NOT used to
        construct filters (prevents privilege escalation).
        """
        return RetrievalFilters(
            language=ctx.user_settings.locale if ctx.user_settings else None,
        )

    # ── Locale ────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_locale(ctx: TurnContext) -> str | None:
        """Resolve the locale from user settings or profile."""
        if ctx.user_settings and ctx.user_settings.locale:
            return ctx.user_settings.locale
        if ctx.user_profile and ctx.user_profile.locale:
            return ctx.user_profile.locale
        return None
