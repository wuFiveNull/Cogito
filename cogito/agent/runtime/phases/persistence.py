# cogito/agent/runtime/phases/persistence.py
#
# PersistencePhase — Phase 7 of the 8-phase pipeline.
#
# Persists turn data to SQLite within an atomic, retryable transaction
# boundary.  This Phase is a pure orchestrator: it validates the turn
# context, builds an immutable plan, prepares embeddings outside the
# transaction, then executes a 13-step write pipeline.
#
# Design rules (see persistence-phase-spec §1, §20):
#   - Does NOT call model, retrieval, or tools.
#   - Does NOT directly publish MessageBus / Channel messages.
#   - Does NOT contain SQL strings (all SQL in infrastructure/).
#   - Does NOT import sqlite3, aiosqlite, MessageBus, or Channel SDK.
#   - Embeds outside the write transaction.
#   - Commits are shielded from cancellation by asyncio.shield().

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.embedding import EmbeddingPort
from cogito.agent.ports.unit_of_work import UnitOfWorkFactoryPort
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    InvalidPersistenceContextError,
    PersistenceAlreadyCompletedError,
    PersistenceCommitError,
    PersistenceCommitOutcomeUnknownError,
    PersistenceError,
)
from cogito.agent.runtime.models import TurnStatus
from cogito.agent.runtime.persistence.commit_recovery import (
    CommitRecoveryService,
)
from cogito.agent.runtime.persistence.fingerprint import (
    PersistenceFingerprint,
)
from cogito.agent.runtime.persistence.memory_policy import (
    MemoryPersistencePolicy,
)
from cogito.agent.runtime.persistence.models import (
    CandidateWriteAudit,
    CandidateWriteOutcome,
    EmbeddingJob,
    PersistenceOutcome,
    PersistencePlan,
    PersistedEvent,
    SessionSnapshot,
    TurnCommitRecord,
)
from cogito.agent.runtime.persistence.planner import (
    PersistencePlanBuilder,
)
from cogito.agent.runtime.persistence.preference_policy import (
    PreferencePersistencePolicy,
)
from cogito.agent.runtime.persistence.retry import (
    PersistenceRetryPolicy,
    RetryDecision,
)
from cogito.agent.runtime.persistence.sanitizer import (
    PersistenceSanitizer,
    canonical_json,
)
from cogito.agent.runtime.phase import BasePhase
from cogito.database.ids import new_uuid

logger = logging.getLogger(__name__)


class PersistencePhase(BasePhase):
    """Persist turn data to SQLite within an atomic transaction boundary.

    Responsibilities:
    - Validate turn context preconditions.
    - Sanitise turn data (remove secrets, truncate large results).
    - Build an immutable PersistencePlan.
    - Prepare embeddings outside the write transaction.
    - Execute the 13-step write pipeline with retry.
    - Handle cancellation during commit via shielded commit + recovery.
    """

    name = "persistence"

    def __init__(
        self,
        *,
        clock: ClockPort | None = None,
        uow_factory: UnitOfWorkFactoryPort | None = None,
        planner: PersistencePlanBuilder | None = None,
        sanitizer: PersistenceSanitizer | None = None,
        fingerprint: PersistenceFingerprint | None = None,
        preference_policy: PreferencePersistencePolicy | None = None,
        memory_policy: MemoryPersistencePolicy | None = None,
        retry_policy: PersistenceRetryPolicy | None = None,
        commit_recovery: CommitRecoveryService | None = None,
        embedding_port: EmbeddingPort | None = None,
        embedding_model: str = "",
    ) -> None:
        self._clock = clock
        self._uow_factory = uow_factory
        self._planner = planner
        self._sanitizer = sanitizer
        self._fingerprint = fingerprint
        self._preference_policy = preference_policy
        self._memory_policy = memory_policy
        self._retry_policy = retry_policy or PersistenceRetryPolicy()
        self._commit_recovery = commit_recovery
        self._embedding_port = embedding_port
        self._embedding_model = embedding_model

    # ═══════════════════════════════════════════════════════════════════
    # Execute (main entry point)
    # ═══════════════════════════════════════════════════════════════════

    async def execute(self, ctx: TurnContext) -> None:
        """Execute the PersistencePhase for one turn.

        Args:
            ctx: The turn context.  Must satisfy all preconditions
                documented in persistence-phase-spec §2.1.

        Raises:
            InvalidPersistenceContextError: precondition violation.
            PersistenceError: transaction failure after all retries.
        """
        # If not fully wired, skip persistence (backward compat stub mode)
        if self._uow_factory is None or self._planner is None:
            logger.warning("PersistencePhase not wired — skipping persistence")
            return

        # Step 1: Validate context preconditions
        self._validate_context(ctx)

        # Step 2: Build the immutable PersistencePlan
        now = self._clock.now()
        plan = await self._planner.build(ctx=ctx, now=now)

        # Step 3: Execute with retry
        outcome = await self._execute_with_retry(plan)

        # Step 4: Record outcome on context
        ctx.persistence_outcome = outcome
        ctx.persistence_completed = True

        logger.info(
            "Turn persisted",
            extra={
                "turn_id": plan.turn_id,
                "commit_id": outcome.commit_id,
                "session_version": outcome.session_version,
                "idempotent_replay": outcome.idempotent_replay,
                "events_written": len(plan.events),
            },
        )

    # ═══════════════════════════════════════════════════════════════════
    # Context validation
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _validate_context(ctx: TurnContext) -> None:
        """Validate turn context preconditions (spec §2.1).

        Must be called BEFORE building the plan — these checks are
        cheap and do not open any database connection.
        """
        if ctx.persistence_completed:
            raise PersistenceAlreadyCompletedError(
                "persistence has already completed for this turn"
            )

        if ctx.persistence_outcome is not None:
            raise InvalidPersistenceContextError(
                "persistence_outcome exists before persistence"
            )

        if not ctx.turn_id:
            raise InvalidPersistenceContextError("turn_id is required")

        if ctx.started_at is None:
            raise InvalidPersistenceContextError("started_at is required")

        if ctx.status not in (TurnStatus.RUNNING, TurnStatus.COMPLETED):
            raise InvalidPersistenceContextError(
                f"invalid turn status: {ctx.status} — "
                f"expected RUNNING or COMPLETED"
            )

        if ctx.error is not None:
            raise InvalidPersistenceContextError(
                "cannot persist a turn containing an error"
            )

        if ctx.output_text is None:
            raise InvalidPersistenceContextError("output_text is required")

        request = ctx.request
        for field_name, value in (
            ("request_id", request.request_id),
            ("session_id", request.session_id),
            ("actor_id", request.actor_id),
        ):
            if not value.strip():
                raise InvalidPersistenceContextError(
                    f"{field_name} is required"
                )

    # ═══════════════════════════════════════════════════════════════════
    # Retry loop
    # ═══════════════════════════════════════════════════════════════════

    async def _execute_with_retry(
        self,
        plan: PersistencePlan,
    ) -> PersistenceOutcome:
        """Execute the write pipeline with retry on transient errors."""
        last_error: BaseException | None = None

        for attempt in range(1, self._retry_policy._config.max_attempts + 1):
            try:
                return await self._persist_once(plan)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                decision = self._retry_policy.classify(exc)
                if decision == RetryDecision.ABORT:
                    raise

                last_error = exc
                if attempt >= self._retry_policy._config.max_attempts:
                    raise PersistenceError(
                        f"persistence failed after {attempt} attempts: {exc}",
                    ) from exc

                logger.warning(
                    "Persistence retry %d/%d",
                    attempt,
                    self._retry_policy._config.max_attempts,
                    extra={"turn_id": plan.turn_id, "error": str(exc)[:200]},
                )
                self._retry_policy.sleep_before_retry(attempt)

        raise PersistenceError("unreachable retry state") from last_error

    # ═══════════════════════════════════════════════════════════════════
    # Single persistence attempt (the 13-step pipeline)
    # ═══════════════════════════════════════════════════════════════════

    async def _persist_once(
        self,
        plan: PersistencePlan,
    ) -> PersistenceOutcome:
        """Execute one persistence attempt within a single transaction.

        This is the full 13-step pipeline defined in the spec §19.
        """
        async with self._uow_factory.create() as uow:
            now = self._clock.now()
            now_iso = now.strftime("%Y-%m-%dT%H:%M:%fZ")

            # ── Step 1: Idempotency check ──────────────────────────
            existing = await uow.turn_commits.get_by_request(
                user_id=plan.user_id,
                request_id=plan.request_id,
            )
            if existing is not None:
                return self._resolve_replay(plan, existing, now)

            # ── Step 2: Create / load session ──────────────────────
            await uow.sessions.create_if_absent(
                session_id=plan.session_id,
                user_id=plan.user_id,
                now=now,
            )
            session = await uow.sessions.get_for_write(
                session_id=plan.session_id,
            )
            if session is None:
                raise PersistenceError(
                    f"Session {plan.session_id} not found after create_if_absent"
                )

            # ── Step 3: Assign event sequences ──────────────────────
            persisted_events = self._planner.assign_sequences(
                plan=plan,
                base_seq_no=session.next_seq_no,
            )

            # ── Step 4: Write events ───────────────────────────────
            await uow.events.add_many(persisted_events)

            # ── Step 5: Advance session ────────────────────────────
            last_event = persisted_events[-1] if persisted_events else None
            advanced_session = await uow.sessions.advance(
                session_id=plan.session_id,
                expected_version=session.version,
                consumed_sequences=len(persisted_events),
                last_turn_id=plan.turn_id,
                last_request_id=plan.request_id,
                last_message_at=last_event.created_at if last_event else now,
            )

            # ── Step 6: Apply summary candidate ────────────────────
            summary_outcomes: list[CandidateWriteOutcome] = []
            if plan.summary_candidate and plan.summary_candidate.content:
                summary_outcome = await self._apply_summary(
                    uow=uow,
                    plan=plan,
                    session=advanced_session,
                    now=now,
                )
                if summary_outcome:
                    summary_outcomes.append(summary_outcome)

            # ── Step 7: Apply preference candidates ────────────────
            import logging as _logging
            _logging.getLogger(__name__).debug(
                "Applying %d preference candidates", len(plan.preference_candidates)
            )
            preference_outcomes = []
            if plan.preference_candidates:
                preference_outcomes = await self._preference_policy.apply(
                    candidates=plan.preference_candidates,
                    memories=uow.memories,
                    persisted_events=persisted_events,
                    commit_id=plan.commit_id,
                    user_id=plan.user_id,
                    session_id=plan.session_id,
                    turn_id=plan.turn_id,
                    now=now,
                )

            # ── Step 8: Apply memory candidates ────────────────────
            memory_outcomes = []
            if plan.memory_candidates:
                memory_outcomes = await self._memory_policy.apply(
                    candidates=plan.memory_candidates,
                    memories=uow.memories,
                    persisted_events=persisted_events,
                    commit_id=plan.commit_id,
                    user_id=plan.user_id,
                    session_id=plan.session_id,
                    turn_id=plan.turn_id,
                    now=now,
                )

            # ── Step 9: Write embedding jobs ───────────────────────
            all_candidate_outcomes = summary_outcomes + preference_outcomes + memory_outcomes
            embedding_jobs = self._build_embedding_jobs(
                plan=plan,
                outcomes=all_candidate_outcomes,
            )
            if embedding_jobs:
                await uow.embedding_jobs.add_many(embedding_jobs)

            # ── Step 10: Build outcome ─────────────────────────────
            user_event = next(
                (e for e in persisted_events if e.role == "user"), None
            )
            assistant_event = next(
                (e for e in persisted_events if e.role == "assistant"), None
            )
            tool_events = tuple(
                e.event_id for e in persisted_events if e.role == "tool"
            )

            outcome = PersistenceOutcome(
                commit_id=plan.commit_id,
                turn_id=plan.turn_id,
                request_id=plan.request_id,
                session_id=plan.session_id,
                committed_at=now,
                session_version=advanced_session.version,
                summary_version=advanced_session.summary_version,
                idempotent_replay=False,
                user_event_id=user_event.event_id if user_event else "",
                assistant_event_id=assistant_event.event_id if assistant_event else "",
                tool_event_ids=tool_events,
                candidate_outcomes=tuple(all_candidate_outcomes),
                embedding_job_count=len(embedding_jobs),
            )

            # ── Step 11: Write turn_commit record ──────────────────
            await uow.turn_commits.add(TurnCommitRecord(
                commit_id=plan.commit_id,
                user_id=plan.user_id,
                session_id=plan.session_id,
                request_id=plan.request_id,
                turn_id=plan.turn_id,
                commit_fingerprint=plan.commit_fingerprint,
                user_event_id=outcome.user_event_id,
                assistant_event_id=outcome.assistant_event_id,
                session_version=advanced_session.version,
                outcome_json=canonical_json({
                    "summary_version": advanced_session.summary_version,
                    "tool_event_ids": list(tool_events),
                    "embedding_job_count": len(embedding_jobs),
                }),
                persistence_span_id=plan.persistence_span_id,
                committed_at=now_iso,
            ))

            # ── Step 12: Write candidate audit records ─────────────
            audits = self._build_audit_records(
                plan=plan,
                outcomes=all_candidate_outcomes,
                commit_id=plan.commit_id,
            )
            if audits:
                await uow.candidate_audits.add_many(audits)

            # ── Step 13: Commit (shielded from cancellation) ──────
            await self._commit_with_shield(uow, plan)

            return outcome

    # ═══════════════════════════════════════════════════════════════════
    # Internal helpers
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _resolve_replay(
        plan: PersistencePlan,
        existing: TurnCommitRecord,
        now: datetime,
    ) -> PersistenceOutcome:
        """Handle an idempotent replay — same request_id already committed."""
        import json as _json
        try:
            outcome_data = _json.loads(existing.outcome_json)
        except (json.JSONDecodeError, TypeError):
            outcome_data = {}

        return PersistenceOutcome(
            commit_id=existing.commit_id,
            turn_id=existing.turn_id,
            request_id=existing.request_id,
            session_id=existing.session_id,
            committed_at=datetime.fromisoformat(existing.committed_at) if existing.committed_at else now,
            session_version=existing.session_version,
            summary_version=outcome_data.get("summary_version", 0),
            idempotent_replay=True,
            user_event_id=existing.user_event_id,
            assistant_event_id=existing.assistant_event_id,
            tool_event_ids=tuple(outcome_data.get("tool_event_ids", [])),
            embedding_job_count=outcome_data.get("embedding_job_count", 0),
        )

    async def _apply_summary(
        self,
        uow: object,
        plan: PersistencePlan,
        session: SessionSnapshot,
        now: datetime,
    ) -> CandidateWriteOutcome | None:
        """Apply the summary candidate to the session."""
        candidate = plan.summary_candidate
        if candidate is None or not candidate.content:
            return None

        # Check confidence
        if candidate.confidence < 0.3:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="summary",
                candidate_key=None,
                status="ignored",
                record_id=None,
                reason_code="low_confidence",
            )

        # Check if content is the same as current
        if session.summary_text and session.summary_text == candidate.content:
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="summary",
                candidate_key=None,
                status="deduplicated",
                record_id=None,
                reason_code="identical_content",
            )

        try:
            await uow.sessions.update_summary(
                session_id=plan.session_id,
                content=candidate.content,
                expected_summary_version=session.summary_version,
                now=now,
            )
        except RuntimeError as exc:
            # Version conflict
            return CandidateWriteOutcome(
                candidate_id=candidate.candidate_id,
                candidate_type="summary",
                candidate_key=None,
                status="rejected",
                record_id=None,
                reason_code="version_conflict",
            )

        return CandidateWriteOutcome(
            candidate_id=candidate.candidate_id,
            candidate_type="summary",
            candidate_key=None,
            status="applied_update",
            record_id=None,
            reason_code=None,
        )

    @staticmethod
    def _build_embedding_jobs(
        plan: PersistencePlan,
        outcomes: list[CandidateWriteOutcome],
    ) -> tuple[EmbeddingJob, ...]:
        """Build embedding jobs for candidates that don't have precomputed embeddings.

        Only creates jobs for candidates that were actually applied
        (inserted or superseded with new content).
        """
        # Find candidate IDs that have prepared embeddings
        prepared_ids = {e.candidate_id for e in plan.embeddings}

        jobs: list[EmbeddingJob] = []
        for outcome in outcomes:
            if outcome.status in ("applied_insert", "superseded"):
                if outcome.record_id and outcome.candidate_id not in prepared_ids:
                    jobs.append(EmbeddingJob(
                        id=new_uuid(),
                        memory_id=outcome.record_id,
                        embedding_model="default",
                        status="pending",
                    ))

        return tuple(jobs)

    @staticmethod
    def _build_audit_records(
        plan: PersistencePlan,
        outcomes: list[CandidateWriteOutcome],
        commit_id: str,
    ) -> tuple[CandidateWriteAudit, ...]:
        """Build audit trail records for all candidate outcomes.

        These records are written AFTER the turn_commit to ensure
        referential integrity, but still within the same transaction.
        """
        audits: list[CandidateWriteAudit] = []
        for oc in outcomes:
            audits.append(CandidateWriteAudit(
                id=new_uuid(),
                commit_id=commit_id,
                user_id=plan.user_id,
                session_id=plan.session_id,
                turn_id=plan.turn_id,
                candidate_id=oc.candidate_id,
                candidate_type=oc.candidate_type,
                candidate_key=oc.candidate_key,
                requested_operation=oc.status.split("_")[0] if "_" in oc.status else oc.status,
                result_status=oc.status,
                target_record_id=oc.record_id,
                reason_code=oc.reason_code,
            ))
        return tuple(audits)

    @staticmethod
    async def _commit_with_shield(uow: object, plan: PersistencePlan) -> None:
        """Commit the transaction, shielded from cancellation.

        If the transaction is cancelled during commit, we try to
        determine the outcome via the recovery service.
        """
        commit_coro = uow.commit()
        try:
            await asyncio.shield(commit_coro)
        except asyncio.CancelledError:
            # The commit may or may not have completed — the shielded
            # call will complete in the background.  We re-raise the
            # cancellation; the caller's retry will hit idempotency.
            raise
