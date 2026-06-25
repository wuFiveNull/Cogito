# cogito/agent/tools/builtin/file_edit.py
#
# Built-in tools: write_file, edit_file, apply_patch — safe file editing.
#
# All paths resolved through WorkspaceScopePort for security.

from __future__ import annotations

import hashlib
import os
import tempfile
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
from cogito.agent.ports.tools.sandbox import WorkspaceScopePort
from cogito.infrastructure.sandbox.file_path_guardian import FilePathToolGuardian


class WriteFileHandler:
    """Handler for write_file — atomically writes content to a file."""

    def __init__(
        self,
        *,
        workspace: WorkspaceScopePort | None = None,
        file_guard: FilePathToolGuardian | None = None,
        max_size: int = 1_000_000,
    ) -> None:
        self._workspace = workspace
        self._file_guard = file_guard
        self._max_size = max_size

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_file",
            description="Write content to a file. Creates the file if it doesn't exist. Uses atomic write to prevent corruption.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "description": "File path (relative to workspace or absolute)"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.LOCAL_MUTATION,
            risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=10.0, idempotent=False, parallel_safe=False,
            kind=ToolKind.EDIT, risk=ToolRisk.LOCAL_WRITE,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_SESSION,
            limits=ToolLimits(timeout_seconds=10.0, max_result_chars=500),
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        path = str(arguments.get("path", ""))
        content = str(arguments.get("content", ""))

        resolved = self._resolve_path(path)
        if not resolved.get("allowed", True):
            return resolved
        filepath = resolved["absolute"]

        # FilePathGuardian check
        if self._file_guard is not None:
            guard_result = self._file_guard.check_tool_call("write_file", {"file_path": path})
            if guard_result.is_blocked:
                return {"error": {"code": "PATH_BLOCKED", "message": guard_result.reason}}

        if len(content.encode("utf-8")) > self._max_size:
            return {"error": {"code": "FILE_TOO_LARGE", "message": f"Content exceeds {self._max_size} bytes"}}

        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, filepath)
            return {"succeeded": True, "path": path, "size_chars": len(content)}
        except Exception as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return {"error": {"code": "WRITE_ERROR", "message": str(exc)}}

    def _resolve_path(self, path: str) -> dict:
        if self._workspace is not None:
            r = self._workspace.resolve_write(path)
            return {"allowed": r.within_workspace, "absolute": r.absolute}
        abs_path = os.path.abspath(path)
        return {"allowed": True, "absolute": abs_path}


class EditFileHandler:
    """Handler for edit_file — edits a file by replacing old text with new text."""

    def __init__(self, *, workspace: WorkspaceScopePort | None = None, file_guard: FilePathToolGuardian | None = None) -> None:
        self._workspace = workspace
        self._file_guard = file_guard

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="edit_file",
            description="Edit a file by replacing old_text with new_text. The old_text must match exactly once.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "description": "File path"},
                    "old_text": {"type": "string", "description": "Exact text to replace (must match once)"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.LOCAL_MUTATION, risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=10.0, idempotent=False, parallel_safe=False,
            kind=ToolKind.EDIT, risk=ToolRisk.LOCAL_WRITE,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_SESSION,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        path = str(arguments.get("path", ""))
        old_text = str(arguments.get("old_text", ""))
        new_text = str(arguments.get("new_text", ""))

        resolved = self._resolve_path(path)
        if not resolved.get("allowed", True):
            return resolved
        filepath = resolved["absolute"]

        if self._file_guard is not None:
            guard_result = self._file_guard.check_tool_call("edit_file", {"file_path": path})
            if guard_result.is_blocked:
                return {"error": {"code": "PATH_BLOCKED", "message": guard_result.reason}}

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            count = content.count(old_text)
            if count == 0:
                return {"error": {"code": "TEXT_NOT_FOUND", "message": "old_text not found in file"}}
            if count > 1:
                return {"error": {"code": "MULTIPLE_MATCHES", "message": f"old_text matches {count} times"}}
            new_content = content.replace(old_text, new_text, 1)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(new_content)
            return {"succeeded": True, "path": path}
        except FileNotFoundError:
            return {"error": {"code": "FILE_NOT_FOUND", "message": f"File not found: {path}"}}
        except Exception as exc:
            return {"error": {"code": "EDIT_ERROR", "message": str(exc)}}

    def _resolve_path(self, path: str) -> dict:
        if self._workspace is not None:
            r = self._workspace.resolve_read(path)
            return {"allowed": r.within_workspace, "absolute": r.absolute}
        return {"allowed": True, "absolute": os.path.abspath(path)}


class ApplyPatchHandler:
    """Handler for apply_patch — applies a unified diff to a file."""

    def __init__(self, *, workspace: WorkspaceScopePort | None = None, file_guard: FilePathToolGuardian | None = None) -> None:
        self._workspace = workspace
        self._file_guard = file_guard

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="apply_patch",
            description="Apply a unified diff/patch to a file. The patch must be a valid unified diff format.",
            input_schema={
                "type": "object", "properties": {
                    "path": {"type": "string", "minLength": 1, "description": "File to patch"},
                    "patch": {"type": "string", "minLength": 1, "description": "Unified diff content"},
                },
                "required": ["path", "patch"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.LOCAL_MUTATION, risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=15.0, idempotent=False, parallel_safe=False,
            kind=ToolKind.EDIT, risk=ToolRisk.LOCAL_WRITE,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_SESSION,
            limits=ToolLimits(timeout_seconds=15.0, max_result_chars=1_000),
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        path = str(arguments.get("path", ""))
        patch = str(arguments.get("patch", ""))

        resolved = self._resolve_path(path)
        if not resolved.get("allowed", True):
            return resolved
        filepath = resolved["absolute"]

        if self._file_guard is not None:
            guard_result = self._file_guard.check_tool_call("apply_patch", {"file_path": path})
            if guard_result.is_blocked:
                return {"error": {"code": "PATH_BLOCKED", "message": guard_result.reason}}

        import tempfile
        try:
            fd, tmp = tempfile.mkstemp(suffix=".patch")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(patch)
            import subprocess
            result = subprocess.run(
                ["patch", filepath],
                input=patch.encode("utf-8"),
                capture_output=True, timeout=15,
            )
            if result.returncode == 0:
                return {"succeeded": True, "path": path, "stdout": result.stdout.decode("utf-8", errors="replace")[:500]}
            else:
                return {"error": {"code": "PATCH_FAILED", "message": result.stderr.decode("utf-8", errors="replace")[:500]}}
        except FileNotFoundError:
            return {"error": {"code": "PATCH_NOT_FOUND", "message": "patch command not available on this system"}}
        except Exception as exc:
            return {"error": {"code": "PATCH_ERROR", "message": str(exc)}}
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _resolve_path(self, path: str) -> dict:
        if self._workspace is not None:
            r = self._workspace.resolve_read(path)
            return {"allowed": r.within_workspace, "absolute": r.absolute}
        return {"allowed": True, "absolute": os.path.abspath(path)}
