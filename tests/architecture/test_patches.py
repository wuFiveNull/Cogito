"""Patch applier + protected-field tests — Plan 01 M4."""

from __future__ import annotations

import pytest

from cogito.contracts.envelope import PROTECTED_FIELDS
from cogito.contracts.patch import (
    AddMetadata,
    AddTag,
    AppendContentPart,
    PatchRejectedError,
    RemoveField,
    RequireApproval,
    Reject,
    SetField,
    apply_patches,
    protected_field_names,
)


def test_set_field_applies() -> None:
    target: dict = {"a": 1}
    result = apply_patches(target, [SetField("a", 2)])
    assert target["a"] == 2
    assert len(result.applied) == 1
    assert len(result.rejected) == 0


def test_protected_field_rejected() -> None:
    target: dict = {"trace_id": "keep", "other": "x"}
    result = apply_patches(
        target,
        [SetField("trace_id", "hacked"), SetField("other", "y")],
    )
    assert target["trace_id"] == "keep"
    assert target["other"] == "y"
    assert len(result.rejected) == 1
    assert "protected" in result.rejected[0][1]


def test_remove_field_cannot_touch_protected() -> None:
    target: dict = {"principal_id": "owner", "extra": "ok"}
    result = apply_patches(
        target,
        [RemoveField("principal_id"), RemoveField("extra")],
    )
    assert "principal_id" in target
    assert "extra" not in target
    assert len(result.rejected) == 1


def test_append_content_part() -> None:
    target: dict = {"content_parts": []}
    result = apply_patches(
        target,
        [AppendContentPart({"type": "text", "text": "hi"})],
    )
    assert target["content_parts"] == [{"type": "text", "text": "hi"}]
    assert len(result.applied) == 1


def test_add_metadata_and_tag() -> None:
    target: dict = {}
    result = apply_patches(
        target,
        [AddMetadata("k", "v"), AddTag("urgent"), AddTag("urgent")],
    )
    assert target["metadata"] == {"k": "v", "tags": ["urgent"]}
    assert len(result.applied) == 3


def test_control_flow_patches_rejected_by_applier() -> None:
    target: dict = {}
    result = apply_patches(
        target,
        [Reject("bad", "no"), RequireApproval("tool", {"x": 1})],
    )
    assert len(result.rejected) == 2


def test_same_patch_sequence_is_deterministic() -> None:
    """Same Patch sequence yields the same result (Plan 01 M4 invariant)."""
    base: dict = {"a": 1, "content_parts": []}
    p = [SetField("a", 2), AppendContentPart({"t": 1}), AddTag("x")]
    r1 = apply_patches(dict(base), list(p))
    r2 = apply_patches(dict(base), list(p))
    assert r1.applied == r2.applied
    assert r1.rejected == r2.rejected


def test_protected_field_names_matches_envelope() -> None:
    assert protected_field_names() == PROTECTED_FIELDS
    assert "trace_id" in protected_field_names()
    assert "schema_version" in protected_field_names()
