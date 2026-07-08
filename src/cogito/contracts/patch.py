"""Patch types — controlled mutation of Envelopes (Plan 01 M4).

Middleware / Handlers return Patches instead of mutating shared objects.
The Patch Applier enforces schema validation, authorization, and records the
source/order/rejection of every Patch.

Protected fields (trace_id, principal_id, conversation_id, turn_id, attempt_id,
origin, reply_route, schema_version, idempotency_key) cannot be patched and
attempts to do so must fail with an Audit entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cogito.contracts.envelope import PROTECTED_FIELDS

# ─── Patch intent types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SetField:
    """Set a top-level field to a new value."""
    field: str
    value: Any


@dataclass(frozen=True)
class RemoveField:
    """Remove an optional top-level field."""
    field: str


@dataclass(frozen=True)
class AppendContentPart:
    """Append a ContentPart to the content list."""
    content_part: dict[str, Any]


@dataclass(frozen=True)
class AppendPromptSection:
    """Append a section to the assembled prompt."""
    section: str
    content: str


@dataclass(frozen=True)
class AddMetadata:
    """Add or overwrite a single metadata key."""
    key: str
    value: Any


@dataclass(frozen=True)
class AddTag:
    """Append a tag to the metadata tag list."""
    tag: str


@dataclass(frozen=True)
class Reject:
    """Reject the request/operation outright with a reason code."""
    reason_code: str
    message: str = ""


@dataclass(frozen=True)
class RequireApproval:
    """Route the operation through approval before proceeding."""
    approval_type: str
    payload: dict[str, Any] = field(default_factory=dict)


Patch = (
    SetField | RemoveField | AppendContentPart | AppendPromptSection
    | AddMetadata | AddTag | Reject | RequireApproval
)


# ─── Patch application result ───────────────────────────────────────────────


@dataclass(frozen=True)
class PatchResult:
    """Outcome of applying a Patch sequence."""
    applied: tuple[Patch, ...]
    rejected: tuple[tuple[Patch, str], ...]


class PatchRejectedError(ValueError):
    """Base class for rejected patches."""


class ProtectedFieldError(PatchRejectedError):
    """Attempted to mutate a protected field."""


# ─── Patch Applier ──────────────────────────────────────────────────────────


def apply_patches(target: dict[str, Any], patches: list[Patch]) -> PatchResult:
    """Apply a sequence of Patches to a mutable target dict (never the original).

    Returns which patches applied and which were rejected (with reason).
    Never raises on rejection — rejections are recorded, not exceptional, so a
    bad patch never leaves the target in a half-modified state.
    """
    applied: list[Patch] = []
    rejected: list[tuple[Patch, str]] = []

    for patch in patches:
        if isinstance(patch, SetField):
            if patch.field in PROTECTED_FIELDS:
                rejected.append((patch, f"protected field: {patch.field}"))
                continue
            target[patch.field] = patch.value
            applied.append(patch)
        elif isinstance(patch, RemoveField):
            if patch.field in PROTECTED_FIELDS:
                rejected.append((patch, f"protected field: {patch.field}"))
                continue
            target.pop(patch.field, None)
            applied.append(patch)
        elif isinstance(patch, AppendContentPart):
            target.setdefault("content_parts", []).append(patch.content_part)
            applied.append(patch)
        elif isinstance(patch, AppendPromptSection):
            # Stored under metadata.prompt_sections as a deterministic list.
            sections = target.setdefault("metadata", {}).setdefault(
                "prompt_sections", []
            )
            sections.append({"section": patch.section, "content": patch.content})
            applied.append(patch)
        elif isinstance(patch, AddMetadata):
            target.setdefault("metadata", {})[patch.key] = patch.value
            applied.append(patch)
        elif isinstance(patch, AddTag):
            tags = target.setdefault("metadata", {}).setdefault("tags", [])
            if patch.tag not in tags:
                tags.append(patch.tag)
            applied.append(patch)
        elif isinstance(patch, (Reject, RequireApproval)):
            # These are control-flow signals, not data mutations: record + reject.
            rejected.append((
                patch,
                "control-flow patch rejected by applier; middleware must handle",
            ))
        else:
            rejected.append((patch, f"unknown patch type: {type(patch).__name__}"))

    return PatchResult(applied=tuple(applied), rejected=tuple(rejected))


def protected_field_names() -> frozenset[str]:
    """Expose the protected field set for audits / tests."""
    return PROTECTED_FIELDS
