from __future__ import annotations

import json

from cogito.capability.models import DeferredExecution, ToolContext
from cogito.service.delegation_lifecycle import DelegationLifecycleService
from cogito.service.tool_sinks import ToolCallRepositorySink
from cogito.store.tool_call_repo import ToolCallRepository


def test_delegation_queues_two_children_and_resumes_any_join(in_memory_db) -> None:
    in_memory_db.execute(
        "INSERT INTO turns(turn_id,status,created_at) VALUES ('parent','waiting_external',0)",
    )
    context = ToolContext(
        attempt_id="attempt", trace_id="trace", tool_call_id="tool-call",
        turn_id="parent", principal_id="owner", session_id="session",
        input_message_id="message", tool_state={},
        resource_budget={
            "max_loop_iterations": 10,
            "max_model_calls": 20,
            "max_tool_calls": 50,
            "max_input_tokens": 32_000,
            "max_output_tokens": 8_192,
            "max_wall_time_s": 120,
            "max_cost": 0,
        },
        resource_usage={"loop_iterations": 1, "model_calls": 1, "tool_calls": 0},
    )
    service = DelegationLifecycleService(in_memory_db)
    deferred = service.create(
        {
            "tasks": [
                {
                    "client_id": "a", "prompt": "A", "role": "researcher",
                    "toolsets": ["web"],
                },
                {
                    "client_id": "b", "prompt": "B", "role": "reviewer",
                    "toolsets": ["memory", "web"],
                },
                {"client_id": "c", "prompt": "C", "role": "planner", "toolsets": ["web"]},
            ],
            "join_policy": "any",
            "failure_policy": "collect",
        },
        context,
        allowed_toolsets={"web", "memory"},
    )
    assert isinstance(deferred, DeferredExecution)
    tasks = in_memory_db.execute(
        "SELECT t.task_id,t.status,l.client_id FROM tasks t JOIN child_task_links l "
        "ON l.task_id=t.task_id WHERE t.task_type='agent.delegate' ORDER BY t.created_at,t.task_id",
    ).fetchall()
    assert sorted(row["status"] for row in tasks) == ["queued", "queued", "waiting_external"]

    first = tasks[0]["task_id"]
    in_memory_db.execute(
        "UPDATE tasks SET status='completed',result_ref=? WHERE task_id=?",
        (json.dumps({"result": "done"}), first),
    )
    in_memory_db.execute(
        "UPDATE child_task_links SET status='completed',result_summary='done',usage_json=? "
        "WHERE task_id=?",
        (json.dumps({"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}), first),
    )
    in_memory_db.commit()
    assert service.evaluate_for_task(first) is True
    assert in_memory_db.execute(
        "SELECT status FROM turns WHERE turn_id='parent'",
    ).fetchone()["status"] == "queued"
    delegation_usage = json.loads(in_memory_db.execute(
        "SELECT usage_json FROM agent_delegations",
    ).fetchone()["usage_json"])
    assert delegation_usage == {
        "input_tokens": 11,
        "output_tokens": 4,
        "total_tokens": 15,
    }

    sink = ToolCallRepositorySink(
        ToolCallRepository(in_memory_db), connection=in_memory_db,
    )
    claimed = sink.claim_deferred_result("parent")
    assert claimed is not None
    assert claimed["tool_call_id"] == "tool-call"
    assert json.loads(claimed["result"])["children"][0]["result_summary"] == "done"
    aggregate = json.loads(claimed["result"])
    assert aggregate["join_policy"] == "any"
    assert aggregate["usage"]["total_tokens"] == 15
    expected_role = {"a": "researcher", "b": "reviewer", "c": "planner"}
    assert aggregate["children"][0]["role"] == expected_role[tasks[0]["client_id"]]
    assert aggregate["children"][0]["budget"]["max_loop_iterations"] == 3
    assert sink.claim_deferred_result("parent") is None


def test_legacy_prompt_is_normalized(in_memory_db) -> None:
    in_memory_db.execute(
        "INSERT INTO turns(turn_id,status,created_at) VALUES ('parent','running',0)",
    )
    context = ToolContext(
        attempt_id="attempt", trace_id="trace", tool_call_id="call",
        turn_id="parent", principal_id="owner", tool_state={},
    )
    DelegationLifecycleService(in_memory_db).create(
        {"prompt": "legacy", "toolsets": ["web"]}, context,
        allowed_toolsets={"web"},
    )
    payload = json.loads(in_memory_db.execute(
        "SELECT payload_ref FROM tasks WHERE task_type='agent.delegate'",
    ).fetchone()["payload_ref"])
    assert payload["prompt"] == "legacy"
    assert payload["role"] == "general"
    assert payload["toolsets"] == ["web"]


def test_role_policy_narrows_requested_toolsets(in_memory_db) -> None:
    in_memory_db.execute(
        "INSERT INTO turns(turn_id,status,created_at) VALUES ('parent','running',0)",
    )
    context = ToolContext(
        attempt_id="attempt", trace_id="trace", tool_call_id="call",
        turn_id="parent", principal_id="owner", tool_state={},
    )
    DelegationLifecycleService(in_memory_db).create(
        {
            "tasks": [{
                "prompt": "review", "role": "reviewer",
                "toolsets": ["file", "web", "subagent"],
            }],
        },
        context,
        allowed_toolsets={"file", "web", "subagent"},
    )
    payload = json.loads(in_memory_db.execute(
        "SELECT payload_ref FROM tasks WHERE task_type='agent.delegate'",
    ).fetchone()["payload_ref"])
    assert payload["role"] == "reviewer"
    assert payload["read_only"] is True
    assert payload["requested_toolsets"] == ["file", "subagent", "web"]
    assert payload["toolsets"] == ["file"]
