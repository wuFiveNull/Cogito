"""Architecture test — detect new legacy-table SQL in production code.

CI scanning rule from 01-baseline-and-guardrails.md:
  - Every legacy table reference in ``src/cogito`` must be accounted for.
  - Only migration/backfill/cutover tooling and dedicated cleanup tests
    may reference legacy tables by SQL name.
  - New code MUST NOT add SQL against ``principals``, ``turns``, ``tasks``,
    ``deliveries``, ``memory_items``, ``proactive_candidates``, etc.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

from tests.architecture.legacy_table_inventory import (
    all_legacy_tables,
    legacy_table_names,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "cogito"

# ── Explicit allow-list ─────────────────────────────────────────────────────
# Files that are permitted to reference legacy business tables.
# Every entry must be an explicit file path — no directory wildcards.
_ALLOWED_FILES: frozenset[str] = frozenset({
    # Migration tooling — operates on old tables during schema upgrade
    "store/migration.py",
    "store/backfill.py",
    # Legacy backfill — one-time import of old rows into Event snapshots
    "store/legacy_event_backfill.py",
    # Cutover migration tooling — validates and contracts legacy tables
    "store/event_store_cutover.py",
    # The inventory itself — declares the old table list by name
    "tests/architecture/legacy_table_inventory.py",
    "tests/architecture/test_legacy_table_scan.py",

    # ── Schema definition — DDL, not DML ──────────────────────────────
    "store/schema.py",
})

# ── Tables with CI carve-out ─────────────────────────────────────────────────
# Every (file, table) combination listed below is a KNOWN pre-existing legacy
# dependency.  The carve-out exists only so the CI sees NEW violations.
#
# As each phase completes, the corresponding entries are REMOVED from this
# dictionary so any remaining references fail the test.
_CI_CARVEOUT_TABLES: dict[str, frozenset[str]] = {
    # Phase 04: identity tables moved to kept carve-out
    "config_versions": frozenset({"store/config_version_repo.py"}),
    "payload_objects": frozenset({"infrastructure/payload_store.py",
                                  "service/asset_service.py",
                                  "service/sticker_service.py"}),
    "gateway_operation_receipts": frozenset({"channel/bridge_server.py"}),
    # kept = the test's legacy_table_names() already excludes these,
    # but the carve-out won't hurt when they're not searched.
    "event_log": frozenset(),
    "_schema_version": frozenset({"store/migration.py"}),
    "_backfill_progress": frozenset({"store/backfill.py"}),
    "backups": frozenset({"service/api/command_handlers.py",
                          "service/api/query_service.py"}),

    # ── Phase 04: Interaction / Identity / Execution / Delivery ─────
    "content_parts": frozenset({"application.py",
                                "contracts/context.py",
                                "service/channel_gateway.py",
                                "service/completion.py",
                                "service/sqlite_delivery_service.py",
                                "service/summary_service.py",
                                "store/multimodal_repo.py",
                                "store/repositories.py",
                                "service/api/query_service.py",
                                "service/task_handlers.py"}),
    "inbound_inbox": frozenset({"channel/bridge_server.py",
                                "store/repositories.py"}),
    "conversations": frozenset({"service/sticker_service.py",
                                "store/repositories.py",
                                "service/api/query_service.py",
                                "service/api/command_handlers.py",
                                "service/task_handlers.py"}),
    "messages": frozenset({"application.py", "contracts/context.py",
                           "service/completion.py", "service/memory_extractor.py",
                           "service/presence.py", "service/summary_service.py",
                           "service/sticker_service.py",
                           "service/api/query_service.py",
                           "store/repositories.py",
                           "store/multimodal_repo.py",
                           "service/dispatcher.py",
                           "service/event_consumers.py",
                           "service/task_handlers.py"}),
    "sessions": frozenset({"contracts/context.py",
                           "store/repositories.py",
                           "service/api/query_service.py",
                           "service/api/command_handlers.py",
                           "service/dispatcher.py",
                           "service/task_handlers.py"}),
    "endpoints": frozenset({"store/repositories.py",
                            "service/api/query_service.py",
                            "service/task_handlers.py"}),
    "principals": frozenset({"store/repositories.py"}),
    "turns": frozenset({"service/dispatcher.py", "service/inbound_service.py",
                        "service/recovery_service.py",
                        "service/delegation_lifecycle.py",
                        "service/drift_admission.py",
                        "service/drift_preemption.py",
                        "service/event_consumers.py",
                        "service/api/command_handlers.py",
                        "service/api/command_service.py",
                        "service/api/query_service.py",
                        "store/repositories.py"}),
    "run_attempts": frozenset({"service/dispatcher.py",
                               "service/recovery_service.py",
                               "service/delegation_lifecycle.py",
                               "service/api/query_service.py",
                               "store/repositories.py"}),
    "turn_checkpoints": frozenset({"store/checkpoint_repo.py"}),
    "deliveries": frozenset({"service/drift_runner.py",
                             "service/api/command_handlers.py",
                             "store/repositories.py"}),
    "delivery_attempts": frozenset({"store/repositories.py"}),
    "delivery_receipts": frozenset({"store/repositories.py"}),
    "approvals": frozenset(),
    "tool_calls": frozenset(),
    "model_calls": frozenset(),
    "side_effect_receipts": frozenset(),

    # ── Phase 05: Task / Scheduler / Delegation ────────────────────
    "tasks": frozenset({"contracts/context.py",
                        "service/drift_admission.py",
                        "service/drift_preemption.py",
                        "service/recovery_service.py",
                        "service/event_consumers.py",
                        "service/delegation_lifecycle.py",
                        "service/task_dispatcher.py",
                        "service/api/command_handlers.py",
                        "service/api/command_service.py",
                        "service/api/query_service.py",
                        "store/multimodal_repo.py",
                        "store/task_repo.py"}),
    "task_attempts": frozenset({"service/drift_preemption.py",
                                "service/recovery_service.py",
                                "service/task_dispatcher.py",
                                "store/task_repo.py"}),
    "task_checkpoints": frozenset({"store/task_checkpoint_repo.py"}),
    "schedules": frozenset({"service/scheduler.py",
                            "service/api/command_handlers.py",
                            "service/api/query_service.py",
                            "store/schedule_repo.py"}),
    "scheduled_fires": frozenset({"store/schedule_repo.py"}),
    "agent_delegations": frozenset({"service/delegation_lifecycle.py",
                                    "service/task_handlers.py",
                                    "service/tool_sinks.py"}),
    "child_task_links": frozenset({"service/delegation_lifecycle.py",
                                   "service/task_handlers.py",
                                   "service/api/command_service.py"}),
    "waiting_conditions": frozenset({"service/delegation_lifecycle.py",
                                     "service/dispatcher.py",
                                     "service/tool_sinks.py"}),
    "capabilities": frozenset({"store/capability_repo.py",
                               "service/api/command_handlers.py",
                               "service/api/query_service.py"}),
    "agent_tool_command_results": frozenset({"service/agent_tool_commands.py"}),
    "skills": frozenset({"service/agent_tool_commands.py",
                         "service/api/command_handlers.py",
                         "service/api/query_service.py"}),
    "plugins": frozenset({"capability/plugin_runtime.py",
                          "service/api/query_service.py"}),
    "plugin_snapshots": frozenset({"capability/plugin_runtime.py"}),
    "plugin_runtime_audit": frozenset({"capability/plugin_runtime.py"}),
    "commands": frozenset({"store/command_audit_repo.py",
                           "service/api/command_handlers.py"}),

    # ── Phase 06: Connector / Memory / Knowledge ───────────────────
    "connectors": frozenset({"service/proactive_digest_service.py",
                             "service/scheduler.py",
                             "service/api/command_handlers.py",
                             "service/api/query_service.py",
                             "store/connector_repo.py",
                             "store/mcp_connector_repo.py"}),
    "connector_cursors": frozenset({"store/connector_repo.py",
                                    "service/api/query_service.py"}),
    "connector_raw_items": frozenset({"store/connector_repo.py"}),
    "connector_items": frozenset({"service/proactive_digest_service.py",
                                  "service/digest_service.py",
                                  "service/event_consumers.py",
                                  "service/api/query_service.py",
                                  "store/connector_repo.py",
                                  "service/digest_service.py"}),
    "memory_items": frozenset({"service/memory_signals.py",
                               "service/retrieval_service.py",
                               "service/explain.py",
                               "service/memory_views.py",
                               "service/api/query_service.py",
                               "service/cognition_metrics_service.py",
                               "store/memory_repo.py"}),
    "memory_embeddings": frozenset({"store/memory_repo.py"}),
    "memory_relations": frozenset({"service/memory_views.py",
                                   "store/memory_repo.py"}),
    "memory_sources": frozenset({"service/explain.py",
                                 "service/memory_service.py",
                                 "service/knowledge/service.py",
                                 "store/memory_repo.py"}),
    "memory_signals": frozenset({"store/signal_repo.py"}),
    "memory_fts": frozenset({"service/retrieval_service.py",
                             "store/memory_repo.py"}),
    "knowledge_resources": frozenset({"service/knowledge/service.py",
                                      "service/knowledge/sync.py",
                                      "service/knowledge_views.py",
                                      "service/explain.py",
                                      "service/api/command_handlers.py",
                                      "service/api/query_service.py",
                                      "service/cognition_metrics_service.py",
                                      "store/knowledge_repo.py"}),
    "knowledge_documents": frozenset({"service/explain.py",
                                      "service/api/query_service.py",
                                      "store/knowledge_repo.py"}),
    "knowledge_segments": frozenset({"service/explain.py",
                                     "service/knowledge/embedding.py",
                                     "service/knowledge/service.py",
                                     "service/api/query_service.py",
                                     "service/cognition_metrics_service.py",
                                     "store/knowledge_repo.py"}),
    "knowledge_embeddings": frozenset({"store/knowledge_repo.py"}),
    "knowledge_fts": frozenset({"service/knowledge/embedding.py",
                                "store/knowledge_repo.py"}),
    "ingestion_batches": frozenset({"service/recovery_service.py",
                                    "service/mcp_connector_handler.py",
                                    "service/api/query_service.py"}),

    # ── Phase 07: Proactive / Drift / Multimodal ───────────────────
    "proactive_candidates": frozenset({"service/drift_projection.py",
                                       "service/event_consumers.py",
                                       "service/api/query_service.py",
                                       "store/proactive_repo.py"}),
    "proactive_decisions_v2": frozenset({"service/proactive_feedback.py",
                                         "service/api/query_service.py",
                                         "store/proactive_repo.py"}),
    "proactive_policies": frozenset({"service/api/command_handlers.py",
                                     "service/api/query_service.py",
                                     "store/proactive_repo.py"}),
    "proactive_cadence_state": frozenset({"service/scheduler.py"}),
    "proactive_signals": frozenset({"service/proactive_feedback.py",
                                    "service/api/query_service.py"}),
    "proactive_ticks": frozenset({"service/api/query_service.py"}),
    "drift_runs": frozenset({"service/drift_admission.py",
                             "service/drift_preemption.py",
                             "service/drift_projection.py",
                             "service/event_consumers.py",
                             "service/api/query_service.py",
                             "store/drift_repo.py"}),
    "drift_skill_state": frozenset({"service/drift_preemption.py",
                                    "service/api/query_service.py",
                                    "store/drift_repo.py"}),
    "drift_results": frozenset({"service/api/query_service.py",
                                "store/drift_result_repo.py"}),
    "multimodal_assets": frozenset({"store/multimodal_repo.py"}),
    "vision_analyses": frozenset({"store/multimodal_repo.py"}),
    "digests": frozenset({"service/proactive_digest_service.py",
                          "service/digest_service.py",
                          "store/digest_repo.py"}),
    "digest_items": frozenset({"service/proactive_digest_service.py",
                               "service/digest_service.py",
                               "store/digest_repo.py"}),

    # ── Phase 08: Observability ────────────────────────────────────
    "audit_records": frozenset({"service/api/audit.py",
                                "service/api/query_service.py"}),
    "event_consumptions": frozenset({"service/event_consumers.py"}),
    "context_snapshots": frozenset({"store/context_snapshot_repo.py"}),
    "context_snapshot_items": frozenset({"store/context_snapshot_repo.py"}),

    # ── Infrastructure (kept) ──────────────────────────────────────
    "processing_watermarks": frozenset({"store/watermark_repo.py"}),

    # Phase 04: tables with zero production references (already event-only)
    "message_revisions": frozenset(),
    "memory_sources_v2": frozenset(),
    "proactive_decisions": frozenset(),
    "traces": frozenset(),
    "spans": frozenset(),
    "events": frozenset(),
    "outbox_events": frozenset(),
    "multimodal_links": frozenset(),
    "sticker_metadata": frozenset(),
}


class LegacyTableScanError(AssertionError):
    """Raised when new legacy-table SQL is found outside the allow-list."""


def _find_sql_table_references(file_path: Path, legacy_tables: set[str]) -> list[tuple[int, str, str]]:
    """Scan a single file for SQL references to legacy tables.

    Returns [(line_number, table_name, matched_line_text), ...].
    Uses a regex that matches FROM|JOIN|INTO|UPDATE|DELETE FROM patterns
    combined with a table name as a word boundary.

    Uses case-INSENSITIVE SQL keywords but case-SENSITIVE table names
    to avoid false positives on English words like "from Events".
    """
    hits: list[tuple[int, str, str]] = []
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return hits

    sql_keywords = ["FROM ", "JOIN ", "INTO ", "UPDATE ", "DELETE FROM "]
    for table in sorted(legacy_tables):
        for kw in sql_keywords:
            # Build pattern that matches kw (case-insensitive) + table (case-sensitive)
            pattern = re.compile(
                r"\b" + kw + re.escape(table) + r"\b",
                re.IGNORECASE,
            )
            for match in pattern.finditer(text):
                pos = match.start()
                # Quick docstring/comment filter: skip if preceded
                # by """ or # on the same line before this match.
                line_start = text.rfind("\n", 0, pos) + 1 if pos > 0 else 0
                line_prefix = text[line_start:pos]
                if '"""' in line_prefix or line_prefix.lstrip().startswith("#"):
                    continue

                line_no = text[:pos].count("\n") + 1
                line_text = text.splitlines()[line_no - 1].strip()
                hits.append((line_no, table, line_text[:120]))

    return hits


def _relative_path(file_path: Path) -> str:
    """Return path relative to src/cogito/ (or fallback to just the filename)."""
    try:
        rel = file_path.relative_to(SRC_ROOT)
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(file_path)


def scan_src_for_legacy_sql() -> dict[str, list[tuple[int, str, str]]]:
    """Scan all .py files under src/cogito (not tests/) for legacy table SQL.

    Returns {relative_path: [(line_no, table, line_text), ...]}.
    """
    legacy_tables = legacy_table_names()
    results: dict[str, list[tuple[int, str, str]]] = {}

    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in str(py_file):
            continue
        rel = _relative_path(py_file)
        if rel in _ALLOWED_FILES:
            continue
        hits = _find_sql_table_references(py_file, legacy_tables)
        if hits:
            results[rel] = hits

    return results


def check_carveout_violations(
    scan_results: dict[str, list[tuple[int, str, str]]],
) -> dict[str, list[tuple[int, str, str]]]:
    """Remove hits that match the CI carve-out by (file, table).

    Returns the remaining violations after all carve-outs are applied.
    """
    violations: dict[str, list[tuple[int, str, str]]] = {}
    for rel, hits in scan_results.items():
        remaining: list[tuple[int, str, str]] = []
        for line_no, table, line_text in hits:
            allowed_files = _CI_CARVEOUT_TABLES.get(table)
            if allowed_files and rel in allowed_files:
                continue  # this combination is expressly permitted
            remaining.append((line_no, table, line_text))
        if remaining:
            violations[rel] = remaining
    return violations


# ── Tests ───────────────────────────────────────────────────────────────────


def test_no_unaccounted_legacy_table_sql() -> None:
    """Fail if ANY production file references a legacy table outside the
    allow-list or carve-out.

    This test catches new code that accidentally reads/writes old tables
    without a migration path.
    """
    raw = scan_src_for_legacy_sql()
    violations = check_carveout_violations(raw)

    if not violations:
        return

    parts = [
        "Legacy table SQL found outside allow-list/carve-out:",
        "",
    ]
    for rel in sorted(violations):
        parts.append(f"  {rel}:")
        for line_no, table, text in violations[rel]:
            parts.append(f"    L{line_no}: {table} — {text}")
    parts.append("")
    parts.append(
        textwrap.dedent("""\
    Action:
      - If the table access is a NEW dependency, remove it and use Event replay.
      - If it's EXISTING code that is not yet migrated, add the file to either
        _ALLOWED_FILES (for migration/cutover tooling) or _CI_CARVEOUT_TABLES
        (for production code with a known migration phase).
      - NEVER add a broad directory wildcard. Every allowed file is explicit.
    """))

    msg = "\n".join(parts)
    print(msg)  # visible in CI output even on success
    if violations:
        raise LegacyTableScanError(
            f"{sum(len(hits) for hits in violations.values())} legacy table "
            f"references in {len(violations)} files outside allow-list"
        )


def test_allow_list_is_current() -> None:
    """Verify that every file in _ALLOWED_FILES actually exists on disk."""
    missing: list[str] = []
    for rel in _ALLOWED_FILES:
        # Check src/cogito/ and tests/ directories
        path = SRC_ROOT / rel
        if path.exists():
            continue
        test_path = Path(__file__).resolve().parent.parent.parent / rel
        if test_path.exists():
            continue
        missing.append(rel)
    if missing:
        raise LegacyTableScanError(
            "Allow-list entries not found:\n  " + "\n  ".join(missing)
        )


def test_legacy_inventory_coverage() -> None:
    """Verify the inventory covers every table referenced in schema.py."""
    schema_path = SRC_ROOT / "store" / "schema.py"
    schema_text = schema_path.read_text(encoding="utf-8")
    schema_tables: set[str] = set()
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)", schema_text):
        schema_tables.add(m.group(1))

    # Also add tables known from the full LEGACY_STATE_TABLES list
    event_log_tables: set[str] = set()
    # LEGACY_STATE_TABLES frozenset in event_store_cutover.py contains
    # the full list of tables to delete on cutover.  Parse the python literal
    # so the CI test stays synchronized with the authoritative list.
    cutover_path = SRC_ROOT / "store" / "event_store_cutover.py"
    cutover_text = cutover_path.read_text(encoding="utf-8")
    # Find the 'LEGACY_STATE_TABLES = frozenset({...})' assignment
    m = re.search(
        r"LEGACY_STATE_TABLES\s*=\s*frozenset\(\s*\{(.*?)\}", cutover_text, re.DOTALL
    )
    if m:
        for quoted in re.findall(r'"([^"]+)"', m.group(1)):
            event_log_tables.add(quoted)

    inventory_names = {t.table for t in all_legacy_tables()}
    covered = schema_tables | event_log_tables

    # Remove kept tables from the comparison
    kept = {t.table for t in all_legacy_tables() if t.phase == "kept"}

    missing_from_inventory = (schema_tables - inventory_names) - kept
    if missing_from_inventory:
        raise LegacyTableScanError(
            "Legacy tables in schema.py missing from inventory:\n  "
            + "\n  ".join(sorted(missing_from_inventory))
        )
