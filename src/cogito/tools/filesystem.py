"""Workspace-bounded filesystem tools."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.unified_patch import apply_hunks, parse_unified_diff
from cogito.capability.workspace import WorkspaceBoundary


def create_tool_defs(boundary: WorkspaceBoundary) -> list[ToolDef]:
    def reconcile_file(receipt: dict[str, Any]) -> dict[str, str]:
        try:
            summary = json.loads(str(receipt.get("summary", "{}")))
            changes = list(summary.get("changes") or [])
            if not changes and summary.get("path"):
                changes = [
                    {
                        "old_path": summary.get("path"),
                        "new_path": summary.get("path"),
                        "before_sha256": summary.get("before_sha256"),
                        "after_sha256": summary.get("after_sha256"),
                    }
                ]
            if not changes:
                return {"status": "manual_required", "summary": "receipt has no file manifest"}
            after_matches = []
            before_matches = []
            for change in changes:
                old_path = change.get("old_path")
                new_path = change.get("new_path")
                before_hash = change.get("before_sha256")
                after_hash = change.get("after_sha256")
                after_matches.append(_matches_hash(boundary, new_path, after_hash))
                before_matches.append(_matches_hash(boundary, old_path, before_hash))
            if all(after_matches):
                return {"status": "succeeded", "summary": "workspace matches after hashes"}
            if all(before_matches):
                return {"status": "not_executed", "summary": "workspace matches before hashes"}
        except (TypeError, ValueError, OSError):
            pass
        return {"status": "manual_required", "summary": "workspace state is ambiguous"}

    async def read_file(args: dict[str, Any], _: ToolContext) -> str:
        path = boundary.resolve(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        max_bytes = min(int(args.get("max_bytes", 100_000)), boundary.max_read_bytes)
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, min(int(args.get("limit", 2_000)), 10_000))
        selected: list[str] = []
        total_lines = 0
        output_bytes = 0
        byte_limited = False
        for number, line in enumerate(boundary.iter_text_lines(args["path"]), 1):
            total_lines = number
            if number < offset or len(selected) >= limit or byte_limited:
                continue
            rendered = f"{number}: {line.rstrip(chr(10) + chr(13))}"
            addition = rendered.encode("utf-8")
            separator_size = 1 if selected else 0
            if output_bytes + separator_size + len(addition) > max_bytes:
                byte_limited = True
                continue
            selected.append(rendered)
            output_bytes += separator_size + len(addition)
        text = "\n".join(selected)
        truncated = byte_limited or offset - 1 + len(selected) < total_lines
        return json.dumps(
            {
                "path": boundary.relative(path),
                "content": text,
                "start_line": offset,
                "returned_lines": len(selected),
                "total_lines": total_lines,
                "size_bytes": path.stat().st_size,
                "next_offset": offset + len(selected) if truncated and selected else None,
                "truncated": truncated,
            },
            ensure_ascii=False,
        )

    async def list_directory(args: dict[str, Any], _: ToolContext) -> str:
        base = boundary.resolve(args.get("path", "."))
        if not base.is_dir():
            raise ValueError("path is not a directory")
        limit = max(1, min(int(args.get("limit", 200)), 1_000))
        entries = []
        for item in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            try:
                rel = boundary.relative(item)
                boundary.resolve(rel)
            except ValueError:
                continue
            entries.append(
                {
                    "path": rel,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )
            if len(entries) >= limit:
                break
        return json.dumps({"entries": entries, "truncated": len(entries) >= limit})

    async def glob_tool(args: dict[str, Any], _: ToolContext) -> str:
        base = boundary.resolve(args.get("path", "."))
        pattern = str(args["pattern"])
        limit = max(1, min(int(args.get("limit", 200)), 1_000))
        matches = []
        for item in base.rglob("*"):
            rel_base = item.relative_to(base).as_posix()
            if fnmatch.fnmatch(rel_base, pattern):
                try:
                    matches.append(boundary.relative(item))
                except ValueError:
                    continue
            if len(matches) >= limit:
                break
        return json.dumps({"matches": matches, "truncated": len(matches) >= limit})

    async def grep_tool(args: dict[str, Any], _: ToolContext) -> str:
        base = boundary.resolve(args.get("path", "."))
        regex = re.compile(str(args["pattern"]))
        include = str(args.get("include", "*"))
        limit = max(1, min(int(args.get("limit", 200)), 1_000))
        files = [base] if base.is_file() else base.rglob("*")
        matches = []
        for file in files:
            if not file.is_file() or not fnmatch.fnmatch(file.name, include):
                continue
            try:
                safe = boundary.resolve(boundary.relative(file))
                for number, line in enumerate(
                    boundary.iter_text_lines(boundary.relative(safe), errors="ignore"),
                    1,
                ):
                    if regex.search(line):
                        matches.append(
                            {
                                "path": boundary.relative(safe),
                                "line": number,
                                "content": line[:500],
                            }
                        )
                        if len(matches) >= limit:
                            return json.dumps({"matches": matches, "truncated": True})
            except (OSError, ValueError):
                continue
        return json.dumps({"matches": matches, "truncated": False})

    async def write_file(args: dict[str, Any], _: ToolContext) -> str:
        content = str(args["content"])
        if len(content.encode("utf-8")) > boundary.max_write_bytes:
            raise ValueError("content exceeds workspace write limit")
        path = boundary.resolve(args["path"], write=True, allow_missing=True)
        existed = path.exists()
        before_hash = _boundary_file_hash(boundary, path) if existed else None
        if existed and not bool(args.get("overwrite", False)):
            raise ValueError("file exists; set overwrite=true explicitly")
        boundary.atomic_write(boundary.relative(path), content.encode("utf-8"))
        return json.dumps(
            {
                "path": boundary.relative(path),
                "changed_files": [boundary.relative(path)],
                "created": not existed,
                "before_sha256": before_hash,
                "after_sha256": _boundary_file_hash(boundary, path),
            }
        )

    async def edit_file(args: dict[str, Any], _: ToolContext) -> str:
        path = boundary.resolve(args["path"], write=True)
        content = boundary.read_bytes(args["path"]).decode("utf-8")
        before_hash = _boundary_file_hash(boundary, path)
        old = str(args["old_string"])
        new = str(args["new_string"])
        count = content.count(old)
        if count == 0:
            raise ValueError("old_string not found")
        if count > 1 and not bool(args.get("replace_all", False)):
            raise ValueError(f"old_string has {count} matches; set replace_all=true")
        updated = content.replace(old, new, -1 if args.get("replace_all") else 1)
        if len(updated.encode("utf-8")) > boundary.max_write_bytes:
            raise ValueError("result exceeds workspace write limit")
        boundary.atomic_write(boundary.relative(path), updated.encode("utf-8"))
        return json.dumps(
            {
                "path": boundary.relative(path),
                "replacements": count if args.get("replace_all") else 1,
                "changed_files": [boundary.relative(path)],
                "before_sha256": before_hash,
                "after_sha256": _boundary_file_hash(boundary, path),
            }
        )

    async def apply_patch(args: dict[str, Any], _: ToolContext) -> str:
        if "patch" in args:
            return _apply_unified_patch(boundary, str(args["patch"]))
        # Compatibility for the original exact-replacement contract.
        path = boundary.resolve(args["path"], write=True)
        content = boundary.read_bytes(args["path"]).decode("utf-8")
        before_hash = _boundary_file_hash(boundary, path)
        old = str(args["old_text"])
        new = str(args["new_text"])
        count = content.count(old)
        if count != 1:
            raise ValueError(f"patch context must match exactly once, got {count}")
        updated = content.replace(old, new, 1)
        boundary.atomic_write(boundary.relative(path), updated.encode("utf-8"))
        return json.dumps(
            {
                "path": boundary.relative(path),
                "changed_files": [boundary.relative(path)],
                "applied": True,
                "before_sha256": before_hash,
                "after_sha256": _boundary_file_hash(boundary, path),
            }
        )

    object_schema = {"type": "object", "additionalProperties": False}
    output_schema = {"type": "object"}
    return [
        ToolDef(
            "read_file",
            "Read a UTF-8 text file from the configured workspace.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "max_bytes": {"type": "integer"},
                },
                "required": ["path"],
            },
            read_file,
            toolset=("file",),
            permissions=("filesystem.read",),
            output_schema=output_schema,
            deferred=True,
        ),
        ToolDef(
            "list_directory",
            "List files in a workspace directory.",
            {
                **object_schema,
                "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            },
            list_directory,
            toolset=("file",),
            permissions=("filesystem.read",),
            output_schema=output_schema,
            deferred=True,
        ),
        ToolDef(
            "glob",
            "Find workspace paths by glob pattern.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
            glob_tool,
            toolset=("file",),
            permissions=("filesystem.read",),
            output_schema=output_schema,
            deferred=True,
        ),
        ToolDef(
            "grep",
            "Search workspace text files with a regular expression.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "include": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
            grep_tool,
            toolset=("file",),
            permissions=("filesystem.read",),
            output_schema=output_schema,
            deferred=True,
        ),
        ToolDef(
            "write_file",
            "Create or atomically replace a workspace text file.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            write_file,
            toolset=("file",),
            permissions=("filesystem.write",),
            risk_level="medium",
            side_effect_class="reconcilable",
            reconcile_fn=reconcile_file,
            output_schema=output_schema,
            deferred=True,
        ),
        ToolDef(
            "edit_file",
            "Replace an exact string in a workspace text file.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            edit_file,
            toolset=("file",),
            permissions=("filesystem.write",),
            risk_level="medium",
            side_effect_class="reconcilable",
            reconcile_fn=reconcile_file,
            output_schema=output_schema,
            deferred=True,
        ),
        ToolDef(
            "apply_patch",
            "Apply a command-free git-style unified diff to workspace text files.",
            {
                **object_schema,
                "properties": {
                    "patch": {"type": "string"},
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "anyOf": [
                    {"required": ["patch"]},
                    {"required": ["path", "old_text", "new_text"]},
                ],
            },
            apply_patch,
            toolset=("file",),
            permissions=("filesystem.write",),
            risk_level="medium",
            side_effect_class="reconcilable",
            reconcile_fn=reconcile_file,
            output_schema=output_schema,
            deferred=True,
        ),
    ]


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _boundary_file_hash(boundary: WorkspaceBoundary, path: Path) -> str:
    return hashlib.sha256(boundary.read_bytes(boundary.relative(path))).hexdigest()


def _matches_hash(
    boundary: WorkspaceBoundary,
    relative_path: str | None,
    expected_hash: str | None,
) -> bool:
    if relative_path is None:
        return expected_hash is None
    try:
        path = boundary.resolve(relative_path)
    except ValueError:
        return expected_hash is None
    if expected_hash is None:
        return False
    return _boundary_file_hash(boundary, path) == expected_hash


def _apply_unified_patch(boundary: WorkspaceBoundary, patch_text: str) -> str:
    patches = parse_unified_diff(patch_text)
    if len(patches) > 100:
        raise ValueError("patch exceeds the 100-file safety limit")
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    for patch in patches:
        old_path = boundary.resolve(patch.old_path, write=True) if patch.old_path else None
        new_path = (
            boundary.resolve(patch.new_path, write=True, allow_missing=True)
            if patch.new_path
            else None
        )
        plan_keys: set[str] = set()
        for candidate in (old_path, new_path):
            if candidate is None:
                continue
            key = str(candidate).casefold()
            if key in seen:
                raise ValueError("a patch path is modified more than once")
            plan_keys.add(key)
        seen.update(plan_keys)
        if old_path is not None:
            if not old_path.is_file():
                raise ValueError(f"patch source is not a file: {patch.old_path}")
            original_bytes = boundary.read_bytes(boundary.relative(old_path))
            if b"\x00" in original_bytes:
                raise ValueError("binary files are not supported")
            original = original_bytes.decode("utf-8")
        else:
            original_bytes = b""
            original = ""
            if new_path is not None and new_path.exists():
                raise ValueError(f"patch create target already exists: {patch.new_path}")
        updated = apply_hunks(original, patch.hunks)
        updated_bytes = updated.encode("utf-8")
        total_bytes += len(updated_bytes)
        if total_bytes > boundary.max_write_bytes:
            raise ValueError("patch result exceeds workspace write limit")
        plans.append(
            {
                "operation": patch.operation,
                "old_path": old_path,
                "new_path": new_path,
                "before": original_bytes,
                "after": updated_bytes,
                "before_sha256": hashlib.sha256(original_bytes).hexdigest() if old_path else None,
                "after_sha256": hashlib.sha256(updated_bytes).hexdigest() if new_path else None,
            }
        )

    committed: list[dict[str, Any]] = []
    rollback_state = "not_needed"
    try:
        # All paths, hashes and contexts were verified above. Commit only now.
        for plan in plans:
            old_path, new_path = plan["old_path"], plan["new_path"]
            if old_path is not None:
                if (
                    not old_path.exists()
                    or _boundary_file_hash(boundary, old_path) != plan["before_sha256"]
                ):
                    raise ValueError("workspace file changed after patch preflight")
            elif new_path is not None and new_path.exists():
                raise ValueError("patch create target appeared after preflight")
            if new_path is not None:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                boundary.atomic_write(boundary.relative(new_path), plan["after"])
            if old_path is not None and (new_path is None or old_path != new_path):
                old_path.unlink()
            committed.append(plan)
    except Exception:
        rollback_state = "succeeded"
        for plan in reversed(committed):
            try:
                old_path, new_path = plan["old_path"], plan["new_path"]
                if old_path is not None:
                    old_path.parent.mkdir(parents=True, exist_ok=True)
                    boundary.atomic_write(boundary.relative(old_path), plan["before"])
                if new_path is not None and new_path != old_path and new_path.exists():
                    new_path.unlink()
            except Exception:
                rollback_state = "failed"
        raise

    changes = []
    for plan in plans:
        changes.append(
            {
                "operation": plan["operation"],
                "old_path": boundary.relative(plan["old_path"]) if plan["old_path"] else None,
                "new_path": boundary.relative(plan["new_path"]) if plan["new_path"] else None,
                "before_sha256": plan["before_sha256"],
                "after_sha256": plan["after_sha256"],
            }
        )
    return json.dumps(
        {
            "applied": True,
            "changes": changes,
            "changed_files": sorted(
                {
                    value
                    for item in changes
                    for value in (item["old_path"], item["new_path"])
                    if value
                }
            ),
            "total_bytes": total_bytes,
            "rollback_state": rollback_state,
        }
    )
