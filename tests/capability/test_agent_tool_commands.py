from __future__ import annotations

from cogito.capability.skill_parser import parse_skill_md
from cogito.service.agent_tool_commands import AgentToolCommandService


def test_schedule_commands_are_exactly_idempotent(in_memory_db) -> None:
    service = AgentToolCommandService(in_memory_db)
    args = {
        "prompt": "check the project",
        "schedule_type": "interval",
        "expression": "5m",
        "timezone": "UTC",
        "session_id": "session-1",
    }

    first = service.create_schedule(args, actor="owner", tool_call_id="call-create")
    second = service.create_schedule(args, actor="owner", tool_call_id="call-create")

    assert first.schedule_id == second.schedule_id
    assert in_memory_db.execute(
        "SELECT COUNT(*) FROM schedules WHERE task_type='agent.prompt'",
    ).fetchone()[0] == 1
    assert in_memory_db.execute(
        "SELECT COUNT(*) FROM agent_tool_command_results "
        "WHERE command_name='CreateAgentSchedule'",
    ).fetchone()[0] == 1

    service.cancel_schedule(
        first.schedule_id, first.version, actor="owner", tool_call_id="call-cancel",
    )
    service.cancel_schedule(
        first.schedule_id, first.version, actor="owner", tool_call_id="call-cancel",
    )
    row = in_memory_db.execute(
        "SELECT enabled,version FROM schedules WHERE schedule_id=?", (first.schedule_id,),
    ).fetchone()
    assert (row["enabled"], row["version"]) == (0, first.version + 1)
    assert in_memory_db.execute(
        "SELECT COUNT(*) FROM audit_records WHERE action IN "
        "('CreateAgentSchedule','CancelAgentSchedule')",
    ).fetchone()[0] == 2


def test_skill_create_is_idempotent_and_version_bound(in_memory_db, tmp_path) -> None:
    raw = """---
name: review
description: Review changes
version: 1
toolsets: [file]
permissions: [filesystem.read]
---
Review the requested files.
"""
    manifest = parse_skill_md(raw)
    service = AgentToolCommandService(in_memory_db)

    first = service.manage_skill(
        root=tmp_path,
        action="create",
        name="review",
        raw=raw,
        manifest=manifest,
        expected_version="",
        actor="owner",
        tool_call_id="skill-create",
    )
    second = service.manage_skill(
        root=tmp_path,
        action="create",
        name="review",
        raw=raw,
        manifest=manifest,
        expected_version="",
        actor="owner",
        tool_call_id="skill-create",
    )

    assert first == second
    assert (tmp_path / "review" / "SKILL.md").read_text(encoding="utf-8") == raw
    assert in_memory_db.execute(
        "SELECT COUNT(*) FROM skills WHERE name='review'",
    ).fetchone()[0] == 1
    assert in_memory_db.execute(
        "SELECT COUNT(*) FROM agent_tool_command_results WHERE command_name='CreateSkill'",
    ).fetchone()[0] == 1
