# cogito/infrastructure/sandbox/workspace_scope.py
#
# DefaultWorkspaceScope — filesystem path security boundary.
#
# Design rules (see tool-system-spec §17.1):
#   - All file paths are resolved through this scope.
#   - Must prevent: `..` traversal, symlink escape, case-insensitivity
#     bypass, Windows UNC/ADS/device paths, TOCTOU.
#   - `resolve_read` for read-only access, `resolve_write` for writes.
#   - Always deny paths outside the workspace root.

from __future__ import annotations

import os
import platform
from pathlib import Path

from cogito.agent.ports.tools.sandbox import ResolvedPath, WorkspaceScopePort


class DefaultWorkspaceScope:
    """Platform-aware filesystem security boundary.

    ``workspace_root`` is the allowed base directory (absolute).
    ``allowed_symlinks``, when True, still resolves the target but
    flags ``is_symlink`` for the caller to decide.

    Security checks (in order):
      1. Reject empty / null paths.
      2. Reject Windows device paths (COM, PRN, NUL, etc.).
      3. Reject Windows UNC paths (\\host\share).
      4. Reject NTFS Alternate Data Streams (file:stream).
      5. Normalize path separators.
      6. Resolve to absolute, rejecting ``..`` traversal beyond root.
      7. If symlink encountered, flag it (policy decides).
      8. On Windows, compare resolved path with workspace root case-insensitively.
      9. Verify resolved path starts with workspace root.
    """

    def __init__(
        self,
        workspace_root: str,
        *,
        follow_symlinks: bool = False,
        max_depth: int = 40,
    ) -> None:
        self._root = Path(workspace_root).resolve()
        self._follow_symlinks = follow_symlinks
        self._max_depth = max_depth
        self._is_windows = platform.system() == "Windows"

    # ── Public API ──────────────────────────────────────────────────────

    def resolve_read(self, path: str) -> ResolvedPath:
        return self._resolve(path, for_write=False)

    def resolve_write(self, path: str) -> ResolvedPath:
        return self._resolve(path, for_write=True)

    # ── Internal ────────────────────────────────────────────────────────

    def _resolve(self, path: str, *, for_write: bool) -> ResolvedPath:
        """Resolve a path through the security boundary."""
        # 1. Empty / null
        if not path or not path.strip():
            return self._deny("path is empty")

        # 2-4. Windows-specific checks
        if self._is_windows:
            violation = self._check_windows_path(path)
            if violation:
                return self._deny(violation)

        # 5. Normalize separators
        normalized = path.replace("\\", "/") if self._is_windows else path

        # 6. Resolve to absolute, preventing .. beyond root
        resolved = self._resolve_safe(normalized)
        if resolved is None:
            return self._deny("path traversal detected")

        # 7. Symlink check
        is_symlink = False
        if resolved.is_symlink():
            is_symlink = True
            if self._follow_symlinks:
                try:
                    resolved = Path(os.path.realpath(resolved))
                except (OSError, RuntimeError):
                    return self._deny("unable to resolve symlink")
            else:
                return self._deny("symlinks not allowed")

        # 8-9. Verify within workspace root
        if not self._is_within_root(resolved):
            return self._deny("path outside workspace")

        exists = resolved.exists()
        return ResolvedPath(
            absolute=str(resolved),
            within_workspace=True,
            exists=exists,
            is_symlink=is_symlink,
        )

    def _resolve_safe(self, path: str) -> Path | None:
        """Resolve a path to absolute, rejecting upward traversal beyond root.

        Returns None if traversal is detected.
        """
        p = Path(path)
        if not p.is_absolute():
            p = self._root / p

        try:
            p = p.resolve()
        except (OSError, RuntimeError):
            return None

        # Check depth (prevent symlink loops / deep recursion)
        parts = p.parts
        if len(parts) > self._max_depth:
            return None

        # Verify we didn't escape root via '..'
        try:
            p.relative_to(self._root)
        except ValueError:
            return None

        return p

    def _is_within_root(self, resolved: Path) -> bool:
        """Check resolved path is within workspace root, case-aware."""
        try:
            resolved.relative_to(self._root)
            return True
        except ValueError:
            pass

        # Windows case-insensitive fallback
        if self._is_windows:
            try:
                resolved_str = str(resolved).lower()
                root_str = str(self._root).lower()
                return resolved_str.startswith(root_str) and (
                    len(resolved_str) == len(root_str)
                    or resolved_str[len(root_str)] in ("\\", "/")
                )
            except Exception:
                return False

        return False

    @staticmethod
    def _check_windows_path(path: str) -> str | None:
        """Windows-specific path security checks.

        Returns a violation description or None if safe.
        """
        # Device paths: COM1-9, LPT1-9, PRN, AUX, NUL, CON
        name = Path(path).stem.upper() if "." in path else Path(path).name.upper()
        device_names = {
            "COM1", "COM2", "COM3", "COM4", "COM5",
            "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
            "LPT6", "LPT7", "LPT8", "LPT9",
            "PRN", "AUX", "NUL", "CON",
        }
        if name in device_names:
            return f"Windows device path rejected: {name}"

        # UNC paths
        if path.startswith("\\\\") or path.startswith("//"):
            return "UNC paths are not allowed"

        # Alternate Data Streams
        if ":" in path.replace(":\\", "").replace(":/", ""):
            return "Alternate Data Streams are not allowed"

        return None

    @staticmethod
    def _deny(reason: str = "path rejected") -> ResolvedPath:
        return ResolvedPath(
            absolute="",
            within_workspace=False,
            exists=False,
            is_symlink=False,
        )
