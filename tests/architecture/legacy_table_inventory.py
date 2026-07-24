"""Machine-readable inventory of every legacy state table targeted for deletion.

Each entry records: target replay stream type, replay function, legacy importer
event type, the phase that will delete it, and its current migration status.

This file is the single source of truth — architect test_legacy_table_scan.py
reads it to produce the CI allow-list, and each phase updates `status` as the
corresponding legacy code path is removed.

Status values:
  pure_legacy — full CRUD against legacy table, no Event code path
  dual_path — both Event and legacy code paths exist (runtime switch)
  event_sourced — read/write through Event only, legacy table still exists
  kept — table stays (payload store, schema metadata, operational only)
  migration_only — only accessed in migration.py / backfill / cutover tooling
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LegacyTable:
    """One legacy SQLite table and its Event Sourcing replacement metadata."""

    table: str
    domain: str
    replay_stream_type: str | None         # None = no replay function yet
    replay_function: str | None            # None = not yet implemented
    legacy_event: str | None               # legacy.<entity>.imported
    phase: str                             # 04 … 10
    status: str                            # see docstring above
    notes: str = ""


_INVENTORY: list[LegacyTable] = [
    # ── Phase 04: Interaction / Identity ────────────────────────────────
    LegacyTable("principals", "identity", "principal", "replay_principal",
                "legacy.principal.imported", "04", "dual_path",
                "replaced by interaction.principal.created/imported Events"),
    LegacyTable("endpoints", "identity", "endpoint", "replay_endpoint",
                "legacy.endpoint.imported", "04", "dual_path",
                "replaced by interaction.endpoint.created/imported Events"),
    LegacyTable("conversations", "identity", "conversation", "replay_conversation",
                "legacy.conversation.imported", "04", "dual_path",
                "replaced by interaction.conversation.created/imported Events"),
    LegacyTable("sessions", "identity", "session", "replay_session",
                "legacy.session.imported", "04", "dual_path",
                "replaced by interaction.session.created/imported Events"),
    LegacyTable("messages", "interaction", "message", "replay_message",
                "legacy.message.imported", "04", "dual_path",
                "replaced by interaction.message.accepted/recorded Events"),
    LegacyTable("content_parts", "interaction", None, None,
                None, "04", "pure_legacy",
                "payload content → PayloadStore only; no Event replay needed"),
    LegacyTable("message_revisions", "interaction", None, None,
                None, "04", "pure_legacy",
                "not in current schema, remove on cutover"),
    LegacyTable("inbound_inbox", "interaction", None, None,
                None, "04", "pure_legacy",
                "transient dedup → replaced by Event idempotency key"),

    # ── Phase 04: Execution ─────────────────────────────────────────────
    LegacyTable("turns", "execution", "turn", "replay_turn",
                "legacy.turn.imported", "04", "dual_path",
                "replaced by runtime.turn.* Event stream"),
    LegacyTable("run_attempts", "execution", "run_attempt", "replay_run_attempt",
                "legacy.run_attempt.imported", "04", "dual_path",
                "replaced by runtime.attempt.* Event stream"),
    LegacyTable("turn_checkpoints", "execution", None, None,
                None, "04", "pure_legacy",
                "replaced by runtime.checkpoint.saved Event + payload ref"),

    # ── Phase 04: Delivery ──────────────────────────────────────────────
    LegacyTable("deliveries", "delivery", "delivery", "replay_delivery",
                "legacy.delivery.imported", "04", "dual_path",
                "replaced by delivery.* Event stream"),
    LegacyTable("delivery_attempts", "delivery", None, None,
                None, "04", "pure_legacy",
                "subsumed by delivery.* lifecycle Events"),
    LegacyTable("delivery_receipts", "delivery", "side_effect_receipt",
                "replay_side_effect_receipt",
                "legacy.side_effect_receipt.imported", "04", "dual_path",
                "replaced by side_effect.receipt.* Event stream"),

    # ── Phase 04: Approval ──────────────────────────────────────────────
    LegacyTable("approvals", "capability", "approval", "replay_approval",
                "legacy.approval.imported", "04", "dual_path",
                "replaced by approval.* Event stream"),

    # ── Phase 04: Tool / Model calls ────────────────────────────────────
    LegacyTable("tool_calls", "capability", "tool_call", "replay_tool_call",
                "legacy.tool_call.imported", "04", "dual_path",
                "replaced by tool.call.* Event stream"),
    LegacyTable("model_calls", "capability", "model_call", "replay_model_call",
                "legacy.model_call.imported", "04", "dual_path",
                "replaced by model.call.* Event stream"),

    # ── Phase 05: Task / Scheduler ──────────────────────────────────────
    LegacyTable("tasks", "background", "task", "replay_task",
                "legacy.task.imported", "05", "dual_path",
                "TaskRepository(event_sourced=True) exists but not default"),
    LegacyTable("task_attempts", "background", "task_attempt", "replay_task_attempt",
                "legacy.task_attempt.imported", "05", "dual_path",
                "TaskRepository(event_sourced=True) exists"),
    LegacyTable("task_checkpoints", "background", None, None,
                None, "05", "pure_legacy",
                "replaced by checkpoint Events + payload ref"),
    LegacyTable("schedules", "background", None, None,
                None, "05", "pure_legacy",
                "need Schedule replay function (not yet implemented)"),
    LegacyTable("scheduled_fires", "background", None, None,
                None, "05", "pure_legacy",
                "subsumed by schedule.* Events (not yet implemented)"),
    LegacyTable("agent_delegations", "background", None, None,
                None, "05", "pure_legacy",
                "need Delegation replay (not yet implemented)"),
    LegacyTable("child_task_links", "background", None, None,
                None, "05", "pure_legacy",
                "subsumed by Delegation causation chain"),
    LegacyTable("waiting_conditions", "background", None, None,
                None, "05", "pure_legacy",
                "subsumed by runtime.turn.waiting_user/external Events"),

    # ── Phase 05: Capability management ─────────────────────────────────
    LegacyTable("capabilities", "capability", None, None,
                None, "05", "pure_legacy",
                "need Capability replay (not yet implemented)"),
    LegacyTable("agent_tool_command_results", "capability", None, None,
                None, "05", "pure_legacy",
                "replaced by agent.command.completed Event"),
    LegacyTable("skills", "capability", None, None,
                None, "05", "pure_legacy",
                "need Skill replay (not yet implemented)"),

    # ── Phase 05: Plugin (PLAN-10) ──────────────────────────────────────
    LegacyTable("plugins", "capability", None, None,
                None, "05", "pure_legacy",
                "need Plugin replay (not yet implemented)"),
    LegacyTable("plugin_snapshots", "capability", None, None,
                None, "05", "pure_legacy",
                "plugin lifecycle Events needed"),
    LegacyTable("plugin_runtime_audit", "capability", None, None,
                None, "05", "pure_legacy",
                "subsumed by Event audit trail"),

    # ── Phase 06: Connector ─────────────────────────────────────────────
    LegacyTable("connectors", "connector", None, None,
                None, "06", "pure_legacy",
                "need Connector replay (not yet implemented)"),
    LegacyTable("connector_cursors", "connector", None, None,
                None, "06", "pure_legacy",
                "subsumed by connector.cursor.updated Event"),
    LegacyTable("connector_raw_items", "connector", None, None,
                None, "06", "pure_legacy",
                "transient → replaced by connector.source.ingested"),
    LegacyTable("connector_items", "connector", "source", "replay_connector_source",
                "legacy.connector.source.imported", "06", "dual_path",
                "replaced by connector.source.ingested Event"),

    # ── Phase 06: Memory ────────────────────────────────────────────────
    LegacyTable("memory_items", "memory", "memory", "replay_memory",
                "legacy.memory.imported", "06", "dual_path",
                "replaced by memory.* Event stream"),
    LegacyTable("memory_embeddings", "memory", None, None,
                None, "06", "pure_legacy",
                "derived index → rebuild from Events + PayloadStore"),
    LegacyTable("memory_relations", "memory", None, None,
                None, "06", "pure_legacy",
                "derived → rebuild from memory.* Events"),
    LegacyTable("memory_sources", "memory", None, None,
                None, "06", "pure_legacy",
                "subsumed by memory.source.invalidated Event"),
    LegacyTable("memory_sources_v2", "memory", None, None,
                None, "06", "pure_legacy",
                "duplicate of memory_sources, not in current schema"),
    LegacyTable("memory_signals", "memory", None, None,
                None, "06", "pure_legacy",
                "subsumed by memory.signal.recorded Event"),
    LegacyTable("memory_fts", "memory", None, None,
                None, "06", "pure_legacy",
                "derived FTS index → rebuild from memory Events + payload"),

    # ── Phase 06: Knowledge ─────────────────────────────────────────────
    LegacyTable("knowledge_resources", "knowledge", "knowledge_resource",
                "replay_knowledge_resource",
                "legacy.knowledge_resource.imported", "06", "dual_path",
                "replaced by knowledge.resource.* Events"),
    LegacyTable("knowledge_documents", "knowledge", None, None,
                None, "06", "pure_legacy",
                "subsumed by knowledge.document.parsed Event"),
    LegacyTable("knowledge_segments", "knowledge", None, None,
                None, "06", "pure_legacy",
                "need KnowledgeSegment replay (not yet implemented)"),
    LegacyTable("knowledge_embeddings", "knowledge", None, None,
                None, "06", "pure_legacy",
                "derived index → rebuild from Events + PayloadStore"),
    LegacyTable("knowledge_fts", "knowledge", None, None,
                None, "06", "pure_legacy",
                "derived FTS index → rebuild from knowledge Events"),

    # ── Phase 07: Proactive ─────────────────────────────────────────────
    LegacyTable("proactive_candidates", "proactive", "proactive_candidate",
                "replay_proactive_candidate",
                "legacy.proactive_candidate.imported", "07", "dual_path",
                "replaced by proactive.candidate.* Events"),
    LegacyTable("proactive_decisions", "proactive", None, None,
                None, "07", "pure_legacy",
                "superseded by proactive_decisions_v2"),
    LegacyTable("proactive_decisions_v2", "proactive", None, None,
                None, "07", "pure_legacy",
                "replaced by proactive.decision.made Event"),
    LegacyTable("proactive_policies", "proactive", None, None,
                None, "07", "pure_legacy",
                "need Policy replay (not yet implemented)"),
    LegacyTable("proactive_cadence_state", "proactive", None, None,
                None, "07", "pure_legacy",
                "need Cadence replay (not yet implemented)"),
    LegacyTable("proactive_signals", "proactive", None, None,
                None, "07", "pure_legacy",
                "subsumed by proactive.* Events"),
    LegacyTable("proactive_ticks", "proactive", None, None,
                None, "07", "pure_legacy",
                "need Tick replay (not yet implemented)"),

    # ── Phase 07: Drift ─────────────────────────────────────────────────
    LegacyTable("drift_runs", "drift", "drift_run", None,
                "legacy.drift_run.imported", "07", "dual_path",
                "replaced by drift.run.* Events (replay not fully implemented)"),
    LegacyTable("drift_skill_state", "drift", None, None,
                None, "07", "pure_legacy",
                "replaced by drift.skill_state.updated Events"),
    LegacyTable("drift_results", "drift", None, None,
                None, "07", "pure_legacy",
                "replaced by drift.result.committed Event"),

    # ── Phase 07: Multimodal ────────────────────────────────────────────
    LegacyTable("multimodal_assets", "multimodal", None, None,
                None, "07", "pure_legacy",
                "need MultimodalAsset Event lifecycle"),
    LegacyTable("multimodal_links", "multimodal", None, None,
                None, "07", "pure_legacy",
                "subsumed by multimodal.* Events"),
    LegacyTable("vision_analyses", "multimodal", None, None,
                None, "07", "pure_legacy",
                "need VisionAnalysis replay"),
    LegacyTable("sticker_metadata", "multimodal", None, None,
                None, "07", "pure_legacy",
                "need Sticker Event lifecycle"),

    # ── Phase 07: Digest ────────────────────────────────────────────────
    LegacyTable("digests", "proactive", None, None,
                None, "07", "pure_legacy",
                "need Digest replay (not yet implemented)"),
    LegacyTable("digest_items", "proactive", None, None,
                None, "07", "pure_legacy",
                "subsumed by digest.* Events"),

    # ── Phase 08: Audit / Trace / Outbox ────────────────────────────────
    LegacyTable("audit_records", "observability", None, None,
                None, "08", "pure_legacy",
                "replaced by Event audit fields + Event Explorer"),
    LegacyTable("traces", "observability", None, None,
                None, "08", "pure_legacy",
                "replaced by EventStore.trace()"),
    LegacyTable("spans", "observability", None, None,
                None, "08", "pure_legacy",
                "subsumed into Event causal fields"),
    LegacyTable("events", "observability", None, None,
                None, "08", "pure_legacy",
                "old event table, distinct from event_log"),
    LegacyTable("outbox_events", "observability", None, None,
                None, "08", "pure_legacy",
                "replaced by Event consumer subscription"),
    LegacyTable("side_effect_receipts", "observability", "side_effect_receipt",
                "replay_side_effect_receipt",
                "legacy.side_effect_receipt.imported", "08", "dual_path",
                "replaced by side_effect.receipt.* Events"),
    LegacyTable("event_consumptions", "observability", None, None,
                None, "08", "pure_legacy",
                "replaced by Event idempotency keys"),
    LegacyTable("commands", "observability", None, None,
                None, "08", "pure_legacy",
                "replaced by command Event stream"),
    LegacyTable("context_snapshots", "observability", None, None,
                None, "08", "pure_legacy",
                "replaced by runtime.checkpoint.* Events + payload ref"),
    LegacyTable("context_snapshot_items", "observability", None, None,
                None, "08", "pure_legacy",
                "subsumed by checkpoint payload"),

    # ── Kept / Infrastructure (not deleted) ─────────────────────────────
    LegacyTable("payload_objects", "infrastructure", None, None,
                None, "kept", "kept",
                "payload store — not a business state table"),
    LegacyTable("event_log", "infrastructure", None, None,
                None, "kept", "kept",
                "THE single source of truth — active Event Store table"),
    LegacyTable("_schema_version", "infrastructure", None, None,
                None, "kept", "kept",
                "schema migration metadata"),
    LegacyTable("_event_store_cutover", "infrastructure", None, None,
                None, "kept", "kept",
                "cutover marker — present after migration"),
    LegacyTable("_backfill_progress", "infrastructure", None, None,
                None, "kept", "kept",
                "backfill checkpoint — migration tooling only"),
    LegacyTable("config_versions", "infrastructure", None, None,
                None, "kept", "kept",
                "config version tracking — operational table"),
    LegacyTable("backups", "infrastructure", None, None,
                None, "kept", "kept",
                "backup manifest — operational/production ops"),
    LegacyTable("processing_watermarks", "infrastructure", None, None,
                None, "kept", "kept",
                "processing watermark — migration tooling"),
    LegacyTable("gateway_operation_receipts", "infrastructure", None, None,
                None, "kept", "kept",
                "gateway bridge receipts — LangBot bridge protocol"),

    # ── Phase 09: Cleanup of obsolete schema items ──────────────────────
    LegacyTable("message_revisions", "interaction", None, None,
                None, "09", "pure_legacy",
                "not in current schema definition; remove on cutover"),
    LegacyTable("memory_sources_v2", "memory", None, None,
                None, "09", "pure_legacy",
                "not in current schema definition; remove on cutover"),
    LegacyTable("proactive_decisions", "proactive", None, None,
                None, "09", "pure_legacy",
                "obsolete, superseded by proactive_decisions_v2"),
]


def all_legacy_tables() -> list[LegacyTable]:
    return list(_INVENTORY)


def tables_by_phase(phase: str) -> list[LegacyTable]:
    return [t for t in _INVENTORY if t.phase == phase]


def tables_by_status(status: str) -> list[LegacyTable]:
    return [t for t in _INVENTORY if t.status == status]


def legacy_table_names() -> set[str]:
    """Return all legacy table names targeted for removal."""
    return {t.table for t in _INVENTORY if t.phase not in ("kept", "10", "11")}


def kept_table_names() -> set[str]:
    """Return all infrastructure/operational tables that stay."""
    return {t.table for t in _INVENTORY if t.phase == "kept"}
