# cogito/infrastructure/sandbox/audit_trail.py
#
# FileAuditTrail — append-only audit trail backed by JSONL files.
#
# Implements ToolAuditPort from the tool-system-spec.
#
# Design:
#   - Append-only JSONL format (one JSON object per line).
#   - Raw arguments are never stored; only hash + redacted summary.
#   - Automatic TTL-based cleanup of old records.
#   - Thread-safe writes via asyncio.Lock.

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from cogito.agent.ports.tools.audit import ToolAuditPort, ToolAuditRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditTrailConfig:
    """Configuration for FileAuditTrail.

    Attributes:
        path:          Directory path for audit log files.
        ttl_days:      Days to keep audit records (0 = never delete).
        flush_interval: Seconds between batch flushes to disk.
        max_file_size: Max bytes per file before rotation (0 = no limit).
    """
    path: str = ""
    ttl_days: int = 7
    flush_interval: float = 5.0
    max_file_size: int = 100 * 1024 * 1024  # 100 MB


class FileAuditTrail:
    """Append-only audit trail backed by JSONL files.

    Records are written to rotating JSONL files in the configured
    directory.  Old records beyond TTL are automatically cleaned up.
    """

    def __init__(
        self,
        config: AuditTrailConfig | None = None,
    ) -> None:
        self._config = config or AuditTrailConfig()
        self._lock = asyncio.Lock()
        self._buffer: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._base_path = Path(self._config.path) if self._config.path else Path.cwd() / ".audit"

        # Ensure directory exists
        self._base_path.mkdir(parents=True, exist_ok=True)

        # Start background flush task
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("Audit trail initialised: %s", self._base_path)

    # ── ToolAuditPort API ──────────────────────────────────────────

    async def record(self, record: ToolAuditRecord) -> None:
        """Record a single tool execution event."""
        entry = self._serialise(record)
        async with self._lock:
            self._buffer.append(entry)

    async def record_batch(self, records: tuple[ToolAuditRecord, ...]) -> None:
        """Record multiple tool execution events."""
        entries = [self._serialise(r) for r in records]
        async with self._lock:
            self._buffer.extend(entries)

    # ── Search / Query ─────────────────────────────────────────────

    async def query(
        self,
        *,
        tool_name: str | None = None,
        actor_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query audit records with filters.

        Returns a list of serialised records matching all filters.
        """
        results: list[dict[str, Any]] = []
        files = sorted(self._base_path.glob("audit-*.jsonl"), reverse=True)

        for fpath in files:
            if len(results) >= offset + limit:
                break
            try:
                with open(fpath, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            if tool_name and record.get("tool_name") != tool_name:
                                continue
                            if actor_id and record.get("actor_id") != actor_id:
                                continue
                            if status and record.get("status") != status:
                                continue
                            results.append(record)
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

        return results[offset:offset + limit]

    async def count(self) -> int:
        """Count total records across all audit files."""
        total = 0
        for fpath in self._base_path.glob("audit-*.jsonl"):
            try:
                with open(fpath, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            total += 1
            except OSError:
                continue
        return total

    async def cleanup_expired(self) -> int:
        """Delete audit files older than TTL. Returns count of files removed."""
        if self._config.ttl_days <= 0:
            return 0
        cutoff = time.time() - (self._config.ttl_days * 86400)
        removed = 0
        for fpath in self._base_path.glob("audit-*.jsonl"):
            try:
                if fpath.stat().st_mtime < cutoff:
                    fpath.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("Audit cleanup: removed %d expired files", removed)
        return removed

    async def close(self) -> None:
        """Flush buffer and stop flush loop."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush()

    # ── Internal ───────────────────────────────────────────────────

    def _serialise(self, record: ToolAuditRecord) -> str:
        """Serialise a ToolAuditRecord to a JSON line."""
        data = {
            "call_id": record.call_id,
            "turn_id": record.turn_id,
            "tool_name": record.tool_name,
            "actor_id": record.actor_id,
            "session_id": record.session_id,
            "status": record.status,
            "risk": record.risk,
            "started_at": record.started_at.isoformat(),
            "duration_ms": record.duration_ms,
            "arguments_hash": record.arguments_hash,
            "approval_id": record.approval_id,
            "error_code": record.error_code,
            "policy_reason_code": record.policy_reason_code,
            "artifact_ids": list(record.artifact_ids),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return json.dumps(data, ensure_ascii=False)

    def _current_file(self) -> Path:
        """Get the current audit log file path (with rotation)."""
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        return self._base_path / f"audit-{date_str}.jsonl"

    async def _flush(self) -> None:
        """Flush buffered records to disk."""
        async with self._lock:
            if not self._buffer:
                return
            entries = self._buffer[:]
            self._buffer.clear()

        fpath = self._current_file()
        try:
            with open(fpath, "a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(entry + "\n")
        except OSError as exc:
            logger.error("Audit trail write failed: %s", exc)

    async def _flush_loop(self) -> None:
        """Background loop that periodically flushes the buffer."""
        try:
            while True:
                await asyncio.sleep(self._config.flush_interval)
                await self._flush()
        except asyncio.CancelledError:
            await self._flush()
            raise
