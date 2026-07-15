"""Host workspace boundary shared by filesystem tools."""

from __future__ import annotations

import contextlib
import ctypes
import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


class WorkspaceAccessError(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceBoundary:
    root: Path
    protected_paths: tuple[str, ...] = ()
    max_read_bytes: int = 1_000_000
    max_write_bytes: int = 1_000_000

    @classmethod
    def create(
        cls,
        root: str,
        *,
        protected_paths: list[str] | tuple[str, ...] = (),
        max_read_bytes: int = 1_000_000,
        max_write_bytes: int = 1_000_000,
    ) -> WorkspaceBoundary:
        resolved = Path(root).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise WorkspaceAccessError(f"workspace root is not a directory: {resolved}")
        return cls(
            root=resolved,
            protected_paths=tuple(_normalize_relative(p) for p in protected_paths),
            max_read_bytes=max_read_bytes,
            max_write_bytes=max_write_bytes,
        )

    def resolve(
        self,
        relative_path: str,
        *,
        write: bool = False,
        allow_missing: bool = False,
    ) -> Path:
        rel = _normalize_relative(relative_path)
        if not rel or rel == ".":
            candidate = self.root
        else:
            candidate = self.root / rel
        self._check_protected(rel)

        if allow_missing and not candidate.exists():
            existing = candidate.parent
            missing: list[str] = [candidate.name]
            while not existing.exists():
                missing.append(existing.name)
                if existing.parent == existing:
                    raise WorkspaceAccessError("path has no existing workspace ancestor")
                existing = existing.parent
            resolved_existing = existing.resolve(strict=True)
            self._assert_within(resolved_existing)
            resolved = resolved_existing.joinpath(*reversed(missing))
        else:
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise WorkspaceAccessError(f"path does not exist: {relative_path}") from exc
            self._assert_within(resolved)

        if write and resolved == self.root:
            raise WorkspaceAccessError("workspace root cannot be replaced")
        return resolved

    def relative(self, path: Path) -> str:
        self._assert_within(path.resolve(strict=False))
        return path.resolve(strict=False).relative_to(self.root).as_posix()

    @contextlib.contextmanager
    def open_fd(self, relative_path: str, *, directory: bool = False) -> Iterator[int]:
        """Open and validate the final object using the same OS handle.

        POSIX walks from a root directory fd with ``O_NOFOLLOW``. Windows opens
        the object first and validates ``GetFinalPathNameByHandleW`` so Junction
        and reparse targets cannot escape the configured workspace.
        """
        rel = _normalize_relative(relative_path)
        self._check_protected(rel)
        if os.name == "posix":
            fd = self._openat_no_follow(rel, directory=directory)
        else:
            target = self.root if rel == "." else self.root / rel
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            fd = os.open(target, flags)
            try:
                self._validate_windows_handle(fd)
            except Exception:
                os.close(fd)
                raise
        try:
            mode = os.fstat(fd).st_mode
            if directory and not stat.S_ISDIR(mode):
                raise WorkspaceAccessError("path is not a directory")
            if not directory and not stat.S_ISREG(mode):
                raise WorkspaceAccessError("path is not a regular file")
            yield fd
        finally:
            os.close(fd)

    def read_bytes(self, relative_path: str, *, max_bytes: int | None = None) -> bytes:
        limit = self.max_read_bytes if max_bytes is None else min(max_bytes, self.max_read_bytes)
        with self.open_fd(relative_path) as fd:
            size = os.fstat(fd).st_size
            if size > limit:
                raise WorkspaceAccessError("file exceeds workspace read limit")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > limit:
                raise WorkspaceAccessError("file exceeds workspace read limit")
            return data

    def iter_text_lines(
        self,
        relative_path: str,
        *,
        errors: str = "replace",
    ) -> Iterator[str]:
        """Stream UTF-8 text from one already validated file handle.

        This does not reject a file merely because its total size exceeds the
        ordinary whole-file read limit. Callers remain responsible for a
        bounded result, which makes line paging and grep useful for large files
        without loading the complete file into memory.
        """
        with self.open_fd(relative_path) as fd:
            with os.fdopen(
                os.dup(fd),
                "r",
                encoding="utf-8",
                errors=errors,
                newline=None,
            ) as stream:
                yield from stream

    def atomic_write(self, relative_path: str, data: bytes) -> Path:
        """Atomically write through a validated parent directory handle."""
        if len(data) > self.max_write_bytes:
            raise WorkspaceAccessError("content exceeds workspace write limit")
        rel = _normalize_relative(relative_path)
        target = self.resolve(rel, write=True, allow_missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        parent_rel = Path(rel).parent.as_posix() or "."
        name = Path(rel).name
        if os.name == "posix":
            with self.open_fd(parent_rel, directory=True) as parent_fd:
                temp_name = f".{name}.{os.urandom(8).hex()}.tmp"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
                try:
                    _write_all(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                try:
                    os.replace(
                        temp_name,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                finally:
                    try:
                        os.unlink(temp_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
        else:
            with self._validate_windows_directory(parent_rel):
                fd, temp_name = tempfile.mkstemp(prefix=f".{name}.", dir=target.parent)
                try:
                    _write_all(fd, data)
                    os.fsync(fd)
                    os.close(fd)
                    fd = -1
                    with self.open_fd(self.relative(Path(temp_name))):
                        pass
                    os.replace(temp_name, target)
                finally:
                    if fd >= 0:
                        os.close(fd)
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
        # Re-open the committed object and verify the final handle before return.
        with self.open_fd(rel):
            pass
        return target

    @contextlib.contextmanager
    def _validate_windows_directory(self, relative_path: str) -> Iterator[None]:
        if os.name != "nt":
            yield
            return
        target = self.resolve(relative_path)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            str(target),
            0,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in {None, invalid}:
            raise WorkspaceAccessError("cannot open final Windows directory handle")
        try:
            self._validate_windows_raw_handle(handle)
            yield
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))

    def _openat_no_follow(self, rel: str, *, directory: bool) -> int:
        flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        current = os.open(self.root, flags_dir)
        if rel == ".":
            if not directory:
                os.close(current)
                raise WorkspaceAccessError("workspace root is not a file")
            return current
        parts = rel.split("/")
        try:
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                if not last or directory:
                    flags |= getattr(os, "O_DIRECTORY", 0)
                next_fd = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = next_fd
            return current
        except OSError as exc:
            os.close(current)
            raise WorkspaceAccessError(f"unsafe or missing workspace path: {rel}") from exc

    def _validate_windows_handle(self, fd: int) -> None:
        if os.name != "nt":
            return
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        self._validate_windows_raw_handle(handle)

    def _validate_windows_raw_handle(self, handle: int) -> None:
        if os.name != "nt":
            return
        buffer = ctypes.create_unicode_buffer(32_768)
        length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(  # type: ignore[attr-defined]
            ctypes.c_void_p(handle),
            buffer,
            len(buffer),
            0,
        )
        if length == 0 or length >= len(buffer):
            raise WorkspaceAccessError("cannot resolve final Windows handle path")
        final = buffer.value
        if final.startswith("\\\\?\\UNC\\"):
            final = "\\\\" + final[8:]
        elif final.startswith("\\\\?\\"):
            final = final[4:]
        self._assert_within(Path(final).resolve(strict=True))

    def _check_protected(self, rel: str) -> None:
        folded = rel.replace("\\", "/").casefold()
        for protected in self.protected_paths:
            p = protected.casefold().rstrip("/")
            if folded == p or folded.startswith(p + "/"):
                raise WorkspaceAccessError(f"protected path: {rel}")

    def _assert_within(self, path: Path) -> None:
        try:
            common = Path(os.path.commonpath((str(self.root), str(path))))
        except ValueError as exc:
            raise WorkspaceAccessError("path is on a different volume") from exc
        if common != self.root:
            raise WorkspaceAccessError("path escapes configured workspace root")


def _normalize_relative(value: str) -> str:
    text = str(value or ".").replace("\\", "/").strip()
    path = Path(text)
    if path.is_absolute() or path.drive:
        raise WorkspaceAccessError("absolute paths are not allowed")
    parts = [part for part in path.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise WorkspaceAccessError("parent traversal is not allowed")
    return Path(*parts).as_posix() if parts else "."


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        written += os.write(fd, view[written:])
