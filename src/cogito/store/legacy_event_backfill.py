"""Safe, idempotent import of legacy rows into canonical Event snapshots.

The importer never serializes a legacy row as event attributes.  It preserves
only identity, status, timestamps and existing guarded payload references, so
the Event log can become the historical fact ledger without duplicating private
message/tool/model content.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore

_ENTITY_SPECS: tuple[tuple[str, str, str, str, str], ...] = (
    ("principals", "principal_id", "principal", "interaction.principal.imported", "principal"),
    ("endpoints", "endpoint_id", "endpoint", "interaction.endpoint.imported", "endpoint"),
    ("conversations", "conversation_id", "conversation", "interaction.conversation.imported", "conversation"),
    ("sessions", "session_id", "session", "interaction.session.imported", "session"),
    ("messages", "message_id", "message", "interaction.message.imported", "message"),
    ("turns", "turn_id", "turn", "runtime.turn.imported", "turn"),
    ("run_attempts", "attempt_id", "run_attempt", "runtime.attempt.imported", "run_attempt"),
    ("tasks", "task_id", "task", "task.imported", "task"),
    ("task_attempts", "task_attempt_id", "task_attempt", "task.attempt.imported", "task_attempt"),
    ("model_calls", "model_call_id", "model_call", "model.call.imported", "model_call"),
    ("tool_calls", "tool_call_id", "tool_call", "tool.call.imported", "tool_call"),
    ("deliveries", "delivery_id", "delivery", "delivery.imported", "delivery"),
    ("approvals", "approval_id", "approval", "approval.imported", "approval"),
    ("connector_items", "item_id", "connector_item", "connector.source.imported", "source"),
    ("proactive_candidates", "candidate_id", "proactive_candidate", "proactive.candidate.imported", "proactive_candidate"),
    ("drift_runs", "drift_run_id", "drift_run", "drift.run.imported", "drift_run"),
    ("memory_items", "memory_id", "memory", "memory.imported", "memory"),
    ("knowledge_resources", "resource_id", "knowledge_resource", "knowledge.resource.imported", "knowledge_resource"),
)

# Values in this allow-list are operational metadata or identifiers.  In
# particular, legacy message bodies, memory values, connector summaries, tool
# arguments and model responses are never copied into ``attributes_json``.
_SAFE_SNAPSHOT_COLUMNS: dict[str, tuple[str, ...]] = {
    "principal": ("principal_type",),
    "endpoint": ("channel_type", "channel_instance_id", "platform_account_id", "endpoint_ref", "capabilities", "verified_at"),
    "conversation": ("conversation_endpoint_id", "platform_conversation_id", "conversation_endpoint_ref", "conversation_type", "principal_scope", "context_partition_policy"),
    "session": ("context_partition_key", "reset_generation"),
    "message": ("sender_endpoint_id", "role", "direction", "reply_to_message_id", "platform_message_id", "receive_sequence", "trust_label", "part_descriptors_json"),
    "turn": ("input_message_id", "priority", "active_attempt_id", "final_message_id", "cancel_requested_at", "completed_at"),
    "run_attempt": ("attempt_no", "worker_id", "lease_version", "lease_expires_at", "checkpoint_ref", "error_ref", "finished_at"),
    "task": ("task_type", "priority", "origin", "scheduled_at", "retry_policy_json", "checkpoint_ref", "idempotency_key", "lease_owner", "lease_expires_at", "lease_version", "result_ref"),
    "task_attempt": ("task_id", "attempt_no", "lease_owner", "lease_version", "lease_expires_at", "checkpoint_ref", "finished_at"),
    "model_call": ("request_id", "provider_id", "model_id", "request_hash", "request_payload_ref", "response_payload_ref", "finish_reason", "input_tokens", "output_tokens", "cached_tokens", "latency_ms", "error_category", "retry_count", "started_at", "completed_at"),
    "tool_call": ("attempt_type", "tool_name", "tool_version", "arguments_ref", "result_ref", "result_trust_label", "result_size_bytes", "started_at", "completed_at"),
    "delivery": ("platform_conversation_id", "delivery_mode", "platform_message_id", "scheduled_at", "error_category"),
    "approval": ("responder_id", "expires_at", "decided_at"),
    "connector_item": ("connector_id", "source_item_id", "content_hash", "published_at", "relevance"),
    "proactive_candidate": ("stream_type", "topic", "recommended_action", "policy_version", "source_payload_ref", "expires_at_value", "consumed_at"),
    "memory": ("kind", "principal_id", "supersedes_id", "goal_status", "goal_priority", "goal_deadline", "goal_progress", "valid_from", "valid_to"),
    "knowledge_resource": ("principal_id", "connector_id", "source_uri_hash", "source_kind", "media_type", "content_hash", "trust_label", "scope_type", "scope_id", "source_version", "retention_class"),
}

_JSON_COLUMNS = frozenset({"capabilities", "part_descriptors_json", "retry_policy_json"})


class LegacyEventBackfill:
    """Imports one deterministic ``legacy.<entity>.imported`` Event per legacy row."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._store = EventStore(conn)

    def import_all(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table, id_column, stream_type, _, _ in _ENTITY_SPECS:
            if not self._table_exists(table):
                continue
            counts[table] = self.import_table(table, id_column, stream_type)
        return counts

    def import_table(self, table: str, id_column: str, stream_type: str) -> int:
        columns = self._columns(table)
        if id_column not in columns:
            return 0
        selectable = [
            id_column,
            *self._present(columns, "status", "created_at", "started_at", "completed_at"),
        ]
        for field in _SAFE_SNAPSHOT_COLUMNS.get(stream_type, ()):
            if field in columns and field not in selectable:
                selectable.append(field)
        for field in (
            "trace_id",
            "principal_id",
            "conversation_id",
            "session_id",
            "turn_id",
            "attempt_id",
            "task_id",
            "sender_principal_id",
            "skill_name",
            "skill_version",
            "preemption_reason",
            "steps_taken",
            "budget_used_json",
        ):
            if field in columns and field not in selectable:
                selectable.append(field)
        for field in (
            "payload_ref",
            "content_ref",
            "raw_payload_ref",
            "request_payload_ref",
            "arguments_ref",
            "result_ref",
        ):
            if field in columns and field not in selectable:
                selectable.append(field)
        for field in ("content_hash", "request_hash", "sha256"):
            if field in columns and field not in selectable:
                selectable.append(field)

        rows = self._conn.execute(
            f"SELECT {','.join(selectable)} FROM {table} ORDER BY {id_column} ASC"
        ).fetchall()
        imported = 0
        for row in rows:
            entity_id = str(row[id_column])
            payload_ref = self._first_value(
                row,
                "payload_ref",
                "content_ref",
                "raw_payload_ref",
                "request_payload_ref",
                "arguments_ref",
                "result_ref",
            )
            payload_hash = self._first_value(row, "content_hash", "request_hash", "sha256") or ""
            status = str(row["status"]) if "status" in columns and row["status"] is not None else ""
            event = Event(
                event_id=f"legacy:{table}:{entity_id}",
                event_type=f"legacy.{stream_type}.imported",
                stream_type="legacy",
                stream_id=f"{table}:{entity_id}",
                producer="legacy-event-backfill",
                event_class=EventClass.TELEMETRY,
                context=EventContext(
                    trace_id=str(row["trace_id"] or "") if "trace_id" in columns else "",
                    principal_id=(
                        str(
                            (row["principal_id"] if "principal_id" in columns else "")
                            or (row["sender_principal_id"] if "sender_principal_id" in columns else "")
                            or ""
                        )
                        if "principal_id" in columns or "sender_principal_id" in columns
                        else ""
                    ),
                    conversation_id=(
                        str(row["conversation_id"] or "")
                        if "conversation_id" in columns
                        else ""
                    ),
                    session_id=str(row["session_id"] or "") if "session_id" in columns else "",
                    turn_id=str(row["turn_id"] or "") if "turn_id" in columns else "",
                    attempt_id=str(row["attempt_id"] or "") if "attempt_id" in columns else "",
                    task_id=str(row["task_id"] or "") if "task_id" in columns else "",
                ),
                summary=f"Imported legacy {stream_type}: {entity_id}"[:2_000],
                attributes={"entity_type": stream_type, "legacy_table": table, "status": status},
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                outcome=status or "imported",
                occurred_at=self._timestamp(row, columns),
                idempotency_key=f"legacy-import:{table}:{entity_id}",
            )
            before = self._store.get(event.event_id)
            self._store.append(event)
            snapshot = self._snapshot_spec(table)
            if snapshot is not None:
                self._append_canonical_snapshot(
                    row, columns, entity_id, event, snapshot[0], snapshot[1], stream_type
                )
            if stream_type == "drift_run":
                self._append_drift_snapshot(row, columns, entity_id, event)
            if before is None:
                imported += 1
        return imported

    def _snapshot_spec(self, table: str) -> tuple[str, str] | None:
        for spec_table, _, _, event_type, stream_type in _ENTITY_SPECS:
            if spec_table == table:
                return event_type, stream_type
        return None

    def _append_canonical_snapshot(
        self,
        row: sqlite3.Row,
        columns: set[str],
        entity_id: str,
        legacy_event: Event,
        event_type: str,
        canonical_stream_type: str,
        legacy_stream_type: str,
    ) -> None:
        """Append one reducer-consumable imported snapshot in the aggregate stream."""
        if event_type == "drift.run.imported":
            return  # Drift has its richer, backwards-compatible snapshot below.
        existing = self._store.read_stream(canonical_stream_type, entity_id)
        if existing:
            # A live canonical lifecycle is more informative than a legacy
            # terminal snapshot.  Do not create a conflicting version-zero
            # Event when a partially migrated deployment is cut over.
            return
        attrs = {"legacy_table": str(legacy_event.attributes["legacy_table"]), "status": legacy_event.outcome}
        for name in _SAFE_SNAPSHOT_COLUMNS.get(legacy_stream_type, ()):
            if name not in columns or row[name] is None:
                continue
            value: object = row[name]
            if name in _JSON_COLUMNS:
                try:
                    value = json.loads(str(value))
                except (TypeError, ValueError):
                    continue
            target_name = name.removesuffix("_json")
            if target_name == "idempotency_key":
                target_name = "task_idempotency_key"
            attrs[target_name] = value
        payload_ref = legacy_event.payload_ref
        if legacy_stream_type == "model_call":
            payload_ref = str(attrs.get("request_payload_ref") or attrs.get("response_payload_ref") or "") or None
        if legacy_stream_type == "tool_call":
            payload_ref = str(attrs.get("arguments_ref") or attrs.get("result_ref") or "") or None
        self._store.append(
            Event(
                event_id=f"legacy:{legacy_event.attributes['legacy_table']}:{entity_id}:snapshot",
                event_type=event_type,
                stream_type=canonical_stream_type,
                stream_id=entity_id,
                producer="legacy-event-backfill",
                event_class=EventClass.TELEMETRY,
                context=legacy_event.context,
                summary=f"Imported {legacy_stream_type} snapshot: {entity_id}"[:2_000],
                attributes=attrs,
                payload_ref=payload_ref,
                payload_hash=legacy_event.payload_hash,
                outcome=legacy_event.outcome or "imported",
                occurred_at=legacy_event.occurred_at,
                idempotency_key=f"legacy-canonical-snapshot:{legacy_event.attributes['legacy_table']}:{entity_id}",
            ),
            expected_version=0,
        )

    def _append_drift_snapshot(
        self,
        row: sqlite3.Row,
        columns: set[str],
        run_id: str,
        legacy_event: Event,
    ) -> None:
        """Make a legacy Drift terminal snapshot replayable without inventing steps."""
        if self._store.read_stream("drift_run", run_id):
            return
        self._store.append(
            Event(
                event_id=f"legacy:drift_runs:{run_id}:snapshot",
                event_type="drift.run.imported",
                stream_type="drift_run",
                stream_id=run_id,
                producer="legacy-event-backfill",
                event_class=EventClass.TELEMETRY,
                context=legacy_event.context,
                summary=f"Imported Drift run snapshot: {run_id}"[:2_000],
                attributes={
                    "skill_name": str(row["skill_name"] or "") if "skill_name" in columns else "",
                    "skill_version": (
                        str(row["skill_version"] or "") if "skill_version" in columns else ""
                    ),
                    "preemption_reason": (
                        str(row["preemption_reason"] or "")
                        if "preemption_reason" in columns
                        else ""
                    ),
                    "steps_taken": int(row["steps_taken"] or 0)
                    if "steps_taken" in columns
                    else 0,
                },
                payload_ref=legacy_event.payload_ref,
                payload_hash=legacy_event.payload_hash,
                outcome=legacy_event.outcome,
                occurred_at=legacy_event.occurred_at,
                idempotency_key=f"legacy-drift-snapshot:{run_id}",
            ),
            expected_version=0,
        )

    def verify(self) -> dict[str, bool]:
        """Check that every importable legacy row has exactly one snapshot Event."""
        result: dict[str, bool] = {}
        for table, id_column, _, _, _ in _ENTITY_SPECS:
            if not self._table_exists(table) or id_column not in self._columns(table):
                continue
            legacy_count = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            event_count = self._conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE producer='legacy-event-backfill' "
                "AND stream_id LIKE ?",
                (f"{table}:%",),
            ).fetchone()[0]
            result[table] = int(legacy_count) == int(event_count)
        return result

    def _table_exists(self, table: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def _columns(self, table: str) -> set[str]:
        return {str(row["name"]) for row in self._conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _present(columns: set[str], *candidates: str) -> list[str]:
        return [candidate for candidate in candidates if candidate in columns]

    @staticmethod
    def _first_value(row: sqlite3.Row, *names: str) -> str | None:
        for name in names:
            if name in row.keys() and row[name]:
                return str(row[name])
        return None

    @staticmethod
    def _timestamp(row: sqlite3.Row, columns: set[str]) -> int:
        for name in ("completed_at", "started_at", "created_at"):
            if name not in columns or row[name] is None:
                continue
            value = row[name]
            if isinstance(value, int):
                return value
            text = str(value)
            if text.isdigit():
                return int(text)
            try:
                return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                continue
        return int(datetime.now(UTC).timestamp() * 1000)
