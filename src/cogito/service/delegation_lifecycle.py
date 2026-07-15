"""Durable parent/child Agent delegation lifecycle."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from cogito.capability.models import DeferredExecution, ToolContext
from cogito.domain.task import Task, TaskStatus
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
        now = datetime.now(UTC).isoformat()
        budget = {
            "max_loop_iterations": min(int(args.get("max_steps", 6)), 8),
            "max_model_calls": 10,
            "max_tool_calls": 20,
            "max_input_tokens": 16_000,
            "max_output_tokens": 4_096,
            "max_wall_time_s": min(int(args.get("timeout_seconds", 120)), 120),
            "max_cost": 0.0,
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
                    json.dumps(budget),
                    "",
                    join_policy,
                    failure_policy,
                    len(raw_tasks),
                    now,
                ),
            )
            for index, raw in enumerate(raw_tasks):
                client_id = str(raw.get("client_id") or f"task-{index + 1}")
                prompt = str(raw.get("prompt", ""))
                if not prompt:
                    raise ValueError("child prompt is required")
                requested = {str(value) for value in raw.get("toolsets", [])}
                selected = allowed_toolsets & requested if requested else allowed_toolsets
                task = Task(
                    task_type="agent.delegate",
                    payload_ref=json.dumps(
                        {
                            "delegation_id": delegation_id,
                            "client_id": client_id,
                            "prompt": prompt,
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
        rows = self._conn.execute(
            "SELECT l.*,t.status AS task_status,t.result_ref AS task_result_ref "
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
            self._conn.execute(
                "UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL "
                "WHERE task_id IN (SELECT task_id FROM child_task_links "
                "WHERE delegation_id=?) AND status IN "
                "('queued','running','waiting_user','waiting_external')",
                (delegation_id,),
            )
            self._conn.execute(
                "UPDATE child_task_links SET status='cancelled',completed_at=?,version=version+1 "
                "WHERE delegation_id=? AND status IN ('queued','running','waiting_user')",
                (now, delegation_id),
            )
            rows = self._conn.execute(
                "SELECT l.*,t.status AS task_status,t.result_ref AS task_result_ref "
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
        children = [
            {
                "client_id": row["client_id"],
                "task_id": row["task_id"],
                "turn_id": row["turn_id"],
                "status": row["task_status"],
                "result_summary": row["result_summary"],
                "result_ref": row["result_ref"] or row["task_result_ref"] or "",
                "usage": json.loads(row["usage_json"] or "{}"),
                "error": row["error"],
            }
            for row in rows
        ]
        result = {
            "delegation_id": delegation_id,
            "status": "completed" if completed else "failed",
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

    def cancel(self, delegation_id: str, parent_turn_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE agent_delegations SET status='cancel_requested',cancel_requested_at=?,"
            "version=version+1 WHERE delegation_id=? AND parent_turn_id=? "
            "AND status IN ('queued','running')",
            (now, delegation_id, parent_turn_id),
        )
        if cur.rowcount:
            self._conn.execute(
                "UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL "
                "WHERE task_id IN (SELECT task_id FROM child_task_links WHERE delegation_id=?) "
                "AND status IN ('queued','running','waiting_user','waiting_external')",
                (delegation_id,),
            )
            self._conn.execute(
                "UPDATE child_task_links SET status='cancelled',completed_at=?,version=version+1 "
                "WHERE delegation_id=? AND status IN ('queued','running','waiting_user')",
                (now, delegation_id),
            )
        self._conn.commit()
        return bool(cur.rowcount)
    def cancel_for_parent(self, parent_turn_id: str) -> int:
        rows = self._conn.execute(
            "SELECT delegation_id FROM agent_delegations WHERE parent_turn_id=? "
            "AND status IN ('queued','running')",
            (parent_turn_id,),
        ).fetchall()
        return sum(self.cancel(row["delegation_id"], parent_turn_id) for row in rows)

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
