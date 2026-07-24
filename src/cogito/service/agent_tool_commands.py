"""Transactional Command handlers invoked by Agent Tools."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.schedule import MisfirePolicy, Schedule, ScheduleType, next_fire_at
from cogito.service.api.audit import write_audit
from cogito.store.event_store import EventStore
from cogito.store.schedule_repo import ScheduleRepository


class AgentToolCommandService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def create_schedule(self, args: dict[str, Any], *, actor: str, tool_call_id: str) -> Schedule:
        expression = str(args["expression"])
        timezone = str(args.get("timezone", "UTC"))
        next_at = next_fire_at(expression, timezone, datetime.now(UTC))
        if next_at is None:
            raise ValueError("invalid schedule expression")
        action_hash = _action_hash(args)
        existing = self._command_result("CreateAgentSchedule", tool_call_id, action_hash)
        if existing is not None:
            schedule = ScheduleRepository(self._conn).get(str(existing["aggregate_id"]))
            if schedule is not None:
                return schedule
        schedule = Schedule(
            schedule_type=ScheduleType(str(args.get("schedule_type", "interval"))),
            expression=expression,
            timezone=timezone,
            misfire_policy=MisfirePolicy.run_once,
            max_catch_up=1,
            next_fire_at=next_at,
            task_type="agent.prompt",
            task_payload=json.dumps(
                {
                    "prompt": str(args["prompt"]),
                    "principal_id": actor,
                    "session_id": str(args.get("session_id", "")),
                    "model_role": "main",
                },
                ensure_ascii=False,
            ),
        )
        try:
            ScheduleRepository(self._conn).insert(schedule)
            self._record_command(
                "CreateAgentSchedule",
                schedule.schedule_id,
                actor,
                tool_call_id,
                action_hash,
                {"schedule_id": schedule.schedule_id},
            )
            self._conn.commit()
            return schedule
        except Exception:
            self._conn.rollback()
            raise

    def cancel_schedule(
        self,
        schedule_id: str,
        expected_version: int,
        *,
        actor: str,
        tool_call_id: str,
    ) -> None:
        repo = ScheduleRepository(self._conn)
        schedule = repo.get(schedule_id)
        if schedule is None or schedule.task_type != "agent.prompt":
            raise ValueError("schedule not found")
        args = {"schedule_id": schedule_id, "expected_version": expected_version}
        action_hash = _action_hash(args)
        if self._command_result("CancelAgentSchedule", tool_call_id, action_hash) is not None:
            return
        try:
            if not repo.update_enabled_expected(schedule_id, False, expected_version):
                raise ValueError("schedule version conflict")
            self._record_command(
                "CancelAgentSchedule",
                schedule_id,
                actor,
                tool_call_id,
                action_hash,
                {"schedule_id": schedule_id, "cancelled": True},
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def manage_skill(
        self,
        *,
        root: Path,
        action: str,
        name: str,
        raw: str,
        manifest: Any | None,
        expected_version: str,
        actor: str,
        tool_call_id: str,
    ) -> dict[str, str]:
        root = root.resolve()
        archive_root = root / ".archive"
        active = root / name
        archived = archive_root / name
        row = self._conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        action_hash = _action_hash(
            {
                "action": action,
                "name": name,
                "content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "expected_version": expected_version,
            }
        )
        command_name = f"{action.title()}Skill"
        prior = self._command_result(command_name, tool_call_id, action_hash)
        if prior is not None:
            return {
                "name": str(prior["result"].get("name", name)),
                "status": str(prior["result"].get("status", "")),
                "version": str(prior["result"].get("version", "")),
            }
        backup_root = root / f".command-backup-{uuid.uuid4().hex}"
        source: Path | None = None
        if active.exists():
            source = active
        elif archived.exists():
            source = archived
        if source is not None:
            shutil.copytree(source, backup_root)
        try:
            now = datetime.now(UTC).isoformat()
            if action in {"create", "update"}:
                if action == "create" and (row is not None or active.exists() or archived.exists()):
                    raise ValueError("skill already exists")
                if action == "update":
                    if (
                        row is None
                        or row["status"] != "active"
                        or row["version"] != expected_version
                    ):
                        raise ValueError("skill version conflict")
                target = active / "SKILL.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_text(raw, encoding="utf-8")
                os.replace(tmp, target)
                if action == "create":
                    skill_id = uuid.uuid4().hex
                    self._conn.execute(
                        "INSERT INTO skills("
                        "skill_id,name,status,version,description,created_at,updated_at) "
                        "VALUES (?,?,'active',?,?,?,?)",
                        (skill_id, name, manifest.version, manifest.description, now, now),
                    )
                else:
                    skill_id = row["skill_id"]
                    self._conn.execute(
                        "UPDATE skills SET version=?,description=?,updated_at=? WHERE skill_id=?",
                        (manifest.version, manifest.description, now, skill_id),
                    )
                status, version = "active", manifest.version
            elif action == "archive":
                if row is None or row["status"] != "active" or row["version"] != expected_version:
                    raise ValueError("skill version conflict")
                if archived.exists():
                    raise ValueError("archived skill already exists")
                archived.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(active), str(archived))
                self._conn.execute(
                    "UPDATE skills SET status='archived',archived_at=?,updated_at=? "
                    "WHERE skill_id=?",
                    (now, now, row["skill_id"]),
                )
                skill_id, status, version = row["skill_id"], "archived", row["version"]
            elif action == "restore":
                if row is None or row["status"] != "archived" or row["version"] != expected_version:
                    raise ValueError("skill version conflict")
                if active.exists():
                    raise ValueError("active skill already exists")
                shutil.move(str(archived), str(active))
                self._conn.execute(
                    "UPDATE skills SET status='active',archived_at=NULL,updated_at=? "
                    "WHERE skill_id=?",
                    (now, row["skill_id"]),
                )
                skill_id, status, version = row["skill_id"], "active", row["version"]
            else:
                raise ValueError("unsupported skill action")
            self._record_command(
                command_name,
                skill_id,
                actor,
                tool_call_id,
                action_hash,
                {"name": name, "status": status, "version": version},
            )
            self._conn.commit()
            shutil.rmtree(backup_root, ignore_errors=True)
            return {"name": name, "status": status, "version": version}
        except Exception:
            self._conn.rollback()
            if active.exists():
                shutil.rmtree(active, ignore_errors=True)
            if archived.exists():
                shutil.rmtree(archived, ignore_errors=True)
            if backup_root.exists() and source is not None:
                destination = active if source == active else archived
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup_root), str(destination))
            raise

    def _record_command(
        self,
        command: str,
        aggregate_id: str,
        actor: str,
        tool_call_id: str,
        action_hash: str,
        payload: dict[str, Any],
    ) -> None:
        idempotency_key = f"{tool_call_id}:{action_hash}"
        write_audit(
            self._conn,
            actor_id=actor,
            action=command,
            target_type="agent_tool_command",
            target_id=aggregate_id,
            changes={"idempotency_key": idempotency_key, **payload},
            trace_id=tool_call_id,
            commit=False,
        )
        EventStore(self._conn).append(
            Event(
                event_type="agent.command.completed",
                stream_type="agent_tool_command",
                stream_id=aggregate_id,
                producer="agent-tool-command-service",
                event_class=EventClass.DOMAIN,
                context=EventContext(
                    trace_id=tool_call_id,
                    correlation_id=tool_call_id,
                    actor_id=actor,
                    principal_id=actor,
                ),
                summary=f"Agent command completed: {command}",
                attributes={
                    "command": command,
                    "aggregate_id": aggregate_id,
                    **{
                        key: value
                        for key, value in payload.items()
                        if isinstance(value, str | int | float | bool)
                    },
                },
                outcome="completed",
                idempotency_key=f"agent-command:{idempotency_key}",
            )
        )
        self._conn.execute(
            "INSERT INTO agent_tool_command_results("
            "command_name,idempotency_key,action_hash,actor_id,aggregate_id,result_json,created_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                command,
                idempotency_key,
                action_hash,
                actor,
                aggregate_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _command_result(
        self,
        command: str,
        tool_call_id: str,
        action_hash: str,
    ) -> dict[str, Any] | None:
        key = f"{tool_call_id}:{action_hash}"
        row = self._conn.execute(
            "SELECT aggregate_id,result_json FROM agent_tool_command_results "
            "WHERE command_name=? AND idempotency_key=?",
            (command, key),
        ).fetchone()
        if row is None:
            return None
        return {
            "aggregate_id": str(row["aggregate_id"]),
            "result": json.loads(row["result_json"] or "{}"),
        }

    def reconcile(self, receipt: dict[str, Any]) -> dict[str, str]:
        operation_id = str(receipt.get("operation_id", ""))
        if not operation_id:
            return {"status": "manual_required", "summary": "missing operation_id"}
        row = self._conn.execute(
            "SELECT command_name,aggregate_id FROM agent_tool_command_results "
            "WHERE idempotency_key LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f"{operation_id}:%",),
        ).fetchone()
        if row is None:
            return {"status": "not_executed", "summary": "Command was not committed"}
        return {
            "status": "succeeded",
            "summary": f"{row['command_name']} committed for {row['aggregate_id']}",
        }


def _action_hash(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
