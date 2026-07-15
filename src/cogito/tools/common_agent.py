"""Schedule and Skill tools backed by existing Cogito domain services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.registry import CapabilityRegistry
from cogito.capability.skill_parser import parse_skill_md, validate_skill
from cogito.service.agent_tool_commands import AgentToolCommandService
from cogito.store.schedule_repo import ScheduleRepository


def create_schedule_tool_defs(connection: Any) -> list[ToolDef]:
    commands = AgentToolCommandService(connection)

    async def create(args: dict[str, Any], ctx: ToolContext) -> str:
        command_args = {**args, "session_id": ctx.session_id}
        schedule = commands.create_schedule(
            command_args,
            actor=ctx.principal_id,
            tool_call_id=ctx.tool_call_id,
        )
        return json.dumps(schedule.to_dict(), ensure_ascii=False)

    async def list_schedules(args: dict[str, Any], _: ToolContext) -> str:
        items = [
            s.to_dict()
            for s in ScheduleRepository(connection).find_all(
                min(int(args.get("limit", 50)), 100),
            )
            if s.task_type == "agent.prompt"
        ]
        return json.dumps({"schedules": items}, ensure_ascii=False)

    async def cancel(args: dict[str, Any], _: ToolContext) -> str:
        commands.cancel_schedule(
            str(args["schedule_id"]),
            int(args["expected_version"]),
            actor=_.principal_id,
            tool_call_id=_.tool_call_id,
        )
        return json.dumps({"schedule_id": args["schedule_id"], "cancelled": True})

    schema = {"type": "object", "additionalProperties": False}
    output_schema = {"type": "object"}
    return [
        ToolDef(
            "schedule_create",
            "Schedule a bounded background Agent prompt.",
            {
                **schema,
                "properties": {
                    "prompt": {"type": "string"},
                    "schedule_type": {"type": "string", "enum": ["once", "interval", "cron"]},
                    "expression": {"type": "string"},
                    "timezone": {"type": "string"},
                },
                "required": ["prompt", "expression"],
            },
            create,
            toolset=("schedule",),
            permissions=("schedule.write",),
            risk_level="medium",
            side_effect_class="reconcilable",
            reconcile_fn=commands.reconcile,
            deferred=True,
            output_schema=output_schema,
        ),
        ToolDef(
            "schedule_list",
            "List Agent-created schedules.",
            {
                **schema,
                "properties": {"limit": {"type": "integer"}},
            },
            list_schedules,
            toolset=("schedule",),
            permissions=("schedule.read",),
            deferred=True,
            output_schema=output_schema,
        ),
        ToolDef(
            "schedule_cancel",
            "Cancel an Agent-created schedule with version checking.",
            {
                **schema,
                "properties": {
                    "schedule_id": {"type": "string"},
                    "expected_version": {"type": "integer"},
                },
                "required": ["schedule_id", "expected_version"],
            },
            cancel,
            toolset=("schedule",),
            permissions=("schedule.write",),
            risk_level="medium",
            side_effect_class="idempotent",
            reconcile_fn=commands.reconcile,
            deferred=True,
            output_schema=output_schema,
        ),
    ]


def create_skill_tool_defs(
    root: Path,
    registry: CapabilityRegistry,
    connection: Any,
) -> list[ToolDef]:
    root = root.resolve()
    archive_root = root / ".archive"
    commands = AgentToolCommandService(connection)

    def scan(include_archived: bool = False) -> dict[str, tuple[Path, Any]]:
        result: dict[str, tuple[Path, Any]] = {}
        roots = [root] + ([archive_root] if include_archived else [])
        for base in roots:
            if not base.exists():
                continue
            for path in base.glob("*/SKILL.md"):
                try:
                    manifest = parse_skill_md(path.read_text(encoding="utf-8"), source="user")
                    if not validate_skill(manifest):
                        result[manifest.name] = (path, manifest)
                except OSError:
                    continue
        return result

    async def skills_list(args: dict[str, Any], _: ToolContext) -> str:
        items = []
        for name, (path, manifest) in scan(bool(args.get("include_archived", False))).items():
            items.append(
                {
                    "name": name,
                    "description": manifest.description,
                    "version": manifest.version,
                    "toolsets": manifest.toolsets,
                    "archived": archive_root in path.parents,
                }
            )
        return json.dumps({"skills": sorted(items, key=lambda x: x["name"])}, ensure_ascii=False)

    async def skill_view(args: dict[str, Any], _: ToolContext) -> str:
        item = scan(True).get(str(args["name"]))
        if item is None:
            raise ValueError("skill not found")
        _, manifest = item
        return json.dumps(
            {
                "name": manifest.name,
                "description": manifest.description,
                "version": manifest.version,
                "toolsets": manifest.toolsets,
                "permissions": manifest.permissions,
                "content": manifest.content,
            },
            ensure_ascii=False,
        )

    async def skill_activate(args: dict[str, Any], ctx: ToolContext) -> str:
        item = scan().get(str(args["name"]))
        if item is None:
            raise ValueError("active skill not found")
        _, manifest = item
        activated = []
        for toolset in manifest.toolsets:
            for tool in registry.list_by_toolset(toolset):
                if ctx.expose_tool and ctx.expose_tool(tool.capability_id):
                    activated.append(tool.name)
        return json.dumps(
            {
                "name": manifest.name,
                "instructions": manifest.content,
                "activated_tools": sorted(set(activated)),
            },
            ensure_ascii=False,
        )

    async def skill_manage(args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args["action"])
        name = str(args["name"])
        existing = scan(True).get(name)
        if action in {"create", "update"}:
            raw = str(args["content"])
            manifest = parse_skill_md(raw, source="user")
            errors = validate_skill(manifest)
            if errors or manifest.name != name:
                raise ValueError("; ".join(errors) or "frontmatter name mismatch")
            allowed_permissions = {
                permission
                for tool in registry.all_tools()
                if not ctx.allowed_toolsets or set(tool.toolset) & set(ctx.allowed_toolsets)
                for permission in tool.permissions
            }
            if not set(manifest.permissions).issubset(allowed_permissions):
                raise ValueError("skill requests unknown permissions")
            if action == "create" and existing is not None:
                raise ValueError("skill already exists")
            if action == "update":
                if existing is None:
                    raise ValueError("skill not found")
                if existing[1].version != str(args.get("expected_version", "")):
                    raise ValueError("skill version conflict")
            result = commands.manage_skill(
                root=root,
                action=action,
                name=name,
                raw=raw,
                manifest=manifest,
                expected_version=str(args.get("expected_version", "")),
                actor=ctx.principal_id,
                tool_call_id=ctx.tool_call_id,
            )
            return json.dumps(result)
        if existing is None:
            raise ValueError("skill not found")
        result = commands.manage_skill(
            root=root,
            action=action,
            name=name,
            raw="",
            manifest=None,
            expected_version=str(args.get("expected_version", "")),
            actor=ctx.principal_id,
            tool_call_id=ctx.tool_call_id,
        )
        return json.dumps(result)

    schema = {"type": "object", "additionalProperties": False}
    output_schema = {"type": "object"}
    return [
        ToolDef(
            "skills_list",
            "List user skills.",
            {**schema, "properties": {"include_archived": {"type": "boolean"}}},
            skills_list,
            toolset=("skills",),
            output_schema=output_schema,
        ),
        ToolDef(
            "skill_view",
            "View a Skill manifest and instructions.",
            {**schema, "properties": {"name": {"type": "string"}}, "required": ["name"]},
            skill_view,
            toolset=("skills",),
            output_schema=output_schema,
        ),
        ToolDef(
            "skill_activate",
            "Load Skill instructions and activate its declared tools.",
            {**schema, "properties": {"name": {"type": "string"}}, "required": ["name"]},
            skill_activate,
            toolset=("skills",),
            output_schema=output_schema,
        ),
        ToolDef(
            "skill_manage",
            "Create, update, archive, or restore a user Skill.",
            {
                **schema,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "archive", "restore"],
                    },
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "expected_version": {"type": "string"},
                },
                "required": ["action", "name"],
            },
            skill_manage,
            toolset=("skills",),
            permissions=("skills.write",),
            risk_level="medium",
            side_effect_class="reconcilable",
            reconcile_fn=commands.reconcile,
            deferred=True,
            output_schema=output_schema,
        ),
    ]
