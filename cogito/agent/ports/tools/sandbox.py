# cogito/agent/ports/tools/sandbox.py
#
# Tool Sandbox Ports — security boundaries for tool execution.
#
# Design rules (see tool-system-spec §17, §18):
#   - All file-system tools go through WorkspaceScopePort for path resolution.
#   - Shell and network tools go through ToolSandboxPort for OS-level sandbox.
#   - Path resolution must prevent traversal, symlink escape, and TOCTOU.
#   - Sandbox is always fail-closed: deny by default.

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A resolved, validated filesystem path."""
    absolute: str
    within_workspace: bool
    exists: bool = False
    is_symlink: bool = False


class WorkspaceScopePort(Protocol):
    """Resolves and validates file paths within the workspace boundary."""

    def resolve_read(self, path: str) -> ResolvedPath:
        ...

    def resolve_write(self, path: str) -> ResolvedPath:
        ...


@dataclass(frozen=True, slots=True)
class SandboxContext:
    command: str
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class ToolSandboxPort(Protocol):
    """OS-level sandbox for shell tool execution."""

    async def run(
        self,
        context: SandboxContext,
    ) -> SandboxResult:
        ...

    async def check_allowed(
        self,
        command: str,
    ) -> bool:
        ...
