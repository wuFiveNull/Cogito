# cogito/agent/tools/builtin/search_tools.py
#
# Built-in tools: glob_search, grep_search — filesystem search.

from __future__ import annotations

import fnmatch
import os
import re
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


class GlobSearchHandler:
    """Handler for glob_search — find files matching a glob pattern."""

    def __init__(self, *, workspace: WorkspaceScopePort | None = None, max_results: int = 100) -> None:
        self._workspace = workspace
        self._max_results = max_results

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="glob_search",
            description="Search for files matching a glob pattern. Supports * and ** wildcards.",
            input_schema={
                "type": "object", "properties": {
                    "pattern": {"type": "string", "minLength": 1, "description": "Glob pattern (e.g., **/*.py, src/**)"},
                    "base_dir": {"type": "string", "description": "Base directory (default: workspace root)"},
                },
                "required": ["pattern"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE, risk_level=ToolRiskLevel.LOW,
            timeout_seconds=15.0, idempotent=True, parallel_safe=True,
            kind=ToolKind.SEARCH, risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
            limits=ToolLimits(timeout_seconds=15.0, max_result_chars=10_000),
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        pattern = str(arguments.get("pattern", ""))
        base_dir = str(arguments.get("base_dir", "."))

        resolved = self._resolve_path(base_dir)
        if not resolved.get("allowed", True):
            return resolved
        root = resolved["absolute"]

        matches = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            entries = dirnames + filenames
            for entry in entries:
                rel_path = os.path.join(rel_dir, entry) if rel_dir != "." else entry
                if fnmatch.fnmatch(rel_path, pattern):
                    matches.append(rel_path)
                    if len(matches) >= self._max_results:
                        break
            if len(matches) >= self._max_results:
                break

        return {"results": sorted(matches), "total": len(matches), "pattern": pattern}

    def _resolve_path(self, path: str) -> dict:
        if self._workspace is not None:
            r = self._workspace.resolve_read(path)
            return {"allowed": r.within_workspace, "absolute": r.absolute}
        return {"allowed": True, "absolute": os.path.abspath(path)}


class GrepSearchHandler:
    """Handler for grep_search — search file contents with a regex pattern."""

    def __init__(self, *, workspace: WorkspaceScopePort | None = None, max_results: int = 50) -> None:
        self._workspace = workspace
        self._max_results = max_results

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="grep_search",
            description="Search file contents for a regex pattern. Returns matching files with line numbers and context.",
            input_schema={
                "type": "object", "properties": {
                    "pattern": {"type": "string", "minLength": 1, "description": "Regular expression pattern"},
                    "include": {"type": "string", "description": "File glob pattern to include (e.g., *.py)"},
                    "path": {"type": "string", "description": "Directory to search (default: workspace root)"},
                },
                "required": ["pattern"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE, risk_level=ToolRiskLevel.LOW,
            timeout_seconds=30.0, idempotent=True, parallel_safe=True,
            kind=ToolKind.SEARCH, risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        pattern_str = str(arguments.get("pattern", ""))
        include = str(arguments.get("include", "*"))
        base_path = str(arguments.get("path", "."))

        resolved = self._resolve_path(base_path)
        if not resolved.get("allowed", True):
            return resolved
        root = resolved["absolute"]

        try:
            regex = re.compile(pattern_str, re.IGNORECASE)
        except re.error as exc:
            return {"error": {"code": "INVALID_REGEX", "message": str(exc)}}

        results = []
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                if not fnmatch.fnmatch(filename, include):
                    continue
                filepath = os.path.join(dirpath, filename)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, 1):
                            if regex.search(line):
                                results.append({
                                    "file": os.path.relpath(filepath, root),
                                    "line": line_no,
                                    "content": line.rstrip()[:200],
                                })
                                if len(results) >= self._max_results:
                                    return {"results": results, "total": len(results), "pattern": pattern_str}
                except (OSError, UnicodeDecodeError):
                    continue

        return {"results": results, "total": len(results), "pattern": pattern_str}

    def _resolve_path(self, path: str) -> dict:
        if self._workspace is not None:
            r = self._workspace.resolve_read(path)
            return {"allowed": r.within_workspace, "absolute": r.absolute}
        return {"allowed": True, "absolute": os.path.abspath(path)}
