"""Approval aggregate backed exclusively by canonical Event streams."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_replay import ApprovalProjection, replay_approval
from cogito.store.event_store import EventStore, StreamVersionConflictError


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    turn_id: str
    request: dict[str, Any]
    expires_at: datetime


@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    status: str
    responder_id: str
    decided_at: datetime


class ApprovalService(Protocol):
    """The sole write boundary for an Approval aggregate."""

    def create(
        self, *, turn_id: str, request: dict[str, Any], ttl_seconds: int = 3600
    ) -> ApprovalRequest: ...

    def approve(
        self, approval_id: str, responder_id: str, *, expected_version: int | None = None,
        action_hash: str = "",
    ) -> ApprovalDecision: ...

    def reject(
        self, approval_id: str, responder_id: str, *, expected_version: int | None = None,
    ) -> ApprovalDecision: ...

    def expire(self, approval_id: str) -> bool: ...

    def cancel(self, approval_id: str) -> bool: ...

    def get(self, approval_id: str) -> dict[str, Any] | None: ...


class ApprovalStateError(ValueError):
    """An attempted approval transition is no longer valid."""


class ApprovalNotFoundError(KeyError):
    """The requested approval stream does not exist."""


class SqliteApprovalService:
    """Event-sourced Approval service; ``approvals`` is never consulted."""

    VALID_TERMINAL = {"approved", "rejected", "expired", "cancelled"}

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._events = EventStore(conn)

    def _state(self, approval_id: str) -> ApprovalProjection | None:
        return replay_approval(self._events.read_stream("approval", approval_id), approval_id)

    def _all_states(self) -> list[ApprovalProjection]:
        events = self._events.read_stream_type("approval")
        ids = {event.stream_id for event in events}
        return [state for approval_id in ids if (state := replay_approval(events, approval_id))]

    @staticmethod
    def _expires_at(value: int | None) -> datetime:
        return datetime.fromtimestamp((value or 0) / 1000, tz=UTC)

    @staticmethod
    def _is_expired(state: ApprovalProjection, now: datetime) -> bool:
        return state.expires_at is not None and state.expires_at <= int(now.timestamp() * 1000)

    @staticmethod
    def _request_from(state: ApprovalProjection) -> dict[str, Any]:
        """Reconstruct the safe execution envelope without raw tool arguments."""
        return {
            "kind": state.subject_type or "tool_call",
            "tool_call_id": state.subject_id,
            "tool_name": state.tool_name,
            "capability_id": state.capability_id,
            "tool_version": state.capability_version,
            "tool_schema_hash": state.tool_schema_hash,
            "arguments_snapshot_ref": state.arguments_snapshot_ref or "",
            "arguments_hash": state.action_hash,
            "turn_id": state.turn_id,
            "attempt_id": state.attempt_id,
            "policy_version": state.policy_version,
            "auto_mode_version": state.auto_mode_version,
            "risk_level": state.risk_level,
            "permissions": list(state.permissions),
            "constraints": state.constraints or {},
        }

    def _append_event(
        self,
        *,
        event_type: str,
        approval_id: str,
        state: ApprovalProjection | None,
        request: dict[str, Any] | None = None,
        responder_id: str = "",
        outcome: str = "",
        idempotency_key: str,
        occurred_at: datetime | None = None,
        expected_version: int | None = None,
        expires_at: datetime | None = None,
        allowed_responders: list[str] | None = None,
    ) -> Event:
        """Append one safe lifecycle fact against the replayed stream version."""
        request = request or self._request_from(state) if state else request or {}
        source = self._events.read_stream("approval", approval_id)
        previous = source[-1] if source else None
        at = occurred_at or datetime.now(UTC)
        attributes = {
            "subject_type": str(request.get("kind", "tool_call")),
            "subject_id": str(request.get("tool_call_id", "")),
            "tool_name": str(request.get("tool_name", "")),
            "capability_id": str(request.get("capability_id", "")),
            "capability_version": str(request.get("tool_version", "")),
            "tool_schema_hash": str(request.get("tool_schema_hash", "")),
            "action_hash": str(request.get("arguments_hash", "")),
            "policy_version": str(request.get("policy_version", "")),
            "auto_mode_version": str(request.get("auto_mode_version", "")),
            "risk_level": str(request.get("risk_level", "")),
            "permissions": [str(item) for item in request.get("permissions", [])],
            "constraints": dict(request.get("constraints", {})),
        }
        if expires_at is not None:
            attributes["expires_at"] = int(expires_at.timestamp() * 1000)
        elif state is not None and state.expires_at is not None:
            attributes["expires_at"] = state.expires_at
        if allowed_responders is not None:
            attributes["allowed_responder_principal_ids"] = sorted(set(allowed_responders))
        elif state is not None:
            attributes["allowed_responder_principal_ids"] = list(
                state.allowed_responder_principal_ids
            )
        return self._events.append(
            Event(
                event_type=event_type,
                stream_type="approval",
                stream_id=approval_id,
                producer="approval-service",
                event_class=(
                    EventClass.OPERATION if event_type == "approval.consumed" else EventClass.DOMAIN
                ),
                context=EventContext(
                    trace_id=(previous.context.trace_id if previous else "")
                    or (state.turn_id if state else ""),
                    correlation_id=(previous.context.correlation_id if previous else "")
                    or (state.turn_id if state else ""),
                    causation_id=previous.event_id if previous else "",
                    principal_id=responder_id or str(request.get("principal_id", "")),
                    turn_id=(state.turn_id if state else "") or str(request.get("turn_id", "")),
                    attempt_id=(state.attempt_id if state else "") or str(request.get("attempt_id", "")),
                ),
                summary=f"Approval {outcome or event_type.rsplit('.', 1)[-1]}",
                attributes=attributes,
                payload_ref=str(request.get("arguments_snapshot_ref", "")) or None,
                outcome=outcome,
                occurred_at=int(at.timestamp() * 1000),
                idempotency_key=idempotency_key,
            ),
            expected_version=expected_version,
        )

    def create(
        self, *, turn_id: str, request: dict[str, Any], ttl_seconds: int = 3600
    ) -> ApprovalRequest:
        now = datetime.now(UTC)
        approval_id = uuid4().hex
        expires_at = now + timedelta(seconds=ttl_seconds)
        allowed = sorted({"owner", str(request.get("principal_id", "owner"))})
        event_request = {**request, "turn_id": turn_id}
        self._append_event(
            event_type="approval.requested",
            approval_id=approval_id,
            state=None,
            request=event_request,
            outcome="pending",
            occurred_at=now,
            expires_at=expires_at,
            allowed_responders=allowed,
            expected_version=0,
            idempotency_key=f"approval:{approval_id}:requested",
        )
        self._conn.commit()
        return ApprovalRequest(approval_id, turn_id, dict(request), expires_at)

    def _transition(
        self, approval_id: str, responder_id: str, decision: str, *,
        expected_version: int | None = None, action_hash: str = "",
    ) -> ApprovalDecision:
        state = self._state(approval_id)
        if state is None:
            raise ApprovalNotFoundError(approval_id)
        now = datetime.now(UTC)
        if state.status != "pending":
            raise ApprovalStateError(f"approval {approval_id} already {state.status}")
        if self._is_expired(state, now):
            raise ApprovalStateError(f"approval {approval_id} expired")
        if expected_version is not None and state.stream_version != expected_version:
            raise ApprovalStateError("approval version conflict")
        if state.allowed_responder_principal_ids and responder_id not in state.allowed_responder_principal_ids:
            raise ApprovalStateError("responder principal is not allowed")
        if action_hash and state.action_hash != action_hash:
            raise ApprovalStateError("approval action hash mismatch")
        try:
            self._append_event(
                event_type="approval.responded",
                approval_id=approval_id,
                state=state,
                responder_id=responder_id,
                outcome=decision,
                occurred_at=now,
                expected_version=state.stream_version,
                idempotency_key=f"approval:{approval_id}:responded",
            )
        except StreamVersionConflictError as exc:
            raise ApprovalStateError("approval was concurrently decided") from exc
        self._conn.commit()
        return ApprovalDecision(approval_id, decision, responder_id, now)

    def approve(self, approval_id: str, responder_id: str, *, expected_version: int | None = None,
                action_hash: str = "") -> ApprovalDecision:
        return self._transition(approval_id, responder_id, "approved",
                                expected_version=expected_version, action_hash=action_hash)

    def reject(self, approval_id: str, responder_id: str, *, expected_version: int | None = None) -> ApprovalDecision:
        return self._transition(approval_id, responder_id, "rejected", expected_version=expected_version)

    def _pending_terminal(self, approval_id: str, event_type: str, outcome: str) -> bool:
        state = self._state(approval_id)
        if state is None or state.status != "pending":
            return False
        try:
            self._append_event(
                event_type=event_type, approval_id=approval_id, state=state, outcome=outcome,
                expected_version=state.stream_version,
                idempotency_key=f"approval:{approval_id}:{outcome}",
            )
        except StreamVersionConflictError:
            return False
        self._conn.commit()
        return True

    def expire(self, approval_id: str) -> bool:
        return self._pending_terminal(approval_id, "approval.expired", "expired")

    def cancel(self, approval_id: str) -> bool:
        return self._pending_terminal(approval_id, "approval.cancelled", "cancelled")

    def get(self, approval_id: str) -> dict[str, Any] | None:
        state = self._state(approval_id)
        if state is None:
            return None
        return {
            "approval_id": state.approval_id,
            "turn_id": state.turn_id,
            "status": state.status,
            "version": state.stream_version,
            "request": self._request_from(state),
            "expires_at": self._expires_at(state.expires_at).isoformat() if state.expires_at else "",
            "responder_id": state.responder_id,
            "consumed_at": bool(state.consumed),
            "action_hash": state.action_hash,
            "allowed_responder_principal_ids": list(state.allowed_responder_principal_ids),
        }

    def find_or_create_tool_approval(self, *, turn_id: str, request: dict[str, Any],
                                     ttl_seconds: int = 3600) -> ApprovalRequest:
        for state in self._all_states():
            existing = self._request_from(state)
            if (
                state.turn_id == turn_id and state.status == "pending"
                and not self._is_expired(state, datetime.now(UTC))
                and existing.get("kind") == "tool_call"
                and existing.get("capability_id") == request.get("capability_id")
                and existing.get("tool_version") == request.get("tool_version")
                and existing.get("tool_schema_hash") == request.get("tool_schema_hash")
                and existing.get("arguments_hash") == request.get("arguments_hash")
            ):
                return ApprovalRequest(state.approval_id, turn_id, existing, self._expires_at(state.expires_at))
        return self.create(turn_id=turn_id, request=request, ttl_seconds=ttl_seconds)

    def claim_approved_tool_call(self, turn_id: str) -> dict[str, Any] | None:
        candidates = sorted(
            (state for state in self._all_states() if state.turn_id == turn_id),
            key=lambda state: state.requested_at or 0,
        )
        for state in candidates:
            if state.status != "approved" or state.consumed or self._is_expired(state, datetime.now(UTC)):
                continue
            request = self._request_from(state)
            if request["kind"] != "tool_call":
                continue
            request["approval_id"] = state.approval_id
            request["approval_version"] = state.stream_version
            return request
        return None

    def consume_approved_tool_call(self, approval_id: str, expected_version: int) -> bool:
        state = self._state(approval_id)
        if (
            state is None or state.status != "approved" or state.consumed
            or state.stream_version != expected_version or self._is_expired(state, datetime.now(UTC))
        ):
            return False
        try:
            self._append_event(
                event_type="approval.consumed", approval_id=approval_id, state=state,
                outcome="consumed", expected_version=state.stream_version,
                idempotency_key=f"approval:{approval_id}:consumed",
            )
        except StreamVersionConflictError:
            return False
        self._conn.commit()
        return True

    def invalidate_approved_tool_call(self, approval_id: str, expected_version: int) -> bool:
        state = self._state(approval_id)
        if (
            state is None or state.status != "approved" or state.consumed
            or state.stream_version != expected_version
        ):
            return False
        try:
            self._append_event(
                event_type="approval.cancelled", approval_id=approval_id, state=state,
                outcome="cancelled", expected_version=state.stream_version,
                idempotency_key=f"approval:{approval_id}:cancelled",
            )
        except StreamVersionConflictError:
            return False
        self._conn.commit()
        return True
