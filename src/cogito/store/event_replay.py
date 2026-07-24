"""Pure, side-effect-free aggregate state reconstruction from canonical Events.

These reducers deliberately know only the public Event Catalog.  They provide
the replacement read model for the execution aggregates while legacy SQLite
tables are still kept as compatibility projections during the staged cutover.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from cogito.domain.event import Event, EventContext


@dataclass(frozen=True, slots=True)
class TaskProjection:
    task_id: str
    status: str = ""
    task_type: str = ""
    payload_ref: str | None = None
    priority: int | None = None
    origin: str = ""
    scheduled_at: int | None = None
    retry_policy: dict[str, Any] | None = None
    checkpoint_ref: str | None = None
    idempotency_key: str = ""
    lease_owner: str = ""
    lease_expires_at: int | None = None
    lease_version: int = 0
    result_ref: str | None = None
    created_at: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class TaskAttemptProjection:
    """One Task execution claim replayed from its lifecycle Event stream."""

    task_attempt_id: str
    task_id: str = ""
    attempt_no: int = 0
    status: str = ""
    lease_owner: str = ""
    lease_version: int = 0
    lease_expires_at: int | None = None
    checkpoint_ref: str | None = None
    started_at: int | None = None
    finished_at: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class TurnProjection:
    turn_id: str
    status: str = ""
    session_id: str = ""
    input_message_id: str = ""
    priority: int | None = None
    active_attempt_id: str = ""
    final_message_id: str = ""
    created_at: int | None = None
    completed_at: int | None = None
    cancel_requested_at: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class RunAttemptProjection:
    """One RunAttempt reconstructed solely from its lifecycle Event stream."""

    attempt_id: str
    turn_id: str = ""
    attempt_no: int = 0
    status: str = ""
    checkpoint_ref: str | None = None
    worker_id: str = ""
    lease_version: int = 0
    lease_expires_at: int | None = None
    started_at: int | None = None
    finished_at: int | None = None
    error_ref: str | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class MessageProjection:
    """Safe message metadata; message bodies remain in the guarded payload store."""

    message_id: str
    conversation_id: str = ""
    session_id: str = ""
    sender_principal_id: str = ""
    sender_endpoint_id: str = ""
    role: str = ""
    direction: str = ""
    reply_to_message_id: str = ""
    platform_message_id: str = ""
    receive_sequence: int = 0
    trust_label: str = ""
    raw_payload_ref: str | None = None
    part_descriptors: tuple[dict[str, Any], ...] = ()
    created_at: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class ConversationProjection:
    """Conversation identity and isolation policy replayed from its stream."""

    conversation_id: str
    conversation_endpoint_id: str = ""
    platform_conversation_id: str = ""
    conversation_endpoint_ref: str = ""
    conversation_type: str = "private"
    principal_scope: str = ""
    context_partition_policy: str = "isolated"
    status: str = "active"
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class SessionProjection:
    """Session boundary reconstructed without a mutable sessions row."""

    session_id: str
    conversation_id: str = ""
    context_partition_key: str = ""
    reset_generation: int = 0
    status: str = "active"
    created_at: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class PrincipalProjection:
    """Principal identity state reconstructed from its immutable stream."""

    principal_id: str
    principal_type: str = "owner"
    status: str = "active"
    created_at: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class EndpointProjection:
    """External endpoint identity reconstructed from its immutable stream."""

    endpoint_id: str
    channel_type: str = ""
    channel_instance_id: str = ""
    platform_account_id: str = ""
    principal_id: str = ""
    endpoint_ref: str = ""
    capabilities: tuple[str, ...] = ()
    status: str = "active"
    verified_at: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class DeliveryProjection:
    delivery_id: str
    status: str = ""
    attempt_id: str = ""
    turn_id: str = ""
    conversation_id: str = ""
    session_id: str = ""
    delivery_mode: str = ""
    platform_conversation_id: str = ""
    content_ref: str | None = None
    error_category: str = ""
    platform_message_id: str | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class ApprovalProjection:
    approval_id: str
    status: str = ""
    turn_id: str = ""
    attempt_id: str = ""
    responder_id: str = ""
    consumed: bool = False
    expires_at: int | None = None
    subject_type: str = ""
    subject_id: str = ""
    tool_name: str = ""
    capability_id: str = ""
    capability_version: str = ""
    tool_schema_hash: str = ""
    action_hash: str = ""
    policy_version: str = ""
    auto_mode_version: str = ""
    risk_level: str = ""
    permissions: tuple[str, ...] = ()
    constraints: dict[str, Any] | None = None
    allowed_responder_principal_ids: tuple[str, ...] = ()
    arguments_snapshot_ref: str | None = None
    requested_at: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class KnowledgeResourceProjection:
    resource_id: str
    status: str = ""
    document_id: str = ""
    segment_count: int | None = None
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class MemoryProjection:
    memory_id: str
    status: str = ""
    kind: str = ""
    principal_id: str = ""
    superseded_by: str = ""
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class ConnectorSourceProjection:
    source_item_id: str
    connector_id: str = ""
    item_status: str = ""
    payload_ref: str | None = None
    payload_hash: str = ""
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class ProactiveCandidateProjection:
    candidate_id: str
    status: str = ""
    principal_id: str = ""
    origin: str = ""
    action: str = ""
    decision_id: str = ""
    delivery_id: str = ""
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class ModelCallProjection:
    """Observable model-call state reconstructed from its Event stream."""

    model_call_id: str
    status: str = "pending"
    request_id: str = ""
    provider_id: str = ""
    model_id: str = ""
    request_hash: str = ""
    request_payload_ref: str | None = None
    response_payload_ref: str | None = None
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    error_category: str = ""
    retry_count: int = 0
    started_at: int | None = None
    completed_at: int | None = None
    context: EventContext = EventContext()
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class ToolCallProjection:
    """Observable tool-call state reconstructed from lifecycle Events."""

    tool_call_id: str
    attempt_id: str = ""
    attempt_type: str = "run"
    tool_name: str = ""
    tool_version: str = "1.0"
    arguments_ref: str = ""
    status: str = "pending"
    started_at: int | None = None
    completed_at: int | None = None
    result_ref: str = ""
    result_trust_label: str = "unverified"
    result_size_bytes: int = 0
    context: EventContext = EventContext()
    stream_version: int = 0


@dataclass(frozen=True, slots=True)
class SideEffectReceiptProjection:
    """External-effect receipt reconstructed without a mutable receipt row."""

    receipt_id: str
    capability_id: str = ""
    operation_id: str | None = None
    request_hash: str = ""
    side_effect_class: str = ""
    status: str = ""
    reconcile_status: str = "not_needed"
    raw_ref: str | None = None
    attempt_id: str = ""
    attempt_type: str = "run"
    created_at: int = 0
    resolved_at: int | None = None
    audit_id: str | None = None
    context: EventContext = EventContext()
    stream_version: int = 0


def replay_task(events: Iterable[Event], task_id: str) -> TaskProjection | None:
    """Rebuild a Task's observable state without touching a database."""
    state: TaskProjection | None = None
    for event in _stream(events, "task", task_id):
        attrs = event.attributes
        if event.event_type in {"task.created", "task.scheduled", "task.imported"}:
            state = TaskProjection(
                task_id=task_id,
                status=_outcome_or(
                    event,
                    "scheduled" if event.event_type == "task.scheduled" else "created",
                ),
                task_type=str(attrs.get("task_type", "")),
                payload_ref=event.payload_ref,
                priority=_optional_int(attrs.get("priority")),
                origin=str(attrs.get("origin", "")),
                scheduled_at=_optional_int(attrs.get("scheduled_at")),
                retry_policy=dict(attrs.get("retry_policy") or {}),
                checkpoint_ref=str(attrs.get("checkpoint_ref") or "") or None,
                idempotency_key=str(attrs.get("task_idempotency_key", "")),
                created_at=event.occurred_at,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "task.scheduled",
            "task.retry_scheduled",
        }:
            state = replace(
                state,
                status=_outcome_or(event, "scheduled"),
                scheduled_at=_optional_int(attrs.get("scheduled_at")),
                lease_owner="",
                lease_expires_at=None,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "task.leased",
            "task.lease_renewed",
        }:
            state = replace(
                state,
                status="running",
                lease_owner=str(attrs.get("worker_id", "")),
                lease_expires_at=_optional_int(attrs.get("lease_expires_at")),
                lease_version=int(attrs.get("lease_version") or state.lease_version),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "task.waiting_user",
            "task.waiting_external",
        }:
            state = replace(
                state,
                status=_outcome_or(event, event.event_type.rsplit(".", 1)[-1]),
                lease_owner="",
                lease_expires_at=None,
                checkpoint_ref=str(attrs.get("waiting_id") or "") or state.checkpoint_ref,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "task.completed",
            "task.failed",
            "task.cancelled",
        }:
            state = replace(
                state,
                status=_outcome_or(event, event.event_type.rsplit(".", 1)[-1]),
                lease_owner="",
                lease_expires_at=None,
                result_ref=(
                    event.payload_ref
                    if event.event_type == "task.completed"
                    else state.result_ref
                ),
                stream_version=event.stream_version,
            )
    return state


def replay_task_attempt(
    events: Iterable[Event], task_attempt_id: str
) -> TaskAttemptProjection | None:
    """Rebuild a TaskAttempt without reading ``task_attempts``."""
    state: TaskAttemptProjection | None = None
    for event in _stream(events, "task_attempt", task_attempt_id):
        attrs = event.attributes
        if event.event_type in {"task.attempt.started", "task.attempt.imported"}:
            state = TaskAttemptProjection(
                task_attempt_id=task_attempt_id,
                task_id=event.context.task_id or str(attrs.get("task_id", "")),
                attempt_no=int(attrs.get("attempt_no") or 0),
                status=_outcome_or(event, "running"),
                lease_owner=str(attrs.get("lease_owner", attrs.get("worker_id", "")) or ""),
                lease_version=int(attrs.get("lease_version") or 0),
                lease_expires_at=_optional_int(attrs.get("lease_expires_at")),
                checkpoint_ref=str(attrs.get("checkpoint_ref") or event.payload_ref or "") or None,
                started_at=event.occurred_at,
                finished_at=_optional_int(attrs.get("finished_at")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "task.attempt.completed",
            "task.attempt.failed",
            "task.attempt.cancelled",
            "task.attempt.abandoned",
        }:
            state = replace(
                state,
                status=_outcome_or(event, event.event_type.rsplit(".", 1)[-1]),
                finished_at=event.occurred_at,
                stream_version=event.stream_version,
            )
    return state


def replay_turn(events: Iterable[Event], turn_id: str) -> TurnProjection | None:
    """Rebuild a Turn lifecycle from its strict per-stream order."""
    state: TurnProjection | None = None
    for event in _stream(events, "turn", turn_id):
        attrs = event.attributes
        if event.event_type in {"runtime.turn.accepted", "runtime.turn.imported"} or (
            event.event_type == "runtime.turn.queued" and state is None
        ):
            state = TurnProjection(
                turn_id=turn_id,
                status=_outcome_or(event, event.event_type.rsplit(".", 1)[-1]),
                session_id=event.context.session_id,
                input_message_id=str(attrs.get("input_message_id", "")),
                priority=_optional_int(attrs.get("priority")),
                created_at=event.occurred_at,
                active_attempt_id=str(attrs.get("active_attempt_id", "")),
                final_message_id=str(attrs.get("final_message_id", "")),
                completed_at=_optional_int(attrs.get("completed_at")),
                cancel_requested_at=_optional_int(attrs.get("cancel_requested_at")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "runtime.turn.queued",
            "runtime.turn.started",
            "runtime.turn.waiting_user",
            "runtime.turn.waiting_external",
            "runtime.turn.completed",
            "runtime.turn.failed",
            "runtime.turn.cancelled",
        }:
            status_by_event = {
                "runtime.turn.started": "running",
                "runtime.turn.waiting_user": "waiting_user",
                "runtime.turn.waiting_external": "waiting_external",
            }
            state = replace(
                state,
                status=_outcome_or(
                    event,
                    status_by_event.get(event.event_type, event.event_type.rsplit(".", 1)[-1]),
                ),
                session_id=event.context.session_id or state.session_id,
                active_attempt_id=(
                    str(attrs.get("active_attempt_id", event.context.attempt_id))
                    if event.event_type == "runtime.turn.started"
                    else ""
                ),
                final_message_id=(
                    str(attrs.get("final_message_id", state.final_message_id))
                    if event.event_type == "runtime.turn.completed"
                    else state.final_message_id
                ),
                completed_at=(
                    event.occurred_at
                    if event.event_type in {
                        "runtime.turn.completed",
                        "runtime.turn.failed",
                        "runtime.turn.cancelled",
                    }
                    else state.completed_at
                ),
                cancel_requested_at=(
                    event.occurred_at
                    if event.event_type == "runtime.turn.cancelled"
                    else state.cancel_requested_at
                ),
                stream_version=event.stream_version,
            )
        # Operational observations (for example context assembly) share the
        # aggregate stream to preserve causality, even when they do not alter
        # the Turn's business status.  The replayed version must nevertheless
        # advance so optimistic concurrency compares against the real stream.
        if state is not None and state.stream_version != event.stream_version:
            state = replace(state, stream_version=event.stream_version)
    return state


def replay_run_attempt(
    events: Iterable[Event], attempt_id: str
) -> RunAttemptProjection | None:
    """Rebuild RunAttempt lease and terminal state without a ``run_attempts`` row."""
    state: RunAttemptProjection | None = None
    for event in _stream(events, "run_attempt", attempt_id):
        attrs = event.attributes
        if event.event_type in {"runtime.attempt.started", "runtime.attempt.imported"}:
            state = RunAttemptProjection(
                attempt_id=attempt_id,
                turn_id=event.context.turn_id,
                attempt_no=_optional_int(attrs.get("attempt_no")) or 1,
                status=_outcome_or(event, "running"),
                checkpoint_ref=str(attrs.get("checkpoint_ref") or event.payload_ref or "") or None,
                worker_id=str(attrs.get("worker_id", "")),
                lease_version=_optional_int(attrs.get("lease_version")) or 1,
                lease_expires_at=_optional_int(attrs.get("lease_expires_at")),
                started_at=event.occurred_at,
                finished_at=_optional_int(attrs.get("finished_at")),
                error_ref=str(attrs.get("error_ref") or "") or None,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "runtime.attempt.lease_renewed":
            state = replace(
                state,
                lease_version=_optional_int(attrs.get("lease_version")) or state.lease_version,
                lease_expires_at=_optional_int(attrs.get("lease_expires_at")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "runtime.attempt.completed",
            "runtime.attempt.failed",
            "runtime.attempt.cancelled",
            "runtime.attempt.abandoned",
        }:
            state = replace(
                state,
                status=_outcome_or(event, event.event_type.rsplit(".", 1)[-1]),
                lease_expires_at=None,
                finished_at=event.occurred_at,
                error_ref=event.payload_ref or state.error_ref,
                stream_version=event.stream_version,
            )
    return state


def replay_message(events: Iterable[Event], message_id: str) -> MessageProjection | None:
    """Rebuild message metadata while never materialising its body into Event data."""
    state: MessageProjection | None = None
    for event in _stream(events, "message", message_id):
        if event.event_type not in {"interaction.message.recorded", "interaction.message.imported"}:
            continue
        attrs = event.attributes
        descriptors = attrs.get("part_descriptors", [])
        state = MessageProjection(
            message_id=message_id,
            conversation_id=event.context.conversation_id,
            session_id=event.context.session_id,
            sender_principal_id=event.context.principal_id,
            sender_endpoint_id=str(attrs.get("sender_endpoint_id", "")),
            role=str(attrs.get("role", "")),
            direction=str(attrs.get("direction", "")),
            reply_to_message_id=str(attrs.get("reply_to_message_id", "")),
            platform_message_id=str(attrs.get("platform_message_id", "")),
            receive_sequence=_optional_int(attrs.get("receive_sequence")) or 0,
            trust_label=str(attrs.get("trust_label", "")),
            raw_payload_ref=event.payload_ref,
            part_descriptors=tuple(
                dict(item) for item in descriptors if isinstance(item, dict)
            ) if isinstance(descriptors, list) else (),
            created_at=event.occurred_at,
            stream_version=event.stream_version,
        )
    return state


def replay_conversation(
    events: Iterable[Event], conversation_id: str
) -> ConversationProjection | None:
    """Rebuild conversation identity and context isolation policy from Events."""
    state: ConversationProjection | None = None
    for event in _stream(events, "conversation", conversation_id):
        if event.event_type not in {"interaction.conversation.created", "interaction.conversation.imported"}:
            continue
        attrs = event.attributes
        state = ConversationProjection(
            conversation_id=conversation_id,
            conversation_endpoint_id=str(attrs.get("conversation_endpoint_id", "")),
            platform_conversation_id=str(attrs.get("platform_conversation_id", "")),
            conversation_endpoint_ref=str(attrs.get("conversation_endpoint_ref", "")),
            conversation_type=str(attrs.get("conversation_type", "private")),
            principal_scope=str(attrs.get("principal_scope", "")),
            context_partition_policy=str(attrs.get("context_partition_policy", "isolated")),
            status=_outcome_or(event, "active"),
            stream_version=event.stream_version,
        )
    return state


def replay_session(events: Iterable[Event], session_id: str) -> SessionProjection | None:
    """Rebuild active/closed session boundaries without a ``sessions`` row."""
    state: SessionProjection | None = None
    for event in _stream(events, "session", session_id):
        if event.event_type in {"interaction.session.created", "interaction.session.imported"}:
            attrs = event.attributes
            state = SessionProjection(
                session_id=session_id,
                conversation_id=event.context.conversation_id,
                context_partition_key=str(attrs.get("context_partition_key", "")),
                reset_generation=_optional_int(attrs.get("reset_generation")) or 0,
                status=_outcome_or(event, "active"),
                created_at=event.occurred_at,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "runtime.session.completed":
            state = replace(state, status="closed", stream_version=event.stream_version)
    return state


def replay_principal(events: Iterable[Event], principal_id: str) -> PrincipalProjection | None:
    """Rebuild the safe Principal identity metadata from its Event stream."""
    state: PrincipalProjection | None = None
    for event in _stream(events, "principal", principal_id):
        if event.event_type not in {"interaction.principal.created", "interaction.principal.imported"}:
            continue
        state = PrincipalProjection(
            principal_id=principal_id,
            principal_type=str(event.attributes.get("principal_type", "owner")),
            status=_outcome_or(event, "active"),
            created_at=event.occurred_at,
            stream_version=event.stream_version,
        )
    return state


def replay_endpoint(events: Iterable[Event], endpoint_id: str) -> EndpointProjection | None:
    """Rebuild Endpoint routing identity without an ``endpoints`` projection."""
    state: EndpointProjection | None = None
    for event in _stream(events, "endpoint", endpoint_id):
        if event.event_type not in {"interaction.endpoint.created", "interaction.endpoint.imported"}:
            continue
        attrs = event.attributes
        capabilities = attrs.get("capabilities", [])
        state = EndpointProjection(
            endpoint_id=endpoint_id,
            channel_type=str(attrs.get("channel_type", "")),
            channel_instance_id=str(attrs.get("channel_instance_id", "")),
            platform_account_id=str(attrs.get("platform_account_id", "")),
            principal_id=event.context.principal_id,
            endpoint_ref=str(attrs.get("endpoint_ref", "")),
            capabilities=tuple(str(value) for value in capabilities if isinstance(value, str))
            if isinstance(capabilities, list | tuple)
            else (),
            status=_outcome_or(event, "active"),
            verified_at=_optional_int(attrs.get("verified_at")),
            stream_version=event.stream_version,
        )
    return state


def replay_delivery(events: Iterable[Event], delivery_id: str) -> DeliveryProjection | None:
    """Rebuild Delivery recovery state; edit-progress Events are intentionally absent."""
    state: DeliveryProjection | None = None
    for event in _stream(events, "delivery", delivery_id):
        if event.event_type in {"delivery.requested", "delivery.imported"}:
            state = DeliveryProjection(
                delivery_id=delivery_id,
                status=_outcome_or(event, "pending"),
                attempt_id=event.context.attempt_id,
                turn_id=event.context.turn_id,
                conversation_id=event.context.conversation_id,
                session_id=event.context.session_id,
                delivery_mode=str(event.attributes.get("delivery_mode", "")),
                platform_conversation_id=str(
                    event.attributes.get("platform_conversation_id", "")
                ),
                content_ref=event.payload_ref,
                error_category=str(event.attributes.get("error_category", "")),
                platform_message_id=(
                    str(event.attributes.get("platform_message_id", "")) or None
                ),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "delivery.started",
            "delivery.completed",
            "delivery.failed",
            "delivery.unknown",
            "delivery.cancelled",
            "delivery.retry_scheduled",
            "delivery.retry_requested",
        }:
            status_by_event = {
                "delivery.started": "sending",
                "delivery.retry_requested": "pending",
            }
            state = replace(
                state,
                status=_outcome_or(
                    event,
                    status_by_event.get(event.event_type, event.event_type.rsplit(".", 1)[-1]),
                ),
                attempt_id=event.context.attempt_id or state.attempt_id,
                error_category=event.error_category or state.error_category,
                platform_message_id=(
                    str(event.attributes["platform_message_id"])
                    if event.attributes.get("platform_message_id")
                    else state.platform_message_id
                ),
                stream_version=event.stream_version,
            )
    return state


def replay_approval(events: Iterable[Event], approval_id: str) -> ApprovalProjection | None:
    """Rebuild the approval state needed for recovery and safe tool resumption."""
    state: ApprovalProjection | None = None
    for event in _stream(events, "approval", approval_id):
        if event.event_type in {"approval.requested", "approval.imported"}:
            allowed = event.attributes.get("allowed_responder_principal_ids", [])
            permissions = event.attributes.get("permissions", [])
            state = ApprovalProjection(
                approval_id=approval_id,
                status=_outcome_or(event, "pending"),
                turn_id=event.context.turn_id,
                attempt_id=event.context.attempt_id,
                expires_at=_optional_int(event.attributes.get("expires_at")),
                subject_type=str(event.attributes.get("subject_type", "")),
                subject_id=str(event.attributes.get("subject_id", "")),
                tool_name=str(event.attributes.get("tool_name", "")),
                capability_id=str(event.attributes.get("capability_id", "")),
                capability_version=str(event.attributes.get("capability_version", "")),
                tool_schema_hash=str(event.attributes.get("tool_schema_hash", "")),
                action_hash=str(event.attributes.get("action_hash", "")),
                policy_version=str(event.attributes.get("policy_version", "")),
                auto_mode_version=str(event.attributes.get("auto_mode_version", "")),
                risk_level=str(event.attributes.get("risk_level", "")),
                permissions=tuple(str(item) for item in permissions if isinstance(item, str))
                if isinstance(permissions, list | tuple)
                else (),
                constraints=(
                    dict(event.attributes["constraints"])
                    if isinstance(event.attributes.get("constraints"), dict)
                    else None
                ),
                allowed_responder_principal_ids=(
                    tuple(str(item) for item in allowed if isinstance(item, str))
                    if isinstance(allowed, list | tuple)
                    else ()
                ),
                arguments_snapshot_ref=event.payload_ref,
                requested_at=event.occurred_at,
                responder_id=str(event.attributes.get("responder_id", "")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "approval.responded":
            state = replace(
                state,
                status=event.outcome,
                responder_id=event.context.principal_id,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {"approval.expired", "approval.cancelled"}:
            state = replace(
                state,
                status=_outcome_or(event, event.event_type.rsplit(".", 1)[-1]),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "approval.consumed":
            state = replace(state, consumed=True, stream_version=event.stream_version)
    return state


def replay_knowledge_resource(
    events: Iterable[Event], resource_id: str
) -> KnowledgeResourceProjection | None:
    """Rebuild a resource's ingestion/invalidated/deleted lifecycle."""
    state: KnowledgeResourceProjection | None = None
    for event in _stream(events, "knowledge_resource", resource_id):
        attrs = event.attributes
        if event.event_type in {
            "knowledge.resource.created",
            "knowledge.resource.updated",
            "knowledge.resource.imported",
        }:
            state = KnowledgeResourceProjection(
                resource_id=resource_id,
                status=_outcome_or(event, "queued"),
                document_id=str(attrs.get("document_id", "")),
                segment_count=_optional_int(attrs.get("segment_count")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "knowledge.document.parsed":
            state = replace(
                state,
                document_id=str(attrs.get("document_id", "")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "knowledge.resource.ingested":
            state = replace(
                state,
                status="active",
                document_id=str(attrs.get("document_id", "")) or state.document_id,
                segment_count=_optional_int(attrs.get("segment_count")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "knowledge.resource.invalidated":
            state = replace(state, status="stale", stream_version=event.stream_version)
        elif state is not None and event.event_type == "knowledge.resource.deleted":
            state = replace(state, status="deleted", stream_version=event.stream_version)
    return state


def replay_memory(events: Iterable[Event], memory_id: str) -> MemoryProjection | None:
    """Rebuild Memory lifecycle state without retaining memory text in Events."""
    state: MemoryProjection | None = None
    for event in _stream(events, "memory", memory_id):
        attrs = event.attributes
        if event.event_type in {"memory.candidate.created", "memory.imported"} or (
            event.event_type == "memory.confirmed" and state is None
        ):
            state = MemoryProjection(
                memory_id=memory_id,
                status=_outcome_or(
                    event,
                    "confirmed" if event.event_type == "memory.confirmed" else "candidate",
                ),
                kind=str(attrs.get("kind", "")),
                principal_id=str(attrs.get("principal_id", "")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "memory.confirmed":
            state = replace(
                state,
                status=_outcome_or(event, "confirmed"),
                kind=str(attrs.get("kind", "")) or state.kind,
                principal_id=str(attrs.get("principal_id", "")) or state.principal_id,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "memory.rejected",
            "memory.expired",
            "memory.erased",
        }:
            state = replace(
                state,
                status=_outcome_or(event, event.event_type.rsplit(".", 1)[-1]),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "memory.superseded":
            state = replace(
                state,
                status="superseded",
                superseded_by=str(attrs.get("superseded_by", "")),
                stream_version=event.stream_version,
            )
        elif state is not None:
            state = replace(state, stream_version=event.stream_version)
    return state


@dataclass(frozen=True, slots=True)
class ConnectorProjection:
    connector_id: str
    connector_type: str = ""
    name: str = ""
    url: str = ""
    status: str = ""
    stream_version: int = 0


def replay_connector(events: Iterable[Event], connector_id: str) -> ConnectorProjection | None:
    """Rebuild a Connector's configuration from its Event stream."""
    state: ConnectorProjection | None = None
    for event in _stream(events, "connector", connector_id):
        attrs = event.attributes
        if event.event_type in {"connector.created", "connector.imported"}:
            state = ConnectorProjection(
                connector_id=connector_id,
                connector_type=str(attrs.get("connector_type", "")),
                name=str(attrs.get("name", "")),
                url=str(attrs.get("url", "")),
                status=event.outcome or "active",
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "connector.status.updated", "connector.cursor.updated",
        }:
            new_status = event.outcome or attrs.get("status", "")
            if new_status:
                state = ConnectorProjection(
                    connector_id=state.connector_id,
                    connector_type=state.connector_type,
                    name=state.name,
                    url=state.url,
                    status=new_status,
                    stream_version=event.stream_version,
                )
            else:
                state = ConnectorProjection(
                    connector_id=state.connector_id,
                    connector_type=state.connector_type,
                    name=state.name,
                    url=state.url,
                    status=state.status,
                    stream_version=event.stream_version,
                )
    return state


def replay_connector_source(
    events: Iterable[Event], source_item_id: str
) -> ConnectorSourceProjection | None:
    """Rebuild a connector source acceptance fact from its ingestion Event."""
    state: ConnectorSourceProjection | None = None
    for event in _stream(events, "source", source_item_id):
        if event.event_type in {"connector.source.ingested", "connector.source.imported"}:
            state = ConnectorSourceProjection(
                source_item_id=source_item_id,
                connector_id=str(event.attributes.get("connector_id", "")),
                item_status=str(event.attributes.get("item_status", event.outcome)),
                payload_ref=event.payload_ref,
                payload_hash=event.payload_hash,
                stream_version=event.stream_version,
            )
    return state


def replay_proactive_candidate(
    events: Iterable[Event], candidate_id: str
) -> ProactiveCandidateProjection | None:
    """Rebuild a candidate and its latest decision from one Event stream."""
    state: ProactiveCandidateProjection | None = None
    for event in _stream(events, "proactive_candidate", candidate_id):
        if event.event_type in {"proactive.candidate.created", "proactive.candidate.imported"}:
            state = ProactiveCandidateProjection(
                candidate_id=candidate_id,
                status=_outcome_or(event, "evaluating"),
                principal_id=event.context.principal_id,
                origin=str(event.attributes.get("origin", "")),
                action=str(event.attributes.get("recommended_action", "")),
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "proactive.decision.made":
            state = replace(
                state,
                status="decided",
                action=_outcome_or(event, str(event.attributes.get("action", ""))),
                decision_id=str(event.attributes.get("decision_id", "")),
                delivery_id=str(event.attributes.get("delivery_id", "")),
                stream_version=event.stream_version,
            )
    return state


def replay_model_call(events: Iterable[Event], model_call_id: str) -> ModelCallProjection | None:
    """Rebuild one provider-call observation from lifecycle Events only."""
    state: ModelCallProjection | None = None
    for event in _stream(events, "model_call", model_call_id):
        attrs = event.attributes
        if event.event_type in {"model.call.started", "model.call.imported"}:
            state = ModelCallProjection(
                model_call_id=model_call_id,
                request_id=str(attrs.get("request_id", "")),
                provider_id=str(attrs.get("provider_id", "")),
                model_id=str(attrs.get("model_id", "")),
                request_hash=str(attrs.get("request_hash", event.payload_hash)),
                request_payload_ref=str(attrs.get("request_payload_ref") or event.payload_ref or "") or None,
                started_at=event.occurred_at,
                response_payload_ref=str(attrs.get("response_payload_ref") or "") or None,
                finish_reason=str(attrs.get("finish_reason", "")) or None,
                input_tokens=_optional_int(attrs.get("input_tokens")) or 0,
                output_tokens=_optional_int(attrs.get("output_tokens")) or 0,
                cached_tokens=_optional_int(attrs.get("cached_tokens")) or 0,
                latency_ms=_optional_int(attrs.get("latency_ms")) or 0,
                retry_count=_optional_int(attrs.get("retry_count")) or 0,
                error_category=str(attrs.get("error_category", "")),
                completed_at=_optional_int(attrs.get("completed_at")),
                status=_outcome_or(event, "pending"),
                context=event.context,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "model.call.completed",
            "model.call.failed",
            "model.call.cancelled",
        }:
            fallback_status = {
                "model.call.completed": "success",
                "model.call.failed": "failed",
                "model.call.cancelled": "cancelled",
            }[event.event_type]
            state = replace(
                state,
                status=_outcome_or(event, fallback_status),
                request_id=str(attrs.get("request_id", "")) or state.request_id,
                provider_id=str(attrs.get("provider_id", "")) or state.provider_id,
                model_id=str(attrs.get("model_id", "")) or state.model_id,
                response_payload_ref=event.payload_ref,
                finish_reason=str(attrs.get("finish_reason", "")) or None,
                input_tokens=_optional_int(attrs.get("input_tokens")) or 0,
                output_tokens=_optional_int(attrs.get("output_tokens")) or 0,
                cached_tokens=_optional_int(attrs.get("cached_tokens")) or 0,
                latency_ms=_optional_int(attrs.get("latency_ms")) or 0,
                retry_count=_optional_int(attrs.get("retry_count")) or 0,
                error_category=event.error_category,
                completed_at=event.occurred_at,
                context=event.context,
                stream_version=event.stream_version,
            )
    return state


def replay_tool_call(events: Iterable[Event], tool_call_id: str) -> ToolCallProjection | None:
    """Rebuild a tool lifecycle without retaining arguments or result content."""
    state: ToolCallProjection | None = None
    for event in _stream(events, "tool_call", tool_call_id):
        attrs = event.attributes
        if event.event_type in {"tool.call.requested", "tool.call.imported"}:
            state = ToolCallProjection(
                tool_call_id=tool_call_id,
                attempt_id=event.context.attempt_id,
                attempt_type=str(attrs.get("attempt_type", "run")),
                tool_name=str(attrs.get("tool_name", "")),
                tool_version=str(attrs.get("tool_version", "1.0")),
                arguments_ref=str(attrs.get("arguments_ref") or event.payload_ref or ""),
                status=_outcome_or(event, "pending"),
                started_at=event.occurred_at,
                completed_at=_optional_int(attrs.get("completed_at")),
                result_ref=str(attrs.get("result_ref", "")),
                result_trust_label=str(attrs.get("result_trust_label", "unverified")),
                result_size_bytes=_optional_int(attrs.get("result_size_bytes")) or 0,
                context=event.context,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type in {
            "tool.call.approval_required",
            "tool.call.started",
            "tool.call.completed",
            "tool.call.failed",
            "tool.call.cancelled",
            "tool.call.unknown",
        }:
            fallback_status = {
                "tool.call.approval_required": "approved",
                "tool.call.started": "executing",
                "tool.call.completed": "succeeded",
                "tool.call.failed": "failed",
                "tool.call.cancelled": "cancelled",
                "tool.call.unknown": "unknown",
            }[event.event_type]
            is_terminal = event.event_type in {
                "tool.call.completed",
                "tool.call.failed",
                "tool.call.cancelled",
                "tool.call.unknown",
            }
            state = replace(
                state,
                attempt_id=event.context.attempt_id or state.attempt_id,
                attempt_type=str(attrs.get("attempt_type", "")) or state.attempt_type,
                tool_name=str(attrs.get("tool_name", "")) or state.tool_name,
                tool_version=str(attrs.get("tool_version", "")) or state.tool_version,
                status=_outcome_or(event, fallback_status),
                started_at=(event.occurred_at if event.event_type == "tool.call.started" else state.started_at),
                completed_at=event.occurred_at if is_terminal else state.completed_at,
                result_ref=(event.payload_ref or state.result_ref) if is_terminal else state.result_ref,
                result_trust_label=(
                    str(attrs.get("result_trust_label", "")) or state.result_trust_label
                ),
                result_size_bytes=_optional_int(attrs.get("result_size_bytes")) or 0,
                context=event.context,
                stream_version=event.stream_version,
            )
    return state


def replay_side_effect_receipt(
    events: Iterable[Event], receipt_id: str
) -> SideEffectReceiptProjection | None:
    """Rebuild a receipt and reconciliation state from immutable facts."""
    state: SideEffectReceiptProjection | None = None
    for event in _stream(events, "side_effect_receipt", receipt_id):
        attrs = event.attributes
        if event.event_type in {"side_effect.receipt.recorded", "side_effect.receipt.imported"}:
            state = SideEffectReceiptProjection(
                receipt_id=receipt_id,
                capability_id=str(attrs.get("capability_id", "")),
                operation_id=str(attrs.get("operation_id", "")) or None,
                request_hash=str(attrs.get("request_hash", "")),
                side_effect_class=str(attrs.get("side_effect_class", "")),
                status=_outcome_or(event, "recorded"),
                reconcile_status=str(attrs.get("reconcile_status", "not_needed")),
                raw_ref=event.payload_ref,
                attempt_id=event.context.attempt_id,
                attempt_type=str(attrs.get("attempt_type", "run")),
                created_at=event.occurred_at,
                audit_id=str(attrs.get("audit_id", "")) or None,
                context=event.context,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "side_effect.receipt.resolved":
            state = replace(
                state,
                status=_outcome_or(event, state.status),
                resolved_at=event.occurred_at,
                context=event.context,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "side_effect.receipt.reconciled":
            state = replace(
                state,
                reconcile_status=_outcome_or(event, state.reconcile_status),
                context=event.context,
                stream_version=event.stream_version,
            )
    return state


@dataclass(frozen=True, slots=True)
class ScheduleProjection:
    """Schedule configuration reconstructed from its Event stream."""

    schedule_id: str
    schedule_type: str = ""
    expression: str = ""
    timezone: str = ""
    misfire_policy: str = ""
    max_catch_up: int | None = None
    enabled: bool = False
    connector_id: str = ""
    task_type: str = ""
    task_payload: str = ""
    next_fire_at: int | None = None
    last_fire_at: int | None = None
    normalized_interval_s: int | None = None
    version: int = 0
    created_at: int | None = None
    stream_version: int = 0


def replay_schedule(events: Iterable[Event], schedule_id: str) -> ScheduleProjection | None:
    """Rebuild a Schedule's current configuration from its Event stream."""
    state: ScheduleProjection | None = None
    for event in _stream(events, "schedule", schedule_id):
        attrs = event.attributes
        if event.event_type == "schedule.created":
            state = ScheduleProjection(
                schedule_id=schedule_id,
                schedule_type=str(attrs.get("schedule_type", "")),
                expression=str(attrs.get("expression", "")),
                timezone=str(attrs.get("timezone", "")),
                misfire_policy=str(attrs.get("misfire_policy", "")),
                max_catch_up=_optional_int(attrs.get("max_catch_up")),
                enabled=bool(attrs.get("enabled", False)),
                connector_id=str(attrs.get("connector_id", "")),
                task_type=str(attrs.get("task_type", "")),
                task_payload=str(attrs.get("task_payload", "")),
                next_fire_at=_optional_int(attrs.get("next_fire_at")),
                last_fire_at=_optional_int(attrs.get("last_fire_at")),
                normalized_interval_s=_optional_int(attrs.get("normalized_interval_s")),
                created_at=event.occurred_at,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "schedule.fired":
            state = replace(
                state,
                next_fire_at=_optional_int(attrs.get("next_fire_at")) or state.next_fire_at,
                last_fire_at=_optional_int(attrs.get("last_fire_at")) or state.last_fire_at,
                version=state.version + 1,
                stream_version=event.stream_version,
            )
        elif state is not None and event.event_type == "schedule.enabled_toggled":
            state = replace(
                state,
                enabled=bool(attrs.get("enabled", not state.enabled)),
                version=state.version + 1,
                stream_version=event.stream_version,
            )
        elif state is not None and state.stream_version != event.stream_version:
            state = replace(state, stream_version=event.stream_version)
    return state


def _stream(events: Iterable[Event], stream_type: str, stream_id: str) -> list[Event]:
    return sorted(
        (
            event
            for event in events
            if event.stream_type == stream_type and event.stream_id == stream_id
        ),
        key=lambda event: event.stream_version,
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float | str) and str(value) else None


def _outcome_or(event: Event, fallback: str) -> str:
    """Legacy bridge uses ``recorded`` as a transport outcome, not aggregate state."""
    return event.outcome if event.outcome and event.outcome != "recorded" else fallback
