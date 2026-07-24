"""Drift run / skill-state 持久化。

drift_runs.status 是查询投影，必须由同一事务或 Event Consumer 更新。
tasks/task_attempts 是生命周期权威 —— 本仓库不复制 Task 状态。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore


class DriftRunRepository:
    def __init__(self, conn: sqlite3.Connection, *, event_sourced: bool = False) -> None:
        self._conn = conn
        self._event_sourced = event_sourced

    def insert(
        self,
        *,
        task_id: str,
        principal_id: str,
        skill_name: str,
        skill_version: str,
        admission_snapshot: dict[str, Any],
        status: str = "admitted",
        selection_trace_json: dict[str, Any] | None = None,
        selector_version: str | None = None,
    ) -> str:
        now = int(time.time() * 1000)
        run_id = f"dr-{uuid.uuid4().hex[:16]}"
        if self._event_sourced:
            EventStore(self._conn).append(
                Event(
                    event_type="drift.run.admitted",
                    stream_type="drift_run",
                    stream_id=run_id,
                    producer="drift-run-repository",
                    event_class=EventClass.DOMAIN,
                    context=EventContext(principal_id=principal_id, task_id=task_id),
                    summary=f"Drift run admitted: {skill_name}"[:2_000],
                    attributes={
                        "skill_name": skill_name,
                        "skill_version": skill_version,
                        "status": status,
                        "selector_version": selector_version or "",
                    },
                    outcome=status,
                    occurred_at=now,
                    idempotency_key=f"drift-run:{run_id}:admitted",
                ),
                expected_version=0,
            )
            return run_id
        self._conn.execute(
            "INSERT INTO drift_runs "
            "(drift_run_id, task_id, principal_id, skill_name, skill_version, "
            " status, admission_snapshot_json, created_at, "
            " selection_trace_json, selector_version) "
            "VALUES (?,?,?,?,?,?,?,?, ?,?)",
            (
                run_id,
                task_id,
                principal_id,
                skill_name,
                skill_version,
                status,
                json.dumps(admission_snapshot, ensure_ascii=False),
                now,
                json.dumps(selection_trace_json, ensure_ascii=False)
                if selection_trace_json is not None
                else None,
                selector_version,
            ),
        )
        self._conn.commit()
        return run_id

    def update_status(self, drift_run_id: str, status: str, **fields: Any) -> None:
        """原子更新 status + 可选字段 (finish_summary, finished_at, candidate_id,...)。"""
        now = int(time.time() * 1000)
        if self._event_sourced:
            event_type = {
                "paused": "drift.run.paused",
                "completed": "drift.run.completed",
                "failed": "drift.run.failed",
                "needs_review": "drift.run.needs_review",
            }.get(status)
            if event_type is None:
                raise ValueError(f"unsupported drift status event: {status}")
            stream = EventStore(self._conn).read_stream("drift_run", drift_run_id)
            if not stream:
                raise ValueError(f"unknown drift run: {drift_run_id}")
            source = stream[-1]
            EventStore(self._conn).append(
                Event(
                    event_type=event_type,
                    stream_type="drift_run",
                    stream_id=drift_run_id,
                    producer="drift-run-repository",
                    event_class=(
                        EventClass.OPERATION if status == "paused" else EventClass.DOMAIN
                    ),
                    context=EventContext(
                        trace_id=source.context.trace_id,
                        correlation_id=source.context.correlation_id,
                        causation_id=source.event_id,
                        principal_id=source.context.principal_id,
                        task_id=source.context.task_id,
                    ),
                    summary=str(fields.get("finish_summary") or f"Drift run {status}"),
                    attributes={
                        "reason": str(fields.get("preemption_reason") or ""),
                        "candidate_id": str(fields.get("candidate_id") or ""),
                    },
                    payload_ref=fields.get("result_ref"),
                    outcome=status,
                    occurred_at=now,
                    idempotency_key=f"drift-run:{drift_run_id}:{status}:{stream[-1].stream_version}",
                ),
                expected_version=stream[-1].stream_version,
            )
            return
        sets = ["status=?", "finished_at=COALESCE(finished_at, ?)"]
        vals: list[Any] = [status, now]
        # budget_used_json 字段名映射
        if "budget_used" in fields and "budget_used_json" not in fields:
            fields["budget_used_json"] = fields.pop("budget_used")
        for k, v in fields.items():
            if k == "budget_used_json" and isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(drift_run_id)
        self._conn.execute(
            f"UPDATE drift_runs SET {', '.join(sets)} WHERE drift_run_id=?",
            vals,
        )
        self._conn.commit()

    def update_progress(
        self, drift_run_id: str, *, budget_used: dict[str, int], steps_taken: int
    ) -> None:
        """写入 steps + budget 的绝对累计值（不是 delta）。

        DriftRunner 在 resume 时把 budget_used/steps_taken 作为"自 admission
        以来累计总值"传入（基线在 resume_budget/resume_from_step），因此这里
        是 REPLACE 语义——保持与 Skill 选择/任务执行一次完成的单次 Attempt 行为
        一致：每个 attempt 的 finish_drift 写入它盯到的真实累计量。
        """
        if self._event_sourced:
            stream = EventStore(self._conn).read_stream("drift_run", drift_run_id)
            if not stream:
                raise ValueError(f"unknown drift run: {drift_run_id}")
            source = stream[-1]
            EventStore(self._conn).append(
                Event(
                    event_type="drift.run.progress.recorded",
                    stream_type="drift_run",
                    stream_id=drift_run_id,
                    producer="drift-run-repository",
                    event_class=EventClass.OPERATION,
                    context=EventContext(
                        trace_id=source.context.trace_id,
                        correlation_id=source.context.correlation_id,
                        causation_id=source.event_id,
                        principal_id=source.context.principal_id,
                        task_id=source.context.task_id,
                    ),
                    summary="Drift run progress recorded",
                    attributes={"steps_taken": int(steps_taken), "budget_used": dict(budget_used)},
                    outcome="running",
                    idempotency_key=(
                        f"drift-run:{drift_run_id}:progress:{int(steps_taken)}"
                    ),
                ),
                expected_version=stream[-1].stream_version,
            )
            return
        self._conn.execute(
            "UPDATE drift_runs SET budget_used_json=?, steps_taken=? WHERE drift_run_id=?",
            (json.dumps(dict(budget_used), ensure_ascii=False), int(steps_taken), drift_run_id),
        )
        self._conn.commit()

    def record_checkpoint(self, drift_run_id: str, payload_ref: str, payload_hash: str = "") -> None:
        """Attach the latest guarded checkpoint to the Drift event stream."""
        if not self._event_sourced:
            self._conn.execute(
                "UPDATE drift_runs SET result_ref=? WHERE drift_run_id=?",
                (payload_ref, drift_run_id),
            )
            return
        stream = EventStore(self._conn).read_stream("drift_run", drift_run_id)
        if not stream:
            raise ValueError(f"unknown drift run: {drift_run_id}")
        source = stream[-1]
        EventStore(self._conn).append(
            Event(
                event_type="drift.run.checkpoint.recorded",
                stream_type="drift_run",
                stream_id=drift_run_id,
                producer="drift-run-repository",
                event_class=EventClass.OPERATION,
                context=EventContext(
                    trace_id=source.context.trace_id,
                    correlation_id=source.context.correlation_id,
                    causation_id=source.event_id,
                    principal_id=source.context.principal_id,
                    task_id=source.context.task_id,
                ),
                summary="Drift checkpoint recorded",
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                outcome="checkpointed",
                idempotency_key=(
                    f"drift-run:{drift_run_id}:checkpoint:{payload_ref}"
                ),
            ),
            expected_version=source.stream_version,
        )

    def get(self, drift_run_id: str) -> dict[str, Any] | None:
        if self._event_sourced:
            return self._event_run(drift_run_id)
        row = self._conn.execute(
            "SELECT * FROM drift_runs WHERE drift_run_id=?",
            (drift_run_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def has_active_run(self, principal_id: str) -> bool:
        """同 Principal 是否已有 active Drift (status admitted/running/waiting/paused)。"""
        if self._event_sourced:
            return any(
                run["principal_id"] == principal_id
                and run["status"] in {"admitted", "running", "waiting", "paused"}
                for run in self._event_runs()
            )
        row = self._conn.execute(
            "SELECT 1 FROM drift_runs WHERE principal_id=? AND status IN "
            "('admitted','running','waiting','paused') LIMIT 1",
            (principal_id,),
        ).fetchone()
        return row is not None

    def list_runs(self, principal_id: str | None = None) -> list[dict[str, Any]]:
        """Return replayed runs in the Event path, with a legacy read fallback."""
        if self._event_sourced:
            runs = self._event_runs()
            return [run for run in runs if principal_id is None or run["principal_id"] == principal_id]
        if principal_id is None:
            rows = self._conn.execute("SELECT * FROM drift_runs").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM drift_runs WHERE principal_id=?", (principal_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def _event_run(self, drift_run_id: str) -> dict[str, Any] | None:
        stream = EventStore(self._conn).read_stream("drift_run", drift_run_id)
        if not stream:
            return None
        first = stream[0]
        if first.event_type not in {"drift.run.admitted", "drift.run.imported"}:
            return None
        data: dict[str, Any] = {
            "drift_run_id": drift_run_id,
            "task_id": first.context.task_id,
            "principal_id": first.context.principal_id,
            "skill_name": str(first.attributes.get("skill_name") or ""),
            "skill_version": str(first.attributes.get("skill_version") or ""),
            "status": first.outcome or str(first.attributes.get("status") or "admitted"),
            "steps_taken": int(first.attributes.get("steps_taken") or 0),
            "budget_used_json": "{}",
            "result_ref": None,
            "preemption_reason": first.attributes.get("preemption_reason") or None,
            "finish_summary": "",
            "created_at": first.occurred_at,
            "finished_at": None,
        }
        for event in stream[1:]:
            if event.event_type == "drift.run.progress.recorded":
                data["steps_taken"] = int(event.attributes.get("steps_taken") or 0)
                data["budget_used_json"] = json.dumps(
                    event.attributes.get("budget_used") or {}, ensure_ascii=False
                )
            elif event.event_type == "drift.run.checkpoint.recorded":
                data["result_ref"] = event.payload_ref or data["result_ref"]
            elif event.event_type.startswith("drift.run."):
                data["status"] = event.outcome or event.event_type.rsplit(".", 1)[-1]
                data["result_ref"] = event.payload_ref or data["result_ref"]
                data["preemption_reason"] = (
                    event.attributes.get("reason") or data["preemption_reason"]
                )
                data["finish_summary"] = event.summary or data["finish_summary"]
                data["finished_at"] = event.occurred_at
        return data

    def _event_runs(self) -> list[dict[str, Any]]:
        ids = {
            event.stream_id
            for event in EventStore(self._conn).read_stream_type("drift_run")
        }
        return [run for run_id in ids if (run := self._event_run(run_id)) is not None]


class DriftSkillStateRepository:
    def __init__(self, conn: sqlite3.Connection, *, event_sourced: bool = False) -> None:
        self._conn = conn
        self._event_sourced = event_sourced

    def get(self, principal_id: str, skill_name: str) -> dict[str, Any] | None:
        if self._event_sourced:
            stream = EventStore(self._conn).read_stream(
                "drift_skill_state", self._stream_id(principal_id, skill_name)
            )
            if not stream:
                return None
            event = stream[-1]
            return {
                "principal_id": principal_id,
                "skill_name": skill_name,
                "skill_version": str(event.attributes.get("skill_version") or ""),
                "last_status": str(event.attributes.get("last_status") or ""),
                "last_run_at": event.attributes.get("last_run_at"),
                "run_count": int(event.attributes.get("run_count") or 0),
                "checkpoint_ref": event.payload_ref,
                "updated_at": event.occurred_at,
            }
        row = self._conn.execute(
            "SELECT * FROM drift_skill_state WHERE principal_id=? AND skill_name=?",
            (principal_id, skill_name),
        ).fetchone()
        return dict(row) if row else None

    def upsert(self, principal_id: str, skill_name: str, skill_version: str, **fields: Any) -> None:
        now = int(time.time() * 1000)
        if self._event_sourced:
            existing = self.get(principal_id, skill_name)
            run_count = int((existing or {}).get("run_count") or 0) + int(
                fields.get("run_count") or 0
            )
            stream_id = self._stream_id(principal_id, skill_name)
            stream = EventStore(self._conn).read_stream("drift_skill_state", stream_id)
            EventStore(self._conn).append(
                Event(
                    event_type="drift.skill_state.updated",
                    stream_type="drift_skill_state",
                    stream_id=stream_id,
                    producer="drift-skill-state-repository",
                    event_class=EventClass.OPERATION,
                    context=EventContext(principal_id=principal_id),
                    summary=f"Drift skill state updated: {skill_name}"[:2_000],
                    attributes={
                        "skill_name": skill_name,
                        "skill_version": skill_version,
                        "last_status": str(fields.get("last_status") or ""),
                        "last_run_at": fields.get("last_run_at"),
                        "run_count": run_count,
                    },
                    payload_ref=fields.get("checkpoint_ref"),
                    outcome=str(fields.get("last_status") or ""),
                    occurred_at=now,
                    idempotency_key=(
                        f"drift-skill:{stream_id}:updated:{run_count}:{fields.get('last_run_at', now)}"
                    ),
                ),
                expected_version=stream[-1].stream_version if stream else 0,
            )
            return
        existing = self.get(principal_id, skill_name)
        if existing is None:
            self._conn.execute(
                "INSERT INTO drift_skill_state "
                "(principal_id, skill_name, skill_version, last_status, "
                " last_run_at, run_count, cursor_json, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    principal_id,
                    skill_name,
                    skill_version,
                    fields.get("last_status"),
                    fields.get("last_run_at"),
                    fields.get("run_count", 1),
                    json.dumps(fields.get("cursor", {}), ensure_ascii=False),
                    now,
                ),
            )
            self._conn.commit()
            return
        # 更新
        sets = ["skill_version=?", "updated_at=?"]
        vals: list[Any] = [skill_version, now]
        for k, v in fields.items():
            if k == "run_count":
                sets.append("run_count=run_count+?")
                vals.append(v)
            elif k == "cursor":
                sets.append("cursor_json=?")
                vals.append(json.dumps(v, ensure_ascii=False))
            else:
                sets.append(f"{k}=?")
                vals.append(v)
        vals.extend([principal_id, skill_name])
        self._conn.execute(
            f"UPDATE drift_skill_state SET {', '.join(sets)} WHERE principal_id=? AND skill_name=?",
            vals,
        )
        self._conn.commit()

    def all_states(self, principal_id: str) -> list[dict[str, Any]]:
        if self._event_sourced:
            stream_ids = {
                event.stream_id
                for event in EventStore(self._conn).read_stream_type("drift_skill_state")
                if event.context.principal_id == principal_id
            }
            states = []
            for stream_id in stream_ids:
                skill_name = stream_id.split(":", 1)[-1]
                state = self.get(principal_id, skill_name)
                if state is not None:
                    states.append(state)
            return sorted(states, key=lambda state: int(state.get("updated_at") or 0), reverse=True)
        rows = self._conn.execute(
            "SELECT * FROM drift_skill_state WHERE principal_id=?",
            (principal_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _stream_id(principal_id: str, skill_name: str) -> str:
        return f"{principal_id}:{skill_name}"
