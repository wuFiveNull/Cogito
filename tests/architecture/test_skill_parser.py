"""PR-C6: SKILL.md parser + Skill Runtime — Plan 03 M6."""

from __future__ import annotations

from cogito.capability.skill_parser import (
    SkillRuntime,
    parse_skill_md,
    validate_skill,
)


SKILL_MD = """---
name: code-reviewer
description: Review code diffs for style and correctness
version: 1.0
toolsets: [read, diff]
permissions: [file_read]
author: hunriiz
---
You are a code reviewer. Analyze the diff and provide feedback.
"""


def test_parse_frontmatter() -> None:
    m = parse_skill_md(SKILL_MD)
    assert m.name == "code-reviewer"
    assert "style" in m.description
    assert "diff" in m.toolsets
    assert "file_read" in m.permissions
    assert "code reviewer" in m.content


def test_parse_error_location() -> None:
    """解析错误定位：缺失 name 字段。"""
    bad = "---\ndescription: no name\n---\nbody"
    m = parse_skill_md(bad)
    errors = validate_skill(m)
    assert any("invalid skill name" in e for e in errors)


def test_runtime_register_and_activate() -> None:
    rt = SkillRuntime(conn=None)
    manifest, errors = rt.parse_and_register(SKILL_MD)
    assert not errors
    content = rt.activate("code-reviewer")
    assert content is not None
    assert "code reviewer" in content


def test_runtime_archive_restore() -> None:
    rt = SkillRuntime(conn=None)
    rt.parse_and_register(SKILL_MD)
    assert rt.archive("code-reviewer") is True
    assert rt.restore("code-reviewer") is True
    assert rt.activate("code-reviewer") is not None


def test_runtime_pin() -> None:
    rt = SkillRuntime(conn=None)
    rt.parse_and_register(SKILL_MD)
    assert rt.pin("code-reviewer") is True
    active = rt.list_active()
    assert len(active) >= 1


def test_overlarge_skill_rejected() -> None:
    """超大 Skill 被拒绝。"""
    huge = f"---\nname: huge\n---\n{'x' * 60_000}"
    rt = SkillRuntime(conn=None)
    _, errors = rt.parse_and_register(huge)
    assert any("too large" in e for e in errors)
