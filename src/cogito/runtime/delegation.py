"""Durable bounded child-Agent tools."""

from __future__ import annotations

import json
from typing import Any

from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.registry import CapabilityRegistry
from cogito.domain.delegation import DELEGATION_ROLES


def create_delegation_tool_defs(
    *,
    connection: Any,
    router: Any,
    registry: CapabilityRegistry,
    executor: Any,
    parent_toolsets: set[str],
    lifecycle: Any,
) -> list[ToolDef]:
    del router, executor
    # Child Agents never receive Channel, delivery, external messaging, account,
    # financial, permission-management, or arbitrary shell capabilities.
    child_policy = {"core", "memory", "search", "web", "file", "knowledge"}
    child_toolsets = parent_toolsets & child_policy

    async def delegate(args: dict[str, Any], ctx: ToolContext):
        return lifecycle.create(args, ctx, allowed_toolsets=child_toolsets)

    async def manage(args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args.get("action", "list"))
        delegation_id = str(args.get("delegation_id", ""))
        if action == "cancel":
            return json.dumps(
                {
                    "delegation_id": delegation_id,
                    "cancel_requested": lifecycle.cancel(delegation_id, ctx.turn_id),
                }
            )
        if delegation_id:
            return json.dumps(
                lifecycle.status(delegation_id, ctx.turn_id) or {}, ensure_ascii=False,
            )
        return json.dumps(lifecycle.list_for_parent(ctx.turn_id), ensure_ascii=False)

    schema = {"type": "object", "additionalProperties": False}
    return [
        ToolDef(
            "delegate_task",
            "Queue one to three bounded child Agents (general/researcher/coder/reviewer/planner) "
            "and resume after their durable join.",
            {
                **schema,
                "properties": {
                    "prompt": {"type": "string"},
                    "toolsets": {"type": "array", "items": {"type": "string"}},
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "client_id": {"type": "string"},
                                "prompt": {"type": "string"},
                                "role": {
                                    "type": "string",
                                    "enum": sorted(DELEGATION_ROLES),
                                },
                                "toolsets": {"type": "array", "items": {"type": "string"}},
                                "budget": {"$ref": "#/$defs/budget"},
                            },
                            "required": ["prompt"],
                        },
                    },
                    "join_policy": {"type": "string", "enum": ["all", "any"]},
                    "failure_policy": {"type": "string", "enum": ["collect"]},
                    "role": {"type": "string", "enum": sorted(DELEGATION_ROLES)},
                    "budget": {"$ref": "#/$defs/budget"},
                    "max_steps": {"type": "integer"},
                    "timeout_seconds": {"type": "integer"},
                },
                "$defs": {
                    "budget": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "max_loop_iterations": {"type": "integer", "minimum": 1},
                            "max_model_calls": {"type": "integer", "minimum": 1},
                            "max_tool_calls": {"type": "integer", "minimum": 1},
                            "max_input_tokens": {"type": "integer", "minimum": 1},
                            "max_output_tokens": {"type": "integer", "minimum": 1},
                            "max_wall_time_s": {"type": "integer", "minimum": 1},
                            "max_cost": {"type": "number", "minimum": 0},
                        },
                    }
                },
                "anyOf": [{"required": ["prompt"]}, {"required": ["tasks"]}],
            },
            delegate,
            toolset=("subagent",),
            permissions=("agent.delegate",),
            risk_level="medium",
            side_effect_class="reconcilable",
            reconcile_fn=lifecycle.reconcile,
            deferred=True,
            output_schema={
                "type": "object",
                "required": ["delegation_id", "status", "children", "usage"],
                "properties": {
                    "delegation_id": {"type": "string"},
                    "status": {
                        "type": "string", "enum": ["completed", "failed", "cancelled"]
                    },
                    "join_policy": {"type": "string", "enum": ["all", "any"]},
                    "failure_policy": {"type": "string", "enum": ["collect"]},
                    "completed_count": {"type": "integer"},
                    "failed_count": {"type": "integer"},
                    "usage": {"type": "object"},
                    "children": {"type": "array", "items": {"type": "object"}},
                },
            },
        ),
        ToolDef(
            "subagent_manage",
            "List, inspect, or cancel durable child Agent runs.",
            {
                **schema,
                "properties": {
                    "action": {"type": "string", "enum": ["list", "status", "cancel"]},
                    "delegation_id": {"type": "string"},
                },
            },
            manage,
            toolset=("subagent",),
            permissions=("agent.delegate",),
            risk_level="medium",
            side_effect_class="idempotent",
            reconcile_fn=lifecycle.reconcile_manage,
            deferred=True,
            output_schema={"type": "object"},
        ),
    ]
