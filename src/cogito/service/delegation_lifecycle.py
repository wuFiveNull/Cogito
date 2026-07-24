"""Durable parent/child Agent delegation lifecycle."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from cogito.capability.models import DeferredExecution, ToolContext
from cogito.contracts.clock import epoch_ms
from cogito.domain.delegation import (
    allocate_child_budget,
    resolve_delegation_role,
    select_role_toolsets,
)
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.task import Task, TaskStatus
from cogito.store.event_store import EventStore
from cogito.store.task_repo import TaskRepository


class DelegationLifecycleService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def create(
        self,
        args: dict[str, Any],
        context: ToolContext,
        *,
        allowed_toolsets: set[str],
    ) -> DeferredExecution:
        if context.tool_call_id:
            existing = self._conn.execute(
                "SELECT d.delegation_id,w.waiting_id FROM agent_delegations d "
                "JOIN waiting_conditions w ON w.subject_id=d.delegation_id "
                "AND w.condition_type='child_join' "
                "WHERE d.parent_turn_id=? AND d.parent_tool_call_id=? "
                "ORDER BY d.created_at DESC LIMIT 1",
                (context.turn_id, context.tool_call_id),
            ).fetchone()
            if existing is not None:
                return DeferredExecution(
                    str(existing["waiting_id"]),
                    f"Delegation {existing['delegation_id']} already queued",
                )
        raw_tasks = list(args.get("tasks") or [])
        if not raw_tasks and args.get("prompt"):
            raw_tasks = [
                {
                    "client_id": "task-1",
                    "prompt": args["prompt"],
                    "toolsets": args.get("toolsets", []),
                }
            ]
        if not raw_tasks or len(raw_tasks) > 3:
            raise ValueError("delegate_task requires between 1 and 3 tasks")
        depth = int(context.tool_state.get("delegation_depth", 0))
        if depth >= 2:
            raise ValueError("maximum delegation depth reached")
        existing = self._conn.execute(
            "SELECT COALESCE(SUM(child_count),0) FROM agent_delegations WHERE parent_attempt_id=?",
            (context.attempt_id,),
        ).fetchone()[0]
        if int(existing) + len(raw_tasks) > 3:
            raise ValueError("maximum child Agents for this parent Attempt reached")
        join_policy = str(args.get("join_policy", "all"))
        failure_policy = str(args.get("failure_policy", "collect"))
        if join_policy not in {"all", "any"} or failure_policy != "collect":
            raise ValueError("unsupported delegation join/failure policy")
        delegation_id = uuid.uuid4().hex
        waiting_id = uuid.uuid4().hex
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        common_budget = dict(args.get("budget") or {})
        if "max_steps" in args:
            common_budget["max_loop_iterations"] = args["max_steps"]
        if "timeout_seconds" in args:
            common_budget["max_wall_time_s"] = args["timeout_seconds"]
        normalized_tasks: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_tasks):
            role = resolve_delegation_role(raw.get("role", args.get("role", "general")))
            requested = {str(value) for value in raw.get("toolsets", args.get("toolsets", []))}
            selected = select_role_toolsets(
                role,
                parent_toolsets=allowed_toolsets,
                requested_toolsets=requested,
            )
            requested_budget = {**common_budget, **dict(raw.get("budget") or {})}
            budget = allocate_child_budget(
                role=role,
                requested=requested_budget,
                parent_budget=context.resource_budget,
                parent_usage=context.resource_usage,
                child_count=len(raw_tasks),
            )
            normalized_tasks.append(
                {
                    "client_id": str(raw.get("client_id") or f"task-{index + 1}"),
                    "prompt": str(raw.get("prompt", "")),
                    "role": role,
                    "requested_toolsets": requested,
                    "selected_toolsets": selected,
                    "budget": budget,
                }
            )
        delegation_budget = {
            "children": [
                {"client_id": item["client_id"], "budget": item["budget"]}
                for item in normalized_tasks
            ],
            "parent_budget": context.resource_budget,
            "parent_usage": context.resource_usage,
        }
        try:
            self._conn.execute(
                "INSERT INTO agent_delegations(delegation_id,parent_turn_id,parent_attempt_id,"
                "parent_tool_call_id,principal_id,depth,status,budget_json,prompt,join_policy,"
                "failure_policy,child_count,created_at) VALUES (?,?,?,?,?,?,'running',?,?,?,?,?,?)",
                (
                    delegation_id,
                    context.turn_id,
                    context.attempt_id,
                    context.tool_call_id,
                    context.principal_id,
                    depth + 1,
                    json.dumps(delegation_budget),
                    "",
                    join_policy,
                    failure_policy,
                    len(raw_tasks),
                    now,
                ),
            )
            for index, normalized in enumerate(normalized_tasks):
                client_id = normalized["client_id"]
                prompt = normalized["prompt"]
                if not prompt:
                    raise ValueError("child prompt is required")
                role = normalized["role"]
                requested = normalized["requested_toolsets"]
                selected = normalized["selected_toolsets"]
                budget = normalized["budget"]
                task = Task(
                    task_type="agent.delegate",
                    payload_ref=json.dumps(
                        {
                            "delegation_id": delegation_id,
                            "client_id": client_id,
                            "prompt": prompt,
                            "role": role.name,
                            "read_only": role.read_only,
                            "role_instruction": role.system_instruction,
                            "requested_toolsets": sorted(requested),
                            "toolsets": sorted(selected),
                            "depth": depth + 1,
                            "principal_id": context.principal_id,
                            "session_id": context.session_id,
                            "input_message_id": context.input_message_id,
                            "conversation_id": context.conversation_id,
                            "budget": budget,
                        },
                        ensure_ascii=False,
                    ),
                    status=TaskStatus.queued if index < 2 else TaskStatus.waiting_external,
                    idempotency_key=f"agent.delegate:{delegation_id}:{client_id}",
                    origin="agent_tool",
                    retry_policy={"max_attempts": 2, "backoff_seconds": [2]},
                )
                TaskRepository(self._conn).insert(task)
                self._conn.execute(
                    "INSERT INTO child_task_links(link_id,delegation_id,client_id,task_id,status,"
                    "requested_toolsets,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        delegation_id,
                        client_id,
                        task.task_id,
                        "queued",
                        json.dumps(sorted(selected)),
                        now,
                    ),
                )
            self._conn.execute(
                "INSERT INTO waiting_conditions(waiting_id,owner_type,owner_id,condition_type,"
                "subject_id,payload_json,created_at) VALUES (?,'turn',?,'child_join',?,?,?)",
                (
                    waiting_id,
                    context.turn_id,
                    delegation_id,
                    json.dumps({"tool_call_id": context.tool_call_id}),
                    now,
                ),
            )
            EventStore(self._conn).append(
                Event(
                    event_type="delegation.created",
                    stream_type="delegation",
                    stream_id=delegation_id,
                    producer="delegation-lifecycle",
                    event_class=EventClass.DOMAIN,
                    summary=f"Delegation created: {len(raw_tasks)} children",
                    attributes={
                        "parent_turn_id": context.turn_id,
                        "parent_attempt_id": context.attempt_id,
                        "principal_id": context.principal_id,
                        "child_count": len(raw_tasks),
                        "join_policy": join_policy,
                        "depth": depth + 1,
                    },
                    occurred_at=epoch_ms(now_dt),
                    idempotency_key=f"delegation:{delegation_id}:created",
                ),
                expected_version=0,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        context.tool_state["delegation_depth"] = depth
        return DeferredExecution(waiting_id, f"Delegation {delegation_id} queued")

    def evaluate_for_task(self, task_id: str) -> bool:
        link = self._conn.execute(
            "SELECT delegation_id FROM child_task_links WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if link is None:
            return False
        delegation_id = link["delegation_id"]
        delegation = self._conn.execute(
            "SELECT * FROM agent_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if delegation is None or delegation["status"] in {"completed", "failed", "cancelled"}:
            return delegation is not None
        rows = self._conn.execute(
            "SELECT l.*,t.status AS task_status,t.result_ref AS task_result_ref,"
            "t.payload_ref AS task_payload_ref "
            "FROM child_task_links l "
            "JOIN tasks t ON t.task_id=l.task_id WHERE l.delegation_id=? ORDER BY l.created_at",
            (delegation_id,),
        ).fetchall()
        # Maintain the per-parent concurrency cap by releasing one queued child.
        active = sum(row["task_status"] in {"queued", "running"} for row in rows)
        if active < 2:
            waiting = next((row for row in rows if row["task_status"] == "waiting_external"), None)
            if waiting is not None:
                self._conn.execute(
                    "UPDATE tasks SET status='queued' WHERE task_id=? "
                    "AND status='waiting_external'",
                    (waiting["task_id"],),
                )
        completed = sum(row["task_status"] == "completed" for row in rows)
        failed = sum(row["task_status"] in {"failed", "cancelled"} for row in rows)
        terminal = completed + failed
        aggregate_usage = _aggregate_child_usage(rows)
        satisfied = (
            completed > 0 if delegation["join_policy"] == "any" else terminal == len(rows)
        ) or terminal == len(rows)
        self._conn.execute(
            "UPDATE agent_delegations SET completed_count=?,failed_count=?,usage_json=?,"
            "version=version+1 "
            "WHERE delegation_id=?",
            (completed, failed, json.dumps(aggregate_usage), delegation_id),
        )
        if not satisfied:
            self._conn.commit()
            return False
        if delegation["join_policy"] == "any" and completed > 0:
            now = datetime.now(UTC).isoformat()
            self._cancel_child_execution(delegation_id, now)
            rows = self._conn.execute(
                "SELECT l.*,t.status AS task_status,t.result_ref AS task_result_ref,"
                "t.payload_ref AS task_payload_ref "
                "FROM child_task_links l JOIN tasks t ON t.task_id=l.task_id "
                "WHERE l.delegation_id=? ORDER BY l.created_at",
                (delegation_id,),
            ).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                row["task_status"] != "completed",
                str(row["created_at"]),
                str(row["client_id"]),
            ),
        )
        children = []
        for row in rows:
            try:
                task_payload = json.loads(row["task_payload_ref"] or "{}")
            except (TypeError, json.JSONDecodeError):
                task_payload = {}
            children.append(
                {
                    "client_id": row["client_id"],
                    "task_id": row["task_id"],
                    "turn_id": row["turn_id"],
                    "role": task_payload.get("role", "general"),
                    "requested_toolsets": task_payload.get("requested_toolsets", []),
                    "toolsets": task_payload.get("toolsets", []),
                    "budget": task_payload.get("budget", {}),
                    "status": row["task_status"],
                    "result_summary": row["result_summary"],
                    "result_ref": row["result_ref"] or row["task_result_ref"] or "",
                    "usage": json.loads(row["usage_json"] or "{}"),
                    "error": row["error"],
                }
            )
        result = {
            "delegation_id": delegation_id,
            "status": "completed" if completed else "failed",
            "join_policy": delegation["join_policy"],
            "failure_policy": delegation["failure_policy"],
            "completed_count": completed,
            "failed_count": failed,
            "usage": aggregate_usage,
            "children": children,
        }
        result_json = json.dumps(result, ensure_ascii=False)
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "UPDATE agent_delegations SET status=?,result_text=?,result_ref=?,completed_at=?,"
            "version=version+1 WHERE delegation_id=? AND status IN ('running','cancel_requested')",
            (result["status"], result_json[:100_000], result_json, now, delegation_id),
        )
        self._conn.execute(
            "UPDATE waiting_conditions SET status='satisfied',satisfied_at=?,version=version+1 "
            "WHERE subject_id=? AND condition_type='child_join' AND status='pending'",
            (now, delegation_id),
        )
        self._conn.execute(
            "UPDATE turns SET status='queued',version=version+1 WHERE turn_id=? "
            "AND status='waiting_external' AND active_attempt_id IS NULL",
            (delegation["parent_turn_id"],),
        )
        self._conn.commit()
        return True

    def cancel(
        self,
        delegation_id: str,
        parent_turn_id: str,
        *,
        resume_parent: bool = True,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE agent_delegations SET status='cancelled',cancel_requested_at=?,"
            "completed_at=?,version=version+1 WHERE delegation_id=? AND parent_turn_id=? "
            "AND status IN ('queued','running')",
            (now, now, delegation_id, parent_turn_id),
        )
        if cur.rowcount:
            self._cancel_child_execution(delegation_id, now)
            result = self.status(delegation_id, parent_turn_id) or {
                "delegation_id": delegation_id,
                "status": "cancelled",
                "children": [],
            }
            result["status"] = "cancelled"
            result_json = json.dumps(result, ensure_ascii=False)
            self._conn.execute(
                "UPDATE agent_delegations SET result_text=?,result_ref=? WHERE delegation_id=?",
                (result_json[:100_000], result_json, delegation_id),
            )
            waiting_status = "satisfied" if resume_parent else "cancelled"
            self._conn.execute(
                "UPDATE waiting_conditions SET status=?,satisfied_at=?,version=version+1 "
                "WHERE subject_id=? AND condition_type='child_join' AND status='pending'",
                (waiting_status, now, delegation_id),
            )
            if resume_parent:
                self._conn.execute(
                    "UPDATE turns SET status='queued',version=version+1 "
                    "WHERE turn_id=? AND status='waiting_external' AND active_attempt_id IS NULL",
                    (parent_turn_id,),
                )
        self._conn.commit()
        return bool(cur.rowcount)

    def cancel_for_parent(self, parent_turn_id: str) -> int:
        rows = self._conn.execute(
            "SELECT delegation_id FROM agent_delegations WHERE parent_turn_id=? "
            "AND status IN ('queued','running')",
            (parent_turn_id,),
        ).fetchall()
        return sum(
            self.cancel(row["delegation_id"], parent_turn_id, resume_parent=False) for row in rows
        )

    def list_for_parent(self, parent_turn_id: str) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT delegation_id FROM agent_delegations WHERE parent_turn_id=? "
            "ORDER BY created_at DESC LIMIT 20",
            (parent_turn_id,),
        ).fetchall()
        items = [
            view
            for row in rows
            if (view := self.status(str(row["delegation_id"]), parent_turn_id)) is not None
        ]
        return {"delegations": items, "total": len(items)}

    def status(self, delegation_id: str, parent_turn_id: str) -> dict[str, Any] | None:
        delegation = self._conn.execute(
            "SELECT * FROM agent_delegations WHERE delegation_id=? AND parent_turn_id=?",
            (delegation_id, parent_turn_id),
        ).fetchone()
        if delegation is None:
            return None
        rows = self._conn.execute(
            "SELECT l.*,t.status AS task_status,t.payload_ref AS task_payload_ref,"
            "tr.status AS turn_status,tr.active_attempt_id "
            "FROM child_task_links l JOIN tasks t ON t.task_id=l.task_id "
            "LEFT JOIN turns tr ON tr.turn_id=l.turn_id "
            "WHERE l.delegation_id=? ORDER BY l.created_at,l.client_id",
            (delegation_id,),
        ).fetchall()
        children = []
        for row in rows:
            payload = _json_object(row["task_payload_ref"])
            children.append(
                {
                    "client_id": row["client_id"],
                    "task_id": row["task_id"],
                    "turn_id": row["turn_id"],
                    "attempt_id": row["active_attempt_id"] or "",
                    "role": payload.get("role", "general"),
                    "status": row["task_status"],
                    "turn_status": row["turn_status"] or "",
                    "requested_toolsets": payload.get("requested_toolsets", []),
                    "toolsets": payload.get("toolsets", []),
                    "budget": payload.get("budget", {}),
                    "usage": _json_object(row["usage_json"]),
                    "result_summary": row["result_summary"],
                    "result_ref": row["result_ref"],
                    "error": row["error"],
                }
            )
        return {
            "delegation_id": delegation_id,
            "parent_turn_id": parent_turn_id,
            "depth": delegation["depth"],
            "status": delegation["status"],
            "join_policy": delegation["join_policy"],
            "failure_policy": delegation["failure_policy"],
            "child_count": delegation["child_count"],
            "completed_count": delegation["completed_count"],
            "failed_count": delegation["failed_count"],
            "budget": _json_object(delegation["budget_json"]),
            "usage": _json_object(delegation["usage_json"]),
            "created_at": delegation["created_at"],
            "completed_at": delegation["completed_at"],
            "children": children,
        }

    def _cancel_child_execution(self, delegation_id: str, now: str) -> None:
        turn_ids = [
            str(row["turn_id"])
            for row in self._conn.execute(
                "SELECT turn_id FROM child_task_links WHERE delegation_id=? AND turn_id<>''",
                (delegation_id,),
            ).fetchall()
        ]
        self._conn.execute(
            "UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL "
            "WHERE task_id IN (SELECT task_id FROM child_task_links WHERE delegation_id=?) "
            "AND status IN ('created','scheduled','queued','running','waiting_user',"
            "'waiting_external','retry_scheduled')",
            (delegation_id,),
        )
        self._conn.execute(
            "UPDATE child_task_links SET status='cancelled',completed_at=?,version=version+1 "
            "WHERE delegation_id=? AND status IN ('queued','running','waiting_user')",
            (now, delegation_id),
        )
        for turn_id in turn_ids:
            self._conn.execute(
                "UPDATE run_attempts SET status='cancelled',finished_at=? "
                "WHERE turn_id=? AND status IN ('created','running')",
                (now, turn_id),
            )
            self._conn.execute(
                "UPDATE turns SET status='cancelled',cancel_requested_at=?,"
                "active_attempt_id=NULL,version=version+1 WHERE turn_id=? "
                "AND status IN ('accepted','queued','running','waiting_user','waiting_external')",
                (now, turn_id),
            )

    def reconcile(self, receipt: dict[str, Any]) -> dict[str, str]:
        """Reconcile delegate_task by its globally unique ToolCall operation ID."""
        operation_id = str(receipt.get("operation_id", ""))
        if not operation_id:
            return {"status": "manual_required", "summary": "missing operation_id"}
        row = self._conn.execute(
            "SELECT delegation_id,status FROM agent_delegations "
            "WHERE parent_tool_call_id=? ORDER BY created_at DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        if row is None:
            return {"status": "not_executed", "summary": "delegation was not created"}
        return {
            "status": "succeeded",
            "summary": f"delegation {row['delegation_id']} is {row['status']}",
        }

    def reconcile_manage(self, receipt: dict[str, Any]) -> dict[str, str]:
        try:
            summary = json.loads(str(receipt.get("summary", "{}")))
            delegation_id = str(summary.get("delegation_id", ""))
        except (TypeError, ValueError):
            delegation_id = ""
        if not delegation_id:
            return {"status": "manual_required", "summary": "missing delegation_id"}
        row = self._conn.execute(
            "SELECT status FROM agent_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return {"status": "not_executed", "summary": "delegation does not exist"}
        if row["status"] in {"cancel_requested", "cancelled"}:
            return {"status": "succeeded", "summary": f"delegation is {row['status']}"}
        return {"status": "manual_required", "summary": f"delegation is {row['status']}"}


def _aggregate_child_usage(rows: list[Any]) -> dict[str, int]:
    """Aggregate durable child usage into the parent delegation budget view."""
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    for row in rows:
        try:
            usage = json.loads(row["usage_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        for key in totals:
            value = usage.get(key, 0)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                totals[key] += value
    # Older child records may not contain total_tokens. Keep the aggregate
    # internally consistent without double-counting records that do provide it.
    minimum_total = totals["input_tokens"] + totals["output_tokens"]
    totals["total_tokens"] = max(totals["total_tokens"], minimum_total)
    return totals


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
