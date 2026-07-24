"""Read-only, non-persistent views reconstructed from Event streams."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any, TypeVar

from cogito.domain.event import Event
from cogito.store.event_replay import (
    replay_connector_source,
    replay_conversation,
    replay_delivery,
    replay_endpoint,
    replay_message,
    replay_memory,
    replay_principal,
    replay_task_attempt,
    replay_run_attempt,
    replay_session,
    replay_task,
    replay_turn,
)
from cogito.store.event_store import EventStore

T = TypeVar("T")


class EventProjectionStore:
    """On-demand read models for aggregates whose Events are self-sufficient.

    This class must never write a projection table.  The temporary legacy
    tables remain only as a compatibility fallback until their every reader is
    migrated and historical payload references are backfilled.
    """

    def __init__(self, event_store: EventStore) -> None:
        self._events = event_store

    def turns(self, *, status: str | None = None) -> list[dict[str, Any]]:
        projections = self._replay("turn", replay_turn)
        result = [
            {
                "turn_id": value.turn_id,
                "session_id": value.session_id,
                "input_message_id": value.input_message_id,
                "status": value.status,
                "priority": value.priority or 0,
                "version": value.stream_version,
                "created_at": created_at,
            }
            for value, created_at in projections
            if status is None or value.status == status
        ]
        return sorted(result, key=lambda item: item["created_at"], reverse=True)

    def tasks(self, *, status: str | None = None) -> list[dict[str, Any]]:
        projections = self._replay("task", replay_task)
        result = [
            {
                "task_id": value.task_id,
                "task_type": value.task_type,
                "status": value.status,
                "priority": value.priority or 0,
                "lease_owner": value.lease_owner or None,
                "lease_expires_at": value.lease_expires_at,
                "result_ref": value.result_ref,
                "version": value.stream_version,
                "created_at": created_at,
            }
            for value, created_at in projections
            if status is None or value.status == status
        ]
        return sorted(result, key=lambda item: item["created_at"], reverse=True)

    def deliveries(self, *, status: str | None = None) -> list[dict[str, Any]]:
        projections = self._replay("delivery", replay_delivery)
        result = [
            {
                "delivery_id": value.delivery_id,
                "status": value.status,
                "content_ref": value.content_ref,
                "attempt_id": value.attempt_id or None,
                "turn_id": value.turn_id or None,
                "conversation_id": value.conversation_id or None,
                "session_id": value.session_id or None,
                "delivery_mode": value.delivery_mode or "standard",
                "platform_conversation_id": value.platform_conversation_id or None,
                "platform_message_id": value.platform_message_id,
                "last_error": value.error_category or None,
                "version": value.stream_version,
                "created_at": created_at,
            }
            for value, created_at in projections
            if status is None or value.status == status
        ]
        return sorted(result, key=lambda item: item["created_at"], reverse=True)

    def attempts(self, *, turn_id: str = "") -> list[dict[str, Any]]:
        projections = self._replay("run_attempt", replay_run_attempt)
        result = [
            {
                "attempt_id": value.attempt_id,
                "turn_id": value.turn_id,
                "attempt_no": value.attempt_no,
                "status": value.status,
                "worker_id": value.worker_id,
                "lease_version": value.lease_version,
                "lease_expires_at": value.lease_expires_at,
                "checkpoint_ref": value.checkpoint_ref,
                "started_at": value.started_at,
                "finished_at": value.finished_at,
                "version": value.stream_version,
            }
            for value, _ in projections
            if not turn_id or value.turn_id == turn_id
        ]
        return sorted(result, key=lambda item: (item["attempt_no"], item["attempt_id"]))

    def task_attempts(self, *, task_id: str = "") -> list[dict[str, Any]]:
        projections = self._replay("task_attempt", replay_task_attempt)
        result = [
            {
                "task_attempt_id": value.task_attempt_id,
                "task_id": value.task_id,
                "attempt_no": value.attempt_no,
                "status": value.status,
                "lease_owner": value.lease_owner,
                "lease_version": value.lease_version,
                "lease_expires_at": value.lease_expires_at,
                "checkpoint_ref": value.checkpoint_ref,
                "started_at": value.started_at,
                "finished_at": value.finished_at,
                "version": value.stream_version,
            }
            for value, _ in projections
            if not task_id or value.task_id == task_id
        ]
        return sorted(result, key=lambda item: (item["attempt_no"], item["task_attempt_id"]))

    def messages(self, *, conversation_id: str = "") -> list[dict[str, Any]]:
        projections = self._replay("message", replay_message)
        result = [
            {
                "message_id": value.message_id,
                "conversation_id": value.conversation_id,
                "session_id": value.session_id,
                "sender_principal_id": value.sender_principal_id,
                "sender_endpoint_id": value.sender_endpoint_id,
                "role": value.role,
                "direction": value.direction,
                "reply_to_message_id": value.reply_to_message_id,
                "platform_message_id": value.platform_message_id,
                "receive_sequence": value.receive_sequence,
                "trust_label": value.trust_label,
                "raw_payload_ref": value.raw_payload_ref,
                "part_descriptors": list(value.part_descriptors),
                "created_at": value.created_at,
                "version": value.stream_version,
            }
            for value, _ in projections
            if not conversation_id or value.conversation_id == conversation_id
        ]
        return sorted(result, key=lambda item: (item["receive_sequence"], item["message_id"]))

    def conversations(self) -> list[dict[str, Any]]:
        projections = self._replay("conversation", replay_conversation)
        return sorted(
            [
                {
                    "conversation_id": value.conversation_id,
                    "conversation_endpoint_id": value.conversation_endpoint_id,
                    "platform_conversation_id": value.platform_conversation_id,
                    "conversation_endpoint_ref": value.conversation_endpoint_ref,
                    "conversation_type": value.conversation_type,
                    "principal_scope": value.principal_scope,
                    "context_partition_policy": value.context_partition_policy,
                    "status": value.status,
                    "version": value.stream_version,
                    "created_at": created_at,
                }
                for value, created_at in projections
            ],
            key=lambda item: (item["created_at"], item["conversation_id"]),
        )

    def principals(self) -> list[dict[str, Any]]:
        projections = self._replay("principal", replay_principal)
        return sorted(
            [
                {
                    "principal_id": value.principal_id,
                    "principal_type": value.principal_type,
                    "status": value.status,
                    "created_at": value.created_at or created_at,
                    "version": value.stream_version,
                }
                for value, created_at in projections
            ],
            key=lambda item: (item["created_at"], item["principal_id"]),
        )

    def endpoints(self, *, principal_id: str = "") -> list[dict[str, Any]]:
        projections = self._replay("endpoint", replay_endpoint)
        result = [
            {
                "endpoint_id": value.endpoint_id,
                "principal_id": value.principal_id,
                "channel_type": value.channel_type,
                "channel_instance_id": value.channel_instance_id,
                "platform_account_id": value.platform_account_id,
                "endpoint_ref": value.endpoint_ref,
                "capabilities": list(value.capabilities),
                "status": value.status,
                "verified_at": value.verified_at,
                "version": value.stream_version,
                "created_at": created_at,
            }
            for value, created_at in projections
            if not principal_id or value.principal_id == principal_id
        ]
        return sorted(result, key=lambda item: (item["created_at"], item["endpoint_id"]))

    def sessions(self, *, conversation_id: str = "", active_only: bool = False) -> list[dict[str, Any]]:
        projections = self._replay("session", replay_session)
        result = [
            {
                "session_id": value.session_id,
                "conversation_id": value.conversation_id,
                "context_partition_key": value.context_partition_key,
                "reset_generation": value.reset_generation,
                "status": value.status,
                "created_at": value.created_at or created_at,
                "version": value.stream_version,
            }
            for value, created_at in projections
            if (not conversation_id or value.conversation_id == conversation_id)
            and (not active_only or value.status == "active")
        ]
        return sorted(result, key=lambda item: (item["created_at"], item["session_id"]))

    def memories(self) -> list[dict[str, Any]]:
        """Safe Memory lifecycle views without querying ``memory_items``."""
        return [
            {
                "memory_id": value.memory_id,
                "status": value.status,
                "kind": value.kind,
                "principal_id": value.principal_id,
                "superseded_by": value.superseded_by,
                "version": value.stream_version,
            }
            for value, _ in self._replay("memory", replay_memory)
        ]

    def connector_sources(self) -> list[dict[str, Any]]:
        """Safe connector-ingestion views; connector configuration is payload-bound."""
        return [
            {
                "source_item_id": value.source_item_id,
                "connector_id": value.connector_id,
                "status": value.item_status,
                "payload_ref": value.payload_ref,
                "payload_hash": value.payload_hash,
                "version": value.stream_version,
            }
            for value, _ in self._replay("source", replay_connector_source)
        ]

    def _replay(
        self,
        stream_type: str,
        reducer: Callable[[list[Event], str], T | None],
    ) -> list[tuple[T, int]]:
        grouped: dict[str, list[Event]] = defaultdict(list)
        for event in self._events.read_stream_type(stream_type):
            grouped[event.stream_id].append(event)
        output: list[tuple[T, int]] = []
        for stream_id, events in grouped.items():
            value = reducer(events, stream_id)
            if value is not None:
                output.append((value, events[0].occurred_at))
        return output
