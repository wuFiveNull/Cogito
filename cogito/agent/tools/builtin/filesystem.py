# cogito/agent/tools/builtin/filesystem.py
#
# Built-in tools: read_file, list_dir — safe filesystem operations.
#
# All file paths are resolved through WorkspaceScopePort to prevent
# path traversal, symlink escapes, and other filesystem attacks.

from __future__ import annotations

import os
from typing import Mapping

from cogito.agent.domain.tools import (
    ToolConcurrencyMode,
    ToolDefinition,
    ToolKind,
    ToolLimits,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)
from cogito.agent.ports.tools.registry import ToolHandler
from cogito.agent.ports.tools.sandbox import WorkspaceScopePort
from cogito.infrastructure.sandbox.file_path_guardian import FilePathToolGuardian


class ReadFileHandler:
    """Handler for read_file — reads file contents from the workspace."""

    def __init__(
        self,
        *,
        workspace: WorkspaceScopePort | None = None,
        file_guard: FilePathToolGuardian | None = None,
        max_chars: int = 50_000,
    ) -> None:
        self._workspace = workspace
        self._file_guard = file_guard
        self._max_chars = max_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            description="Read the contents of a file from the workspace. Returns the file content or an error if the file doesn't exist.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to the file (relative to workspace or absolute)",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100_000,
                        "description": "Maximum number of characters to read (optional)",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=10.0,
            idempotent=True,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
            limits=ToolLimits(timeout_seconds=10.0, max_result_chars=50_000),
            always_visible=True,
        )

    async def execute(
        self,
        *,
        arguments: Mapping[str, object],
        context: Mapping[str, object],
    ) -> dict[str, object]:
        path = str(arguments.get("path", ""))
        limit = arguments.get("limit")

        # Resolve path
        resolved = self._resolve_path(path)
        if not resolved.get("allowed", True):
            return resolved

        # FilePathGuardian check
        if self._file_guard is not None:
            guard_result = self._file_guard.check_tool_call("read_file", {"file_path": path})
            if guard_result.is_blocked:
                return {"error": {"code": "PATH_BLOCKED", "message": guard_result.reason}}

        filepath = resolved["absolute"]
        try:
            if not os.path.isfile(filepath):
                return {"error": {"code": "FILE_NOT_FOUND", "message": f"File not found: {path}"}}

            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            result_chars = limit if isinstance(limit, int) else self._max_chars
            truncated = len(content) > result_chars
            if truncated:
                content = content[:result_chars] + f"\n... [truncated from {len(content)} chars]"

            return {
                "content": content,
                "size_chars": len(content),
                "truncated": truncated,
            }

        except PermissionError:
            return {"error": {"code": "PERMISSION_DENIED", "message": f"Permission denied: {path}"}}
        except Exception as exc:
            return {"error": {"code": "READ_ERROR", "message": str(exc)}}

    def _resolve_path(self, path: str) -> dict[str, object]:
        """Resolve a path through the workspace scope."""
        if self._workspace is not None:
            resolved = self._workspace.resolve_read(path)
            return {
                "allowed": resolved.within_workspace,
                "absolute": resolved.absolute,
                "exists": resolved.exists,
            }
        # No workspace scope — use raw path (development mode)
        import os
        abs_path = os.path.abspath(path)
        return {"allowed": True, "absolute": abs_path, "exists": os.path.exists(abs_path)}


class ListDirHandler:
    """Handler for list_dir — lists directory contents."""

    def __init__(
        self,
        *,
        workspace: WorkspaceScopePort | None = None,
        file_guard: FilePathToolGuardian | None = None,
    ) -> None:
        self._workspace = workspace
        self._file_guard = file_guard

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_dir",
            description="List files and directories in a given path. Returns names, types, and sizes.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Directory path (relative to workspace or absolute)",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=10.0,
            idempotent=True,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
            always_visible=True,
        )

    async def execute(
        self,
        *,
        arguments: Mapping[str, object],
        context: Mapping[str, object],
    ) -> dict[str, object]:
        path = str(arguments.get("path", ""))

        resolved = self._resolve_path(path)
        if not resolved.get("allowed", True):
            return resolved

        # FilePathGuardian check
        if self._file_guard is not None:
            guard_result = self._file_guard.check_tool_call("list_dir", {"path": path})
            if guard_result.is_blocked:
                return {"error": {"code": "PATH_BLOCKED", "message": guard_result.reason}}

        dirpath = resolved["absolute"]
        try:
            if not os.path.isdir(dirpath):
                return {"error": {"code": "DIR_NOT_FOUND", "message": f"Directory not found: {path}"}}

            entries = []
            for entry in os.scandir(dirpath):
                try:
                    info = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "type": "directory" if entry.is_dir() else "file" if entry.is_file() else "other",
                        "size": info.st_size if not entry.is_dir() else None,
                        "modified": info.st_mtime,
                    })
                except OSError:
                    entries.append({"name": entry.name, "type": "unknown"})

            entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))

            return {
                "entries": entries,
                "total": len(entries),
                "path": path,
            }

        except PermissionError:
            return {"error": {"code": "PERMISSION_DENIED", "message": f"Permission denied: {path}"}}
        except Exception as exc:
            return {"error": {"code": "LIST_ERROR", "message": str(exc)}}

    def _resolve_path(self, path: str) -> dict[str, object]:
        return ReadFileHandler._resolve_path_static(path, self._workspace)

    @staticmethod
    def _resolve_path_static(path: str, workspace: WorkspaceScopePort | None) -> dict[str, object]:
        if workspace is not None:
            resolved = workspace.resolve_read(path)
            return {"allowed": resolved.within_workspace, "absolute": resolved.absolute}
        abs_path = os.path.abspath(path)
        return {"allowed": True, "absolute": abs_path}


class ReadArtifactHandler:
    """Handler for read_artifact — reads stored artifacts by ID."""

    def __init__(self, artifact_store: object | None = None) -> None:
        self._artifact_store = artifact_store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_artifact",
            description="Read a stored artifact by its artifact_id. Returns the content with metadata.",
            input_schema={
                "type": "object", "properties": {
                    "artifact_id": {"type": "string", "minLength": 1, "description": "Artifact ID to read"},
                    "offset": {"type": "integer", "minimum": 0, "description": "Byte offset for partial read"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100_000, "description": "Max bytes to return"},
                },
                "required": ["artifact_id"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE, risk_level=ToolRiskLevel.LOW,
            timeout_seconds=10.0, idempotent=True, parallel_safe=True,
            kind=ToolKind.READ, risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        artifact_id = str(arguments.get("artifact_id", ""))
        offset = int(arguments.get("offset", 0))
        limit = arguments.get("limit")

        if self._artifact_store is None:
            return {"error": {"code": "ARTIFACT_STORE_NOT_CONFIGURED", "message": "No artifact store available"}}

        read_limit = int(limit) if limit is not None else None
        data = await self._artifact_store.read(artifact_id, offset=offset, limit=read_limit)
        if data is None:
            return {"error": {"code": "ARTIFACT_NOT_FOUND", "message": f"Artifact not found: {artifact_id}"}}

        return {"content": data.decode("utf-8", errors="replace"), "size_bytes": len(data), "artifact_id": artifact_id}

