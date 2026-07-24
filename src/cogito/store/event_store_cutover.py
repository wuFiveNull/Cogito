"""Verified, candidate-database contract cutover for the Event Store.

The normal migration runner must stay additive: SQLite DDL can commit before a
later validation error.  This module therefore performs the irreversible
contract phase only on a disposable candidate copy, then replaces the live
database after the source snapshot has been verified.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cogito.infrastructure.backup import BackupManifest, BackupService
from cogito.store.legacy_event_backfill import LegacyEventBackfill
from cogito.store.migration import migrate


class EventStoreCutoverError(RuntimeError):
    """Raised when a candidate cannot safely become the Event-only database."""


# Explicit rather than pattern based: payload objects and schema metadata are
# intentionally retained, while every mutable application projection is gone.
LEGACY_STATE_TABLES = frozenset(
    {
        "principals", "endpoints", "conversations", "sessions", "messages",
        "content_parts", "message_revisions", "turns", "run_attempts",
        "turn_checkpoints", "tasks", "task_attempts", "model_calls", "tool_calls",
        "approvals", "deliveries", "delivery_attempts", "delivery_receipts",
        "outbox_events", "events", "traces", "spans", "side_effect_receipts",
        "connectors", "connector_cursors", "connector_raw_items", "connector_items",
        "schedules", "scheduled_fires", "proactive_candidates", "proactive_decisions",
        "proactive_decisions_v2", "proactive_policies", "proactive_cadence_state",
        "digests", "digest_items", "drift_runs", "drift_skill_state",
        "task_checkpoints", "memory_items", "memory_embeddings", "memory_relations",
        "memory_sources", "memory_sources_v2", "memory_signals", "knowledge_resources",
        "knowledge_documents", "knowledge_segments", "knowledge_embeddings",
        "context_snapshots", "event_consumptions", "commands", "audit_records",
        "ingestion_batches", "multimodal_links", "sticker_metadata",
    }
)
_MARKER_TABLE = "_event_store_cutover"


@dataclass(frozen=True, slots=True)
class CutoverReport:
    backup: BackupManifest
    imported: dict[str, int]
    validated: dict[str, bool]
    candidate_path: Path | None
    applied: bool


def is_cutover_database(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_MARKER_TABLE,)
    ).fetchone() is not None


def assert_event_store_runtime_ready(conn: sqlite3.Connection) -> None:
    """Reject a partially switched database before workers receive work."""
    if not is_cutover_database(conn):
        return
    present = _present_legacy_tables(conn)
    if present:
        raise EventStoreCutoverError(
            "event-store cutover marker exists but legacy tables remain: "
            + ", ".join(sorted(present))
        )
    marker = conn.execute(
        f"SELECT validated_at FROM {_MARKER_TABLE} ORDER BY validated_at DESC LIMIT 1"
    ).fetchone()
    if marker is None:
        raise EventStoreCutoverError("event-store cutover marker is incomplete")


class EventStoreCutover:
    """Create, validate and optionally atomically install an Event-only copy."""

    def __init__(self, db_path: str | Path, *, home: str | Path, payload_root: str | Path | None = None) -> None:
        self._db_path = Path(db_path).resolve()
        self._home = Path(home).resolve()
        self._payload_root = Path(payload_root).resolve() if payload_root else None

    def run(self, *, apply: bool = False) -> CutoverReport:
        if not self._db_path.is_file():
            raise EventStoreCutoverError(f"database does not exist: {self._db_path}")
        if self._db_path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            raise EventStoreCutoverError("cutover requires a file-backed SQLite database")

        source = sqlite3.connect(str(self._db_path), isolation_level=None)
        source.row_factory = sqlite3.Row
        candidate_path: Path | None = None
        try:
            # Prove that maintenance has exclusive access before snapshotting.
            # SQLite's online-backup API cannot run through the same connection
            # while it owns an EXCLUSIVE transaction, hence the lock is a
            # preflight and the following backup is SQLite's atomic snapshot.
            # Operators must stop workers before invoking this command.
            source.execute("PRAGMA busy_timeout=30000")
            source.execute("BEGIN EXCLUSIVE")
            source.execute("COMMIT")
            backup = BackupService(self._home, source).create(payload_root=self._payload_root)
            if not BackupService(self._home, source).verify(backup.backup_id):
                raise EventStoreCutoverError("backup integrity check failed")

            candidate_path = self._candidate_path()
            shutil.copy2(backup.sqlite_snapshot_uri, candidate_path)
        except Exception:
            try:
                source.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            source.close()

        candidate = sqlite3.connect(str(candidate_path))
        candidate.row_factory = sqlite3.Row
        try:
            # The normal runner's maintenance mode includes 0069, whose old
            # delivery contract would erase source rows before they are
            # imported.  This module owns every destructive action below.
            migrate(candidate, maintenance=False)
            importer = LegacyEventBackfill(candidate)
            imported = importer.import_all()
            validated = importer.verify()
            if not all(validated.values()):
                raise EventStoreCutoverError(f"legacy import validation failed: {validated}")
            self._validate_candidate(candidate)
            self._contract(candidate, backup)
            self._validate_candidate(candidate, contracted=True)
            candidate.commit()
        except Exception:
            candidate.close()
            candidate_path.unlink(missing_ok=True)
            raise
        else:
            candidate.close()

        if apply:
            self._atomic_replace(candidate_path)
            candidate_path = None
        return CutoverReport(
            backup=backup,
            imported=imported,
            validated=validated,
            candidate_path=candidate_path,
            applied=apply,
        )

    def _candidate_path(self) -> Path:
        self._home.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f"{self._db_path.stem}.event-cutover.", suffix=".db", dir=self._home)
        os.close(fd)
        return Path(raw)

    @staticmethod
    def _validate_candidate(conn: sqlite3.Connection, *, contracted: bool = False) -> None:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise EventStoreCutoverError(f"candidate integrity check failed: {integrity}")
        event_log = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_log'"
        ).fetchone()
        if event_log is None:
            raise EventStoreCutoverError("candidate has no event_log")
        if contracted:
            present = _present_legacy_tables(conn)
            if present:
                raise EventStoreCutoverError(
                    "contract did not remove legacy tables: " + ", ".join(sorted(present))
                )
            assert_event_store_runtime_ready(conn)

    @staticmethod
    def _contract(conn: sqlite3.Connection, backup: BackupManifest) -> None:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in sorted(_present_legacy_tables(conn)):
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_MARKER_TABLE} ("
            "cutover_id TEXT PRIMARY KEY, backup_id TEXT NOT NULL, backup_sha256 TEXT NOT NULL, "
            "validated_at INTEGER NOT NULL)"
        )
        backup_hash = hashlib.sha256(backup.sqlite_snapshot_uri.encode("utf-8")).hexdigest()
        conn.execute(
            f"INSERT INTO {_MARKER_TABLE} (cutover_id, backup_id, backup_sha256, validated_at) "
            "VALUES (?, ?, ?, ?)",
            ("event-store-v1", backup.backup_id, backup_hash, int(datetime.now(UTC).timestamp() * 1000)),
        )

    def _atomic_replace(self, candidate_path: Path) -> None:
        # A verified immutable backup already exists.  Keep the original file
        # untouched until this final same-filesystem replacement succeeds.
        if candidate_path.parent != self._db_path.parent:
            staged = self._db_path.with_suffix(self._db_path.suffix + ".event-cutover")
            shutil.copy2(candidate_path, staged)
            candidate_path.unlink(missing_ok=True)
            candidate_path = staged
        os.replace(candidate_path, self._db_path)


def _present_legacy_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows if str(row[0]) in LEGACY_STATE_TABLES}
