"""Dispatcher — 领取 queued Turn、创建 RunAttempt、推进到 running。

RUNTIME-FLOWS / 3.2 优先级：数值越大越重要。
EXECUTION-LIFECYCLE / 3.2：同一事务内完成 Turn→running + RunAttempt 创建。
GLOBAL-INVARIANTS / 2.5：旧 Lease/旧版本 Attempt 的结果不得提交业务状态。

complete/fail/heartbeat 必须验证：
- Turn.status = running
- Turn.version 匹配
- Turn.active_attempt_id = attempt_id
- RunAttempt.status = running
- worker_id 匹配
- lease_version 匹配
- lease_expires_at > now
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, NamedTuple

from cogito.contracts.clock import Clock, ProductionClock, epoch_ms, from_epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.turn import RunAttempt, RunAttemptStatus, Turn, TurnStatus
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.event_replay import RunAttemptProjection, TurnProjection, replay_run_attempt, replay_turn
from cogito.store.event_store import EventStore, StreamVersionConflictError

DEFAULT_LEASE_TTL_S = 120
_LOGGER = logging.getLogger("cogito.dispatcher")


class ClaimedRun(NamedTuple):
    turn: Turn
    attempt: RunAttempt


class Dispatcher:
    """Turn 调度器 —— 领取、运行、完成、心跳。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        self._lease_ttl_s = lease_ttl_s
        self._clock = clock or ProductionClock()

    def _now(self, override: datetime | None = None) -> datetime:
        return override if override is not None else self._clock.now()

    # ── Canonical Event path ──────────────────────────────────────────────

    def _event_turn(self, turn_id: str) -> tuple[TurnProjection, list[Event]] | None:
        events = EventStore(self._conn).read_stream("turn", turn_id)
        state = replay_turn(events, turn_id)
        return (state, events) if state is not None else None

    def _event_attempt(self, attempt_id: str) -> tuple[RunAttemptProjection, list[Event]] | None:
        events = EventStore(self._conn).read_stream("run_attempt", attempt_id)
        state = replay_run_attempt(events, attempt_id)
        return (state, events) if state is not None else None

    def _event_turns(self) -> list[tuple[TurnProjection, list[Event]]]:
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("turn"):
            grouped.setdefault(event.stream_id, []).append(event)
        return [
            (state, stream)
            for turn_id, stream in grouped.items()
            if (state := replay_turn(stream, turn_id)) is not None
        ]

    def _event_attempts_for_turn(self, turn_id: str) -> list[RunAttemptProjection]:
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("run_attempt"):
            if event.context.turn_id == turn_id:
                grouped.setdefault(event.stream_id, []).append(event)
        return [
            state
            for attempt_id, stream in grouped.items()
            if (state := replay_run_attempt(stream, attempt_id)) is not None
        ]

    @staticmethod
    def _turn_from_event(state: TurnProjection) -> Turn:
        return Turn(
            turn_id=state.turn_id,
            session_id=state.session_id,
            input_message_id=state.input_message_id,
            status=TurnStatus(state.status),
            priority=state.priority or 80,
            version=state.stream_version,
            cancel_requested_at=from_epoch_ms(state.cancel_requested_at),
            active_attempt_id=state.active_attempt_id or None,
            final_message_id=state.final_message_id or None,
            created_at=from_epoch_ms(state.created_at),
            completed_at=from_epoch_ms(state.completed_at),
        )

    @staticmethod
    def _attempt_from_event(state: RunAttemptProjection) -> RunAttempt:
        return RunAttempt(
            attempt_id=state.attempt_id,
            turn_id=state.turn_id,
            attempt_no=state.attempt_no,
            status=RunAttemptStatus(state.status),
            checkpoint_ref=state.checkpoint_ref,
            started_at=from_epoch_ms(state.started_at),
            finished_at=from_epoch_ms(state.finished_at),
            worker_id=state.worker_id,
            lease_version=state.lease_version,
            lease_expires_at=from_epoch_ms(state.lease_expires_at),
            error_ref=state.error_ref or "",
        )

    def _event_context(
        self, stream: list[Event], *, turn_id: str, attempt_id: str = "", causation_id: str = ""
    ) -> EventContext:
        source = stream[-1] if stream else None
        base = source.context if source else EventContext()
        return replace(
            base,
            causation_id=causation_id or (source.event_id if source else base.causation_id),
            turn_id=turn_id,
            attempt_id=attempt_id or base.attempt_id,
        )

    def _append_event_turn(
        self,
        *,
        stream: list[Event],
        state: TurnProjection,
        event_type: str,
        outcome: str,
        context: EventContext,
        attributes: dict[str, Any] | None = None,
        producer: str = "dispatcher",
        summary: str = "",
    ) -> Event:
        return EventStore(self._conn).append(
            Event(
                event_type=event_type,
                stream_type="turn",
                stream_id=state.turn_id,
                producer=producer,
                event_class=(
                    EventClass.OPERATION
                    if event_type in {
                        "runtime.turn.started",
                        "runtime.turn.waiting_user",
                        "runtime.turn.waiting_external",
                    }
                    else EventClass.DOMAIN
                ),
                context=context,
                summary=summary or f"Turn {outcome}",
                attributes=attributes or {},
                outcome=outcome,
                occurred_at=epoch_ms(self._clock.now()),
                idempotency_key=f"turn:{state.turn_id}:{event_type}:{state.stream_version + 1}",
            ),
            expected_version=state.stream_version,
        )

    def _append_event_attempt(
        self,
        *,
        attempt: RunAttempt,
        stream: list[Event],
        event_type: str,
        outcome: str,
        context: EventContext,
        attributes: dict[str, Any] | None = None,
        payload_ref: str | None = None,
    ) -> Event:
        return EventStore(self._conn).append(
            Event(
                event_type=event_type,
                stream_type="run_attempt",
                stream_id=attempt.attempt_id,
                producer="dispatcher",
                event_class=EventClass.OPERATION,
                context=context,
                summary=f"Run attempt {outcome}",
                attributes=attributes or {},
                payload_ref=payload_ref,
                outcome=outcome,
                occurred_at=epoch_ms(self._clock.now()),
                idempotency_key=f"attempt:{attempt.attempt_id}:{event_type}:{len(stream) + 1}",
            ),
            expected_version=len(stream),
        )

    def _event_claim(self, state: TurnProjection, stream: list[Event], worker_id: str, now: datetime,
                     *, checkpoint_ref: str = "") -> ClaimedRun | None:
        if state.status not in {"queued", "waiting_user", "waiting_external", "failed", "expired"}:
            return None
        if state.active_attempt_id:
            return None
        attempts = self._event_attempts_for_turn(state.turn_id)
        attempt = RunAttempt(
            attempt_id=uuid.uuid4().hex,
            turn_id=state.turn_id,
            attempt_no=max((item.attempt_no for item in attempts), default=0) + 1,
            status=RunAttemptStatus.running,
            checkpoint_ref=checkpoint_ref or None,
            started_at=now,
            worker_id=worker_id,
            lease_version=1,
            lease_expires_at=from_epoch_ms(epoch_ms(now) + self._lease_ttl_s * 1000),
        )
        try:
            started = self._append_event_turn(
                stream=stream,
                state=state,
                event_type="runtime.turn.started",
                outcome="running",
                context=self._event_context(stream, turn_id=state.turn_id, attempt_id=attempt.attempt_id),
                attributes={
                    "active_attempt_id": attempt.attempt_id,
                    "worker_id": worker_id,
                    "attempt_no": attempt.attempt_no,
                    "resumed": state.status != "queued",
                },
            )
            self._append_event_attempt(
                attempt=attempt,
                stream=[],
                event_type="runtime.attempt.started",
                outcome="running",
                context=replace(started.context, causation_id=started.event_id, attempt_id=attempt.attempt_id),
                attributes={
                    "attempt_no": attempt.attempt_no,
                    "worker_id": worker_id,
                    "lease_version": 1,
                    "lease_expires_at": epoch_ms(attempt.lease_expires_at),
                    "resumed": state.status != "queued",
                },
                payload_ref=checkpoint_ref or None,
            )
        except StreamVersionConflictError:
            return None
        turn = self._turn_from_event(replay_turn([*stream, started], state.turn_id) or state)
        return ClaimedRun(turn=turn, attempt=attempt)

    def _event_complete_or_fail(
        self, *, turn_id: str, attempt_id: str, expected_turn_version: int, worker_id: str,
        lease_version: int, terminal: str, final_message_id: str | None = None,
        event_context: EventContext | None = None, event_producer: str = "dispatcher",
        event_summary: str = "", event_attributes: dict[str, Any] | None = None,
        now: datetime,
    ) -> bool:
        current = self._event_turn(turn_id)
        attempt_current = self._event_attempt(attempt_id)
        if current is None or attempt_current is None:
            return False
        state, stream = current
        attempt_state, attempt_stream = attempt_current
        now_ms = epoch_ms(now)
        if (
            state.status != "running" or state.stream_version != expected_turn_version
            or state.active_attempt_id != attempt_id or attempt_state.status != "running"
            or attempt_state.turn_id != turn_id or attempt_state.worker_id != worker_id
            or attempt_state.lease_version != lease_version
            or attempt_state.lease_expires_at is None or attempt_state.lease_expires_at <= now_ms
        ):
            _LOGGER.warning(
                "Event Turn completion lost lease/version: turn=%s state=(%s,v%s,attempt=%s) "
                "expected=(v%s,attempt=%s,worker=%s,lease=%s) attempt_state="
                "(%s,worker=%s,lease=%s,expires=%s)",
                turn_id,
                state.status,
                state.stream_version,
                state.active_attempt_id,
                expected_turn_version,
                attempt_id,
                worker_id,
                lease_version,
                attempt_state.status,
                attempt_state.worker_id,
                attempt_state.lease_version,
                attempt_state.lease_expires_at,
            )
            return False
        attempt = self._attempt_from_event(attempt_state)
        attempt_event_type = "runtime.attempt.completed" if terminal == "completed" else "runtime.attempt.failed"
        attempt_outcome = "succeeded" if terminal == "completed" else "failed"
        try:
            attempt_event = self._append_event_attempt(
                attempt=attempt,
                stream=attempt_stream,
                event_type=attempt_event_type,
                outcome=attempt_outcome,
                context=event_context or self._event_context(
                    attempt_stream, turn_id=turn_id, attempt_id=attempt_id
                ),
            )
            attributes = dict(event_attributes or {})
            if final_message_id:
                attributes["final_message_id"] = final_message_id
            self._append_event_turn(
                stream=stream,
                state=state,
                event_type=f"runtime.turn.{terminal}",
                outcome=terminal,
                context=replace(
                    event_context or self._event_context(stream, turn_id=turn_id, attempt_id=attempt_id),
                    causation_id=attempt_event.event_id,
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                ),
                attributes=attributes,
                producer=event_producer,
                summary=event_summary,
            )
        except StreamVersionConflictError as exc:
            _LOGGER.warning("Event Turn completion append conflict: %s", exc)
            return False
        return True

    def _event_pause(
        self, *, turn_id: str, attempt_id: str, expected_turn_version: int, worker_id: str,
        lease_version: int, waiting_status: str, attributes: dict[str, Any], now: datetime,
    ) -> bool:
        current = self._event_turn(turn_id)
        attempt_current = self._event_attempt(attempt_id)
        if current is None or attempt_current is None:
            return False
        state, stream = current
        attempt_state, attempt_stream = attempt_current
        now_ms = epoch_ms(now)
        if (
            state.status != "running" or state.stream_version != expected_turn_version
            or state.active_attempt_id != attempt_id or attempt_state.status != "running"
            or attempt_state.worker_id != worker_id or attempt_state.lease_version != lease_version
            or attempt_state.lease_expires_at is None or attempt_state.lease_expires_at <= now_ms
        ):
            return False
        try:
            terminal = self._append_event_attempt(
                attempt=self._attempt_from_event(attempt_state),
                stream=attempt_stream,
                event_type="runtime.attempt.completed",
                outcome="succeeded",
                context=self._event_context(attempt_stream, turn_id=turn_id, attempt_id=attempt_id),
            )
            self._append_event_turn(
                stream=stream,
                state=state,
                event_type=f"runtime.turn.{waiting_status}",
                outcome=waiting_status,
                context=replace(
                    self._event_context(stream, turn_id=turn_id, attempt_id=attempt_id),
                    causation_id=terminal.event_id,
                ),
                attributes=attributes,
            )
        except StreamVersionConflictError:
            return False
        return True

    def _event_cancel(self, turn_id: str, expected_version: int) -> bool:
        current = self._event_turn(turn_id)
        if current is None:
            return False
        state, stream = current
        if state.status != "queued" or state.stream_version != expected_version:
            return False
        try:
            self._append_event_turn(
                stream=stream,
                state=state,
                event_type="runtime.turn.cancelled",
                outcome="cancelled",
                context=self._event_context(stream, turn_id=turn_id),
            )
        except StreamVersionConflictError:
            return False
        return True

    def _event_heartbeat(
        self, *, turn_id: str, attempt_id: str, worker_id: str, lease_version: int,
        now: datetime,
    ) -> bool:
        attempt_current = self._event_attempt(attempt_id)
        if attempt_current is None:
            return False
        state, stream = attempt_current
        now_ms = epoch_ms(now)
        if (
            state.turn_id != turn_id or state.status != "running" or state.worker_id != worker_id
            or state.lease_version != lease_version or state.lease_expires_at is None
            or state.lease_expires_at <= now_ms
        ):
            return False
        new_expires_at = now_ms + self._lease_ttl_s * 1000
        try:
            self._append_event_attempt(
                attempt=self._attempt_from_event(state),
                stream=stream,
                event_type="runtime.attempt.lease_renewed",
                outcome="running",
                context=self._event_context(stream, turn_id=turn_id, attempt_id=attempt_id),
                attributes={
                    "worker_id": worker_id,
                    "lease_version": lease_version,
                    "lease_expires_at": new_expires_at,
                },
            )
        except StreamVersionConflictError:
            return False
        return True

    def claim_next(self, worker_id: str, clock: datetime | None = None) -> ClaimedRun | None:
        """领取 queued Turn，创建带有效 Lease 的 RunAttempt。

        同一事务：
        1. 验证 Turn=queued
        2. 验证 Lane 可用
        3. 创建 RunAttempt（含 Lease = now + TTL）
        4. 推进 Turn/Attempt 到 running
        """
        now = self._now(clock)

        event_turns = self._event_turns()
        running_sessions = {state.session_id for state, _ in event_turns if state.status == "running"}
        candidates = [
            (state, stream)
            for state, stream in event_turns
            if state.status == "queued" and state.session_id not in running_sessions
        ]
        candidates.sort(
            key=lambda item: (
                -((item[0].priority or 80) + (20 if epoch_ms(now) - (item[0].created_at or epoch_ms(now)) > 300_000 else 0)),
                item[0].created_at or 0,
                item[0].turn_id,
            )
        )
        for state, stream in candidates:
            with UnitOfWork(self._conn) as uow:
                claimed = self._event_claim(state, stream, worker_id, now)
                if claimed is not None:
                    uow.commit()
                    return claimed
        return None

    def complete(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        worker_id: str = "",
        lease_version: int = 0,
        *,
        final_message_id: str | None = None,
        event_context: EventContext | None = None,
        event_producer: str = "dispatcher",
        event_summary: str = "Turn completed",
        event_attributes: dict[str, Any] | None = None,
        clock: datetime | None = None,
        _uow: UnitOfWork | None = None,
    ) -> bool:
        """完成 RunAttempt。全量校验 Lease 有效性。

        ALL 条件必须匹配：
        - Turn.status = running
        - Turn.version = expected_turn_version
        - Turn.active_attempt_id = attempt_id
        - RunAttempt.status = running
        - RunAttempt.worker_id = worker_id
        - RunAttempt.lease_version = lease_version
        - RunAttempt.lease_expires_at > now
        """
        def complete_from_events() -> bool:
            return self._event_complete_or_fail(
                turn_id=turn_id,
                attempt_id=attempt_id,
                expected_turn_version=expected_turn_version,
                worker_id=worker_id,
                lease_version=lease_version,
                terminal="completed",
                final_message_id=final_message_id,
                event_context=event_context,
                event_producer=event_producer,
                event_summary=event_summary,
                event_attributes=event_attributes,
                now=self._now(clock),
            )

        if _uow is not None:
            return complete_from_events()
        with UnitOfWork(self._conn) as uow:
            result = complete_from_events()
            if result:
                uow.commit()
            return result

    def fail(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        worker_id: str = "",
        lease_version: int = 0,
        clock: datetime | None = None,
    ) -> bool:
        """标记 RunAttempt 失败。全量条件验证同 complete。"""
        with UnitOfWork(self._conn) as uow:
            result = self._event_complete_or_fail(
                turn_id=turn_id,
                attempt_id=attempt_id,
                expected_turn_version=expected_turn_version,
                worker_id=worker_id,
                lease_version=lease_version,
                terminal="failed",
                now=self._now(clock),
            )
            if result:
                uow.commit()
            return result

    def cancel(self, turn_id: str, expected_version: int) -> bool:
        """取消 queued 状态的 Turn。"""
        with UnitOfWork(self._conn) as uow:
            result = self._event_cancel(turn_id, expected_version)
            if result:
                uow.commit()
            return result

    def pause_for_approval(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        worker_id: str,
        lease_version: int,
        approval_id: str,
        *,
        clock: datetime | None = None,
    ) -> bool:
        """End a running Attempt and record the approval wait as an Event fact."""
        with UnitOfWork(self._conn) as uow:
            result = self._event_pause(
                turn_id=turn_id, attempt_id=attempt_id,
                expected_turn_version=expected_turn_version, worker_id=worker_id,
                lease_version=lease_version, waiting_status="waiting_user",
                attributes={"approval_id": approval_id}, now=self._now(clock),
            )
            if result:
                uow.commit()
            return result

    def pause_for_external(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        worker_id: str,
        lease_version: int,
        waiting_id: str,
        *,
        clock: datetime | None = None,
    ) -> bool:
        """End a running Attempt and record an external-work wait/requeue transition."""
        with UnitOfWork(self._conn) as uow:
            result = self._event_pause(
                turn_id=turn_id, attempt_id=attempt_id,
                expected_turn_version=expected_turn_version, worker_id=worker_id,
                lease_version=lease_version, waiting_status="waiting_external",
                attributes={"waiting_id": waiting_id}, now=self._now(clock),
            )
            if result:
                uow.commit()
            return result

    def resume(
        self,
        turn_id: str,
        worker_id: str,
        checkpoint_ref: str = "",
        clock: datetime | None = None,
    ) -> ClaimedRun | None:
        """从恢复点新建一个 RunAttempt（Plan 02 M2 恢复路径）。

        严格创建新 Attempt（不复活旧 Attempt），Lease/Version 重算。
        前置条件：Turn 处于可恢复状态（waiting/failed/expired）且无活跃 Attempt。
        """
        now = self._now(clock)
        event_turn = self._event_turn(turn_id)
        if event_turn is not None:
            state, stream = event_turn
            with UnitOfWork(self._conn) as uow:
                resumed = self._event_claim(
                    state, stream, worker_id, now, checkpoint_ref=checkpoint_ref
                )
                if resumed is not None:
                    uow.commit()
                return resumed
        return None

    def heartbeat(
        self,
        turn_id: str,
        attempt_id: str,
        worker_id: str,
        lease_version: int,
        clock: datetime | None = None,
    ) -> bool:
        """延长当前有效 Lease。

        仅当 Lease 尚未过期时续期。已过期 Lease 返回 False。
        验证：worker_id + lease_version + lease_expires_at > now。
        """
        now = self._now(clock)
        with UnitOfWork(self._conn) as uow:
            result = self._event_heartbeat(
                turn_id=turn_id, attempt_id=attempt_id, worker_id=worker_id,
                lease_version=lease_version, now=now,
            )
            if result:
                uow.commit()
            return result
