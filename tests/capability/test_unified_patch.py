from __future__ import annotations

import asyncio
import json

import pytest

from cogito.capability.models import ToolContext
from cogito.capability.unified_patch import UnifiedPatchError, apply_hunks, parse_unified_diff
from cogito.capability.workspace import WorkspaceBoundary
from cogito.tools.filesystem import create_tool_defs


def _tool(boundary: WorkspaceBoundary, name: str):
    return next(item for item in create_tool_defs(boundary) if item.name == name)


def _context() -> ToolContext:
    return ToolContext(attempt_id="attempt", trace_id="trace", tool_call_id="call")


def test_parse_and_apply_hunk() -> None:
    patch = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-two
+changed
"""
    parsed = parse_unified_diff(patch)
    assert apply_hunks("one\ntwo\n", parsed[0].hunks) == "one\nchanged\n"


def test_multi_file_create_modify_and_delete(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "old.txt").write_text("remove\n", encoding="utf-8")
    boundary = WorkspaceBoundary.create(str(tmp_path))
    patch = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-two
+changed
diff --git a/new.txt b/new.txt
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,1 @@
+created
diff --git a/old.txt b/old.txt
--- a/old.txt
+++ /dev/null
@@ -1,1 +0,0 @@
-remove
"""
    result = asyncio.run(_tool(boundary, "apply_patch").handler(
        {"patch": patch}, _context(),
    ))
    decoded = json.loads(result)
    assert decoded["applied"] is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "one\nchanged\n"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "created\n"
    assert not (tmp_path / "old.txt").exists()


def test_context_mismatch_changes_nothing(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("actual\n", encoding="utf-8")
    boundary = WorkspaceBoundary.create(str(tmp_path))
    patch = """--- a/a.txt
+++ b/a.txt
@@ -1,1 +1,1 @@
-expected
+changed
"""
    with pytest.raises(UnifiedPatchError):
        asyncio.run(_tool(boundary, "apply_patch").handler(
            {"patch": patch}, _context(),
        ))
    assert path.read_text(encoding="utf-8") == "actual\n"


def test_hunk_count_mismatch_is_rejected() -> None:
    with pytest.raises(UnifiedPatchError):
        parse_unified_diff("""--- a/a
+++ b/a
@@ -1,2 +1,1 @@
-one
+two
""")
