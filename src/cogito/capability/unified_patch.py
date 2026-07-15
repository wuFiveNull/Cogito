"""Parse and apply command-free git-style unified diffs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class UnifiedPatchError(ValueError):
    pass


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass
class FilePatch:
    old_path: str | None = None
    new_path: str | None = None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def operation(self) -> str:
        if self.old_path is None:
            return "create"
        if self.new_path is None:
            return "delete"
        if self.old_path != self.new_path:
            return "rename" if not self.hunks else "rename_modify"
        return "modify"


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(value: str) -> list[FilePatch]:
    lines = value.splitlines(keepends=True)
    result: list[FilePatch] = []
    current: FilePatch | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            if current is not None:
                result.append(current)
            parts = line.rstrip("\r\n").split(maxsplit=3)
            if len(parts) != 4:
                raise UnifiedPatchError("invalid diff --git header")
            current = FilePatch(_clean_path(parts[2]), _clean_path(parts[3]))
            index += 1
            continue
        if line.startswith("rename from "):
            current = current or FilePatch()
            current.old_path = _clean_path(line[len("rename from ") :].strip(), strip_prefix=False)
            index += 1
            continue
        if line.startswith("rename to "):
            current = current or FilePatch()
            current.new_path = _clean_path(line[len("rename to ") :].strip(), strip_prefix=False)
            index += 1
            continue
        if line.startswith("--- "):
            if current is None:
                current = FilePatch()
            current.old_path = _header_path(line[4:])
            index += 1
            if index >= len(lines) or not lines[index].startswith("+++ "):
                raise UnifiedPatchError("missing +++ header")
            current.new_path = _header_path(lines[index][4:])
            index += 1
            continue
        match = _HUNK.match(line)
        if match:
            if current is None:
                raise UnifiedPatchError("hunk has no file header")
            body: list[str] = []
            index += 1
            while index < len(lines):
                body_line = lines[index]
                if _HUNK.match(body_line) or body_line.startswith("diff --git "):
                    break
                if body_line.startswith((" ", "+", "-")):
                    body.append(body_line)
                elif body_line.startswith("\\ No newline at end of file"):
                    pass
                else:
                    break
                index += 1
            hunk = Hunk(
                int(match.group(1)),
                int(match.group(2) or "1"),
                int(match.group(3)),
                int(match.group(4) or "1"),
                tuple(body),
            )
            _validate_hunk_counts(hunk)
            current.hunks.append(hunk)
            continue
        index += 1
    if current is not None:
        result.append(current)
    if not result:
        raise UnifiedPatchError("patch contains no file changes")
    for item in result:
        if item.old_path is None and item.new_path is None:
            raise UnifiedPatchError("file patch has no path")
    return result


def apply_hunks(original: str, hunks: list[Hunk]) -> str:
    source = original.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    for hunk in hunks:
        target = max(0, hunk.old_start - 1)
        if target < cursor or target > len(source):
            raise UnifiedPatchError("overlapping or out-of-range hunk")
        output.extend(source[cursor:target])
        cursor = target
        for line in hunk.lines:
            marker, payload = line[0], line[1:]
            if marker in {" ", "-"}:
                if cursor >= len(source) or not _same_line(source[cursor], payload):
                    raise UnifiedPatchError(
                        f"patch context mismatch near source line {cursor + 1}",
                    )
                if marker == " ":
                    output.append(source[cursor])
                cursor += 1
            elif marker == "+":
                output.append(payload)
        # Counts are checked during parsing; cursor is now at the end of old range.
    output.extend(source[cursor:])
    return "".join(output)


def _same_line(left: str, right: str) -> bool:
    return left.rstrip("\r\n") == right.rstrip("\r\n")


def _validate_hunk_counts(hunk: Hunk) -> None:
    old = sum(1 for line in hunk.lines if line.startswith((" ", "-")))
    new = sum(1 for line in hunk.lines if line.startswith((" ", "+")))
    if old != hunk.old_count or new != hunk.new_count:
        raise UnifiedPatchError("hunk line counts do not match header")


def _header_path(value: str) -> str | None:
    raw = value.rstrip("\r\n").split("\t", 1)[0].strip()
    return None if raw == "/dev/null" else _clean_path(raw)


def _clean_path(value: str, *, strip_prefix: bool = True) -> str:
    raw = value.strip()
    if raw.startswith('"') or "\x00" in raw:
        raise UnifiedPatchError("quoted or NUL paths are not supported")
    if strip_prefix and raw[:2] in {"a/", "b/"}:
        raw = raw[2:]
    if not raw:
        raise UnifiedPatchError("empty patch path")
    return raw
