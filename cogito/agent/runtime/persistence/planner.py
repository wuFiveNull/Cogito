# cogito/agent/runtime/persistence/planner.py
#
# PersistencePlanBuilder — builds an immutable PersistencePlan from
# a TurnContext.
#
# The plan is built ONCE per turn, before the retry loop starts.
# All IDs (commit_id, event IDs) are generated here and frozen.
# The plan is reused across retries so that the commit fingerprint
# remains stable.

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from cogito.agent.domain.memory import MemoryCandidate, SummaryCandidate
from cogito.agent.domain.preferences import PreferenceCandidate
from cogito.agent.domain.usage import PersistableToolRecord, UsageSummary
from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.embedding import EmbeddingPort, EmbeddingVector
from cogito.agent.ports.ids import IdGeneratorPort
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.persistence.fingerprint import PersistenceFingerprint
from cogito.agent.runtime.persistence.models import (
    CandidateWriteOutcome,
    EventDraft,
    PersistenceOutcome,
    PersistencePlan,
    PersistedEvent,
    PreparedEmbedding,
)
from cogito.agent.runtime.persistence.sanitizer import (
    PersistenceSanitizer,
    canonical_json,
)
from cogito.agent.runtime.models import TurnStatus


# ── Logical order constants ──────────────────────────────────────────

LOGICAL_ORDER_USER = 0
LOGICAL_ORDER_TOOL_REQUEST_BASE = 10
LOGICAL_ORDER_TOOL_RESULT_BASE = 11
LOGICAL_ORDER_STEP = 10  # gap between tool pairs
LOGICAL_ORDER_ASSISTANT = 10000


class PersistencePlanBuilder:
    """Builds a complete, immutable PersistencePlan from TurnContext.

    This is a pure-domain transformation: no SQL, no I/O (except the
    optional embedding prep, which is explicitly called out).

    Usage::

        plan = await builder.build(ctx, sanitized=sanitized, now=clock.now())
    """

    def __init__(
        self,
        *,
        id_generator: IdGeneratorPort,
        fingerprint: PersistenceFingerprint,
        sanitizer: PersistenceSanitizer,
        embedding_port: EmbeddingPort | None = None,
        embedding_model: str = "",
    ) -> None:
        self._id_generator = id_generator
        self._fingerprint = fingerprint
        self._sanitizer = sanitizer
        self._embedding_port = embedding_port
        self._embedding_model = embedding_model

    async def build(
        self,
        ctx: TurnContext,
        *,
        now: datetime,
    ) -> PersistencePlan:
        """Build the PersistencePlan from the turn context.

        This method does NOT touch the database.  Embedding preparation
        (which calls the EmbeddingPort, an external service) happens here
        so that it completes before the write transaction begins.
        """
        user_id = ctx.request.actor_id
        session_id = ctx.request.session_id
        request_id = ctx.request.request_id
        turn_id = ctx.turn_id or ""

        # 1. Determine which events to persist
        drafts: list[EventDraft] = []
        tool_drafts: list[EventDraft] = []

        # 1a. User message event
        user_event_id = self._id_generator.new_id()
        drafts.append(EventDraft(
            event_id=user_event_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            turn_id=turn_id,
            role="user",
            event_type="user_message",
            content=self._sanitizer.sanitize_content(ctx.request.text),
            content_json={},
            extraction_status="pending",
            logical_order=LOGICAL_ORDER_USER,
        ))

        # 1b. Tool events (from PersistableToolRecord if available,
        #     otherwise from ToolExecutionRecord)
        tool_records = getattr(ctx, "tool_records", [])
        for idx, rec in enumerate(tool_records):
            ordinal = (idx + 1) * LOGICAL_ORDER_STEP

            # Tool request event
            if hasattr(rec, "safe_arguments"):
                args = canonical_json(dict(rec.safe_arguments))
            else:
                args = "{}"

            req_event_id = self._id_generator.new_id()
            tool_drafts.append(EventDraft(
                event_id=req_event_id,
                user_id=user_id,
                session_id=session_id,
                request_id=request_id,
                turn_id=turn_id,
                role="tool",
                event_type="tool_request",
                content=f"调用工具: {rec.tool_name}",
                content_json={"tool_name": rec.tool_name, "arguments": args},
                extraction_status="ignored",
                logical_order=ordinal,
            ))

            # Tool result / error event
            res_event_id = self._id_generator.new_id()
            succeeded = rec.succeeded if hasattr(rec, "succeeded") else True
            if succeeded:
                safe_result = getattr(rec, "safe_result", None) or {}
                result_cleaned = self._sanitizer.sanitize_tool_result(safe_result)
                tool_drafts.append(EventDraft(
                    event_id=res_event_id,
                    user_id=user_id,
                    session_id=session_id,
                    request_id=request_id,
                    turn_id=turn_id,
                    role="tool",
                    event_type="tool_result",
                    content=f"工具 {rec.tool_name} 返回结果",
                    content_json=(
                        result_cleaned
                        if isinstance(result_cleaned, dict)
                        else {"storage_ref": str(result_cleaned)}
                    ),
                    extraction_status="ignored",
                    logical_order=ordinal + 1,
                ))
            else:
                err_code = getattr(rec, "error_code", "UNKNOWN")
                err_msg = self._sanitizer.build_safe_error_message(
                    getattr(rec, "safe_error_message", None)
                )
                tool_drafts.append(EventDraft(
                    event_id=res_event_id,
                    user_id=user_id,
                    session_id=session_id,
                    request_id=request_id,
                    turn_id=turn_id,
                    role="tool",
                    event_type="tool_error",
                    content=f"工具 {rec.tool_name} 错误: {err_msg or err_code}",
                    content_json={
                        "tool_name": rec.tool_name,
                        "error_code": err_code,
                        "message": err_msg,
                    },
                    extraction_status="ignored",
                    logical_order=ordinal + 1,
                ))

        drafts.extend(tool_drafts)

        # 1c. Assistant message event
        assistant_event_id = self._id_generator.new_id()
        output_text = ctx.output_text or ""
        drafts.append(EventDraft(
            event_id=assistant_event_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            turn_id=turn_id,
            role="assistant",
            event_type="assistant_message",
            content=self._sanitizer.sanitize_content(output_text),
            content_json={"usage": self._usage_to_json(ctx.usage)},
            extraction_status="pending",
            logical_order=LOGICAL_ORDER_ASSISTANT,
        ))

        # 2. Sort drafts by logical_order
        drafts.sort(key=lambda d: d.logical_order)

        # 3. Build candidate lists
        p_candidates = tuple(getattr(ctx, "preference_candidates", []))
        m_candidates = tuple(getattr(ctx, "memory_candidates", []))
        s_candidate = getattr(ctx, "summary_candidate", None)

        # 4. Compute commit fingerprint
        commit_id = self._id_generator.new_id()
        fp = self._fingerprint.compute(
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            normalised_user_text=self._sanitizer.sanitize_content(ctx.request.text),
            normalised_output_text=self._sanitizer.sanitize_content(output_text),
            ordered_tool_record_digests=self._tool_record_digests(tool_records),
            usage_digest=canonical_json(self._usage_to_json(ctx.usage)),
        )

        # 5. Prepare embeddings (outside transaction)
        prepared_embeddings = await self._prepare_embeddings(
            candidates=list(m_candidates) + list(p_candidates),
        )

        # 6. Session version from state (if loaded)
        session_state = getattr(ctx, "session", None)
        expected_session_version = getattr(session_state, "version", None) if session_state else None

        summary_state = getattr(ctx, "session_summary", None)
        expected_summary_version = getattr(summary_state, "version", None) if summary_state else None

        return PersistencePlan(
            commit_id=commit_id,
            turn_id=turn_id,
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            persistence_span_id=getattr(ctx, "current_span_id", None),
            expected_session_version=expected_session_version,
            expected_summary_version=expected_summary_version,
            events=tuple(drafts),
            preference_candidates=p_candidates,
            memory_candidates=m_candidates,
            summary_candidate=s_candidate,
            embeddings=prepared_embeddings,
            usage=ctx.usage,
            started_at=ctx.started_at or now,
            persistence_started_at=now,
            commit_fingerprint=fp,
        )

    # ── Internal helpers ─────────────────────────────────────────────

    async def _prepare_embeddings(
        self,
        candidates: list,
    ) -> tuple[PreparedEmbedding, ...]:
        """Compute embeddings outside the write transaction."""
        if not candidates or self._embedding_port is None:
            return ()

        # Filter candidates that need embeddings (INSERT/UPDATE operations)
        texts: list[str] = []
        candidate_ids: list[str] = []
        for c in candidates:
            op = getattr(c, "operation", "insert")
            if op in ("insert", "update"):
                texts.append(getattr(c, "content", ""))
                candidate_ids.append(getattr(c, "candidate_id", ""))

        if not texts:
            return ()

        try:
            vectors = await self._embedding_port.embed_many(tuple(texts))
        except Exception:
            # Embedding failure is non-fatal — create jobs instead
            return ()

        prepared: list[PreparedEmbedding] = []
        for cid, vector in zip(candidate_ids, vectors):
            if isinstance(vector, EmbeddingVector):
                blob = self._vector_to_blob(vector.values)
                prepared.append(PreparedEmbedding(
                    candidate_id=cid,
                    model=vector.model,
                    dimensions=vector.dimensions,
                    blob=blob,
                ))
            elif isinstance(vector, (list, tuple)):
                blob = self._vector_to_blob(tuple(vector))
                prepared.append(PreparedEmbedding(
                    candidate_id=cid,
                    model=self._embedding_model,
                    dimensions=len(vector),
                    blob=blob,
                ))
        return tuple(prepared)

    @staticmethod
    def _vector_to_blob(values: tuple[float, ...]) -> bytes:
        """Encode float32 vector as little-endian BLOB."""
        import struct
        return struct.pack(f"<{len(values)}f", *values)

    @staticmethod
    def _usage_to_json(usage: UsageSummary) -> dict[str, int]:
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "model_calls": usage.model_calls,
            "tool_calls": usage.tool_calls,
        }

    def _tool_record_digests(self, records: list) -> tuple[str, ...]:
        digests: list[str] = []
        for rec in records:
            tool_name = getattr(rec, "tool_name", "?")
            ordinal = getattr(rec, "ordinal", 0)
            args_json = canonical_json(dict(getattr(rec, "safe_arguments", {})))
            result = getattr(rec, "safe_result", None)
            result_json = canonical_json(dict(result)) if result else None
            err = getattr(rec, "error_code", None)
            digests.append(
                PersistenceFingerprint.tool_record_digest(
                    tool_name=tool_name,
                    ordinal=ordinal,
                    safe_arguments_json=args_json,
                    safe_result_json=result_json,
                    error_code=err,
                )
            )
        return tuple(digests)

    def assign_sequences(
        self,
        plan: PersistencePlan,
        base_seq_no: int,
    ) -> tuple[PersistedEvent, ...]:
        """Assign sequential numbers to events.

        Called inside the transaction and returns PersistedEvent objects
        with their seq_no filled in.
        """
        from datetime import timezone
        now = datetime.now(timezone.utc)
        persisted: list[PersistedEvent] = []
        for i, draft in enumerate(plan.events):
            seq_no = base_seq_no + i
            persisted.append(PersistedEvent(
                event_id=draft.event_id,
                user_id=draft.user_id,
                session_id=draft.session_id,
                request_id=draft.request_id,
                turn_id=draft.turn_id,
                seq_no=seq_no,
                role=draft.role,
                event_type=draft.event_type,
                content=draft.content,
                content_json=draft.content_json,
                extraction_status=draft.extraction_status,
                created_at=now,
            ))
        return tuple(persisted)
