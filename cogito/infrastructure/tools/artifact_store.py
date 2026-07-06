# cogito/infrastructure/tools/artifact_store.py
#
# FileArtifactStore — filesystem-backed artifact storage for large tool results.
#
# Design rules (see tool-system-spec §15):
#   - Artifacts stored as files in a workspace-relative directory.
#   - SHA-256 deduplication: same content → same artifact_id.
#   - TTL-based cleanup: expired artifacts are periodically removed.
#   - Artifact files are read-only after creation (chmod 0444).

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cogito.agent.domain.tools import ArtifactRef
from cogito.agent.ports.tools.artifacts import ToolArtifactStorePort

logger = logging.getLogger(__name__)


class FileArtifactStore:
    """Filesystem-based artifact storage.

    Artifacts are stored as::

        <store_root>/<sha256_prefix>/<sha256_suffix>.dat

    Example with ``store_root = ".workspace/artifacts"``::

        .workspace/artifacts/a1/b2c3d4e5f6...dat
    """

    def __init__(
        self,
        store_root: str = ".workspace/artifacts",
        *,
        default_ttl_seconds: int = 604_800,  # 7 days
        max_artifact_bytes: int = 100 * 1024 * 1024,  # 100 MB
    ) -> None:
        self._root = Path(store_root)
        self._default_ttl = default_ttl_seconds
        self._max_bytes = max_artifact_bytes

    async def store(
        self,
        *,
        data: bytes,
        media_type: str,
        name: str | None = None,
        ttl_seconds: int | None = None,
    ) -> ArtifactRef:
        """Store data as an artifact. Returns an ArtifactRef."""
        if len(data) > self._max_bytes:
            raise ValueError(
                f"Artifact size {len(data)} exceeds max {self._max_bytes}",
            )

        sha256 = hashlib.sha256(data).hexdigest()
        artifact_id = sha256[:32]  # First 32 chars as stable ID

        # Check if already stored (deduplication)
        filepath = self._path_for(sha256)
        if not filepath.exists():
            self._root.mkdir(parents=True, exist_ok=True)
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(data)
            try:
                filepath.chmod(0o444)  # Make read-only; may fail on Windows
            except OSError:
                pass

        expires_at = None
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        return ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            size_bytes=len(data),
            sha256=sha256,
            storage_uri=str(filepath),
            name=name,
            expires_at=expires_at,
        )

    async def read(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> bytes | None:
        """Read artifact data by ID. Supports offset/limit for partial reads."""
        filepath = self._find_by_id(artifact_id)
        if filepath is None or not filepath.exists():
            return None

        data = filepath.read_bytes()
        if offset > 0:
            data = data[offset:]
        if limit is not None:
            data = data[:limit]
        return data

    async def delete(self, artifact_id: str) -> bool:
        """Delete an artifact by ID."""
        filepath = self._find_by_id(artifact_id)
        if filepath is None or not filepath.exists():
            return False
        try:
            filepath.chmod(0o644)  # Make writable before deleting on Windows
            filepath.unlink()
            return True
        except OSError:
            return False

    async def cleanup_expired(self) -> int:
        """Remove expired artifacts. Returns count of deleted files."""
        count = 0
        now = time.time()

        for filepath in self._root.rglob("*.dat"):
            try:
                mtime = filepath.stat().st_mtime
                age_seconds = now - mtime
                if age_seconds > self._default_ttl:
                    filepath.unlink()
                    count += 1
            except OSError:
                continue

        # Clean up empty prefix directories
        for prefix_dir in self._root.iterdir():
            if prefix_dir.is_dir() and not any(prefix_dir.iterdir()):
                try:
                    prefix_dir.rmdir()
                except OSError:
                    pass

        return count

    # ── Internal ──────────────────────────────────────────────────────

    def _path_for(self, sha256: str) -> Path:
        """Compute file path from SHA-256 hash: prefix/suffix.dat."""
        prefix = sha256[:2]
        suffix = sha256[2:]
        return self._root / prefix / f"{suffix}.dat"

    def _find_by_id(self, artifact_id: str) -> Path | None:
        """Find an artifact file by its ID (first 32 hex chars of SHA-256)."""
        # Search all prefix directories
        if not self._root.exists():
            return None

        for prefix_dir in self._root.iterdir():
            if not prefix_dir.is_dir():
                continue
            for filepath in prefix_dir.iterdir():
                if filepath.suffix == ".dat":
                    full_sha = prefix_dir.name + filepath.stem
                    if full_sha.startswith(artifact_id):
                        return filepath
        return None
