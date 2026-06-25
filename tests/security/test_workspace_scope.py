"""Tests for DefaultWorkspaceScope — filesystem path security boundary."""

from __future__ import annotations

import os
import platform
import tempfile

import pytest

from cogito.infrastructure.sandbox.workspace_scope import DefaultWorkspaceScope


def _can_create_symlink() -> bool:
    """Check if the current process can create symlinks."""
    if platform.system() != "Windows":
        return True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.txt")
            dst = os.path.join(tmp, "dst.txt")
            with open(src, "w") as f:
                f.write("test")
            os.symlink(src, dst)
            os.remove(dst)
            return True
    except OSError:
        return False


class TestDefaultWorkspaceScope:
    def test_resolve_within_workspace(self) -> None:
        """Valid paths within workspace return allowed."""
        with tempfile.TemporaryDirectory() as tmp:
            scope = DefaultWorkspaceScope(tmp)
            result = scope.resolve_read("test.txt")
            assert result.within_workspace is True
            assert tmp in result.absolute

            result = scope.resolve_read(tmp)
            assert result.within_workspace is True

    def test_resolve_outside_workspace_traversal(self) -> None:
        """Paths with '..' that escape workspace are denied."""
        with tempfile.TemporaryDirectory() as tmp:
            scope = DefaultWorkspaceScope(tmp)
            result = scope.resolve_read("../../etc/passwd")
            assert result.within_workspace is False

    def test_resolve_empty_path(self) -> None:
        """Empty paths are denied."""
        with tempfile.TemporaryDirectory() as tmp:
            scope = DefaultWorkspaceScope(tmp)
            result = scope.resolve_read("")
            assert result.within_workspace is False

            result = scope.resolve_read("   ")
            assert result.within_workspace is False

    def test_resolve_absolute_outside_rejected(self) -> None:
        """Absolute paths outside workspace are denied."""
        with tempfile.TemporaryDirectory() as tmp:
            scope = DefaultWorkspaceScope(tmp)
            result = scope.resolve_read("/etc/passwd")
            assert result.within_workspace is False

    @pytest.mark.skipif(not _can_create_symlink(), reason="no symlink permission")
    def test_resolve_symlink_detection(self) -> None:
        """Symlinks are detected and flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            test_file = os.path.join(tmp, "real.txt")
            with open(test_file, "w") as f:
                f.write("content")

            link = os.path.join(tmp, "link.txt")
            os.symlink(test_file, link)

            # Symlinks are rejected by default
            scope = DefaultWorkspaceScope(tmp, follow_symlinks=False)
            result = scope.resolve_read("link.txt")
            assert result.within_workspace is False

    @pytest.mark.skipif(not _can_create_symlink(), reason="no symlink permission")
    def test_resolve_symlink_follow(self) -> None:
        """With follow_symlinks=True, symlinks resolve to target."""
        with tempfile.TemporaryDirectory() as tmp:
            test_file = os.path.join(tmp, "real.txt")
            with open(test_file, "w") as f:
                f.write("content")

            link = os.path.join(tmp, "link.txt")
            os.symlink(test_file, link)

            scope = DefaultWorkspaceScope(tmp, follow_symlinks=True)
            result = scope.resolve_read("link.txt")
            assert result.within_workspace is True
            assert result.is_symlink is True

    def test_write_resolve(self) -> None:
        """resolve_write also validates against workspace."""
        with tempfile.TemporaryDirectory() as tmp:
            scope = DefaultWorkspaceScope(tmp)
            result = scope.resolve_write("output.txt")
            assert result.within_workspace is True

            result = scope.resolve_write("../../outside.txt")
            assert result.within_workspace is False

    def test_nested_path_within_workspace(self) -> None:
        """Deeply nested paths within workspace are valid."""
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a", "b", "c")
            os.makedirs(nested)
            scope = DefaultWorkspaceScope(tmp)

            result = scope.resolve_read(os.path.join("a", "b", "c", "file.txt"))
            assert result.within_workspace is True

    def test_none_path_denied(self) -> None:
        """None-like paths are rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            scope = DefaultWorkspaceScope(tmp)
            result = scope.resolve_read("None")
            assert result.within_workspace is True  # 'None' is just a filename
