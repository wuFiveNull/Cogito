"""QueryService —— 只读查询薄服务层。

ACCESS-DELIVERY §2.2 Query API 的数据来源。
所有只读视图在这里封装，供 FastAPI handler 调用。
handler 绝不直接操作数据库；所有数据访问都经由此服务与现有 repo/service。

内存检索统一走 RetrievalService；任务/轮次/对话等走对应 Repository；
模型调用量走 ModelCallRepository.usage_summary。
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from typing import Any

from cogito.config import Config
from cogito.contracts.event_query import EventPayloadUnavailableError
from cogito.contracts.clock import epoch_ms
from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.delivery_effect_payload import load_delivery_effect_payload
from cogito.service.retrieval_service import RetrievalService
from cogito.store.capability_repo import CapabilityRepository
from cogito.store.config_version_repo import ConfigVersionRepository
from cogito.store.connector_repo import ConnectorCursorRepository, ConnectorRepository
from cogito.store.digest_repo import DigestRepository
from cogito.store.drift_repo import DriftRunRepository, DriftSkillStateRepository
from cogito.store.event_projection_store import EventProjectionStore
from cogito.store.event_message_reader import EventMessageReader
from cogito.store.event_replay import replay_approval, replay_delivery
from cogito.store.event_store import EventPage, EventStore
from cogito.store.model_call_repo import ModelCallRepository
from cogito.store.proactive_repo import (
    ProactiveCandidateRepository,
    ProactiveDecisionRepository,
    ProactivePolicyRepository,
)
from cogito.store.receipt_repo import SideEffectReceiptRepository
from cogito.store.repositories import TurnRepository
from cogito.store.schedule_repo import ScheduleRepository
from cogito.store.task_repo import TaskAttemptRepository, TaskRepository
from cogito.store.tool_call_repo import ToolCallRepository
from cogito.store.event_store_cutover import is_cutover_database


class QueryService:
    """只读查询服务——handler 的唯一数据入口。"""

    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self._conn = conn
        self._config = config
        self._event_only = is_cutover_database(conn)
        self._task_repo = TaskRepository(conn)
        self._attempt_repo = TaskAttemptRepository(conn)
        self._turn_repo = TurnRepository(conn)
        self._connector_repo = ConnectorRepository(conn)
        self._model_call_repo = ModelCallRepository(conn)
        self._retrieval = RetrievalService(conn)
        # ── Plan 08 Dashboard: 新增 repo ──
        self._candidate_repo = ProactiveCandidateRepository(conn)
        self._decision_repo = ProactiveDecisionRepository(conn)
        self._policy_repo = ProactivePolicyRepository(conn)
        self._tool_call_repo = ToolCallRepository(conn)
        self._receipt_repo = SideEffectReceiptRepository(conn)
        self._capability_repo = CapabilityRepository(conn)
        self._config_version_repo = ConfigVersionRepository(conn)
        self._digest_repo = DigestRepository(conn)
        self._schedule_repo = ScheduleRepository(conn)
        self._event_store = EventStore(conn)
        self._event_projections = EventProjectionStore(self._event_store)
        self._event_messages = EventMessageReader(
            conn,
            PayloadStore(config.resolve_payload_dir(), conn),
        )

    def _identity_count(
        self, stream_type: str, legacy_sql: str, *, active_only: bool = False
    ) -> int:
        projection_lookup = {
            "conversation": self._event_projections.conversations,
            "session": self._event_projections.sessions,
            "endpoint": self._event_projections.endpoints,
        }
        projections = projection_lookup[stream_type](**({"active_only": True} if active_only else {}))
        if projections:
            return len(projections)
        return int(self._conn.execute(legacy_sql).fetchone()[0])

    @staticmethod
    def _message_text(message: dict[str, Any] | None) -> str:
        if not message:
            return ""
        return "\n".join(
            str(part.get("inline_data", ""))
            for part in message.get("content_parts", [])
            if isinstance(part, dict)
            and part.get("content_type") in {"text", "markdown"}
            and part.get("inline_data")
        )

    # ── status / usage ─────────────────────────────────────────

    def status(self, recovery_counts: dict[str, int] | None = None) -> dict[str, Any]:
        """系统运行状态快照。"""
        cfg = self._config
        ep = self._event_projections
        turn_total = len(ep.turns())
        task_total = len(ep.tasks())
        memory_total = len(ep.memories())
        return {
            "profile": cfg.runtime.profile,
            "model_configured": cfg.model.main.is_configured(),
            "model": cfg.model.main.model or "(stub)",
            "db_path": cfg.resolve_db_path(),
            "counts": {
                "turns": turn_total,
                "tasks": task_total,
                "conversations": len(ep.conversations()),
                "sessions": len(ep.sessions(active_only=True)),
                "endpoints": len(ep.endpoints()),
                "memory_items": memory_total,
                "connectors": len(self._connector_repo.find_active())
            },
            "recovery": recovery_counts or {},
            "worker": {
                "concurrency": cfg.worker.concurrency,
                "heartbeat_interval_seconds": cfg.worker.heartbeat_interval_seconds,
            },
        }

    def usage(self, hours: int = 24) -> dict[str, Any]:
        """最近 hours 小时模型调用量统计。"""
        summary = self._model_call_repo.usage_summary()  # 全量基线
        # 时间段统计
        from datetime import UTC, datetime, timedelta

        since = epoch_ms(datetime.now(UTC) - timedelta(hours=hours))
        windowed = self._model_call_repo.usage_summary(since_ms=since)
        return {
            "window_hours": hours,
            "windowed": windowed,
            "total": summary,
            "recent_errors": self._model_call_repo.failure_count(since),
        }

    # ── turns ──────────────────────────────────────────────────

    def list_turns(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        replayed = self._event_projections.turns(status=status)
        if self._event_only:
            return {
                "items": replayed[offset : offset + limit],
                "total": len(replayed),
                "limit": limit,
                "offset": offset,
            }
        rows = self._turn_repo.list_(status=status, limit=limit, offset=offset)
        return {
            "items": [item.to_dict() for item in rows],
            "total": self._turn_repo.count(status=status),
            "limit": limit,
            "offset": offset,
        }

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        if not self._event_only:
            turn = self._turn_repo.get(turn_id)
            if turn is None:
                return None
            return {"turn": turn.to_dict(), "attempts": [item.to_dict() for item in self._turn_repo.list_attempts(turn_id)]}
        turn = next(
            (item for item in self._event_projections.turns() if item["turn_id"] == turn_id),
            None,
        )
        if turn is None:
            return None
        attempts = self._event_projections.attempts(turn_id=turn_id)
        return {"turn": turn, "attempts": attempts}

    # ── tasks ──────────────────────────────────────────────────

    def list_tasks(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        replayed = self._event_projections.tasks(status=status)
        return {
            "items": replayed[offset : offset + limit],
            "total": len(replayed),
            "limit": limit,
            "offset": offset,
        }

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self._task_repo.get(task_id)
        attempt_repo = self._attempt_repo
        if task is None:
            return None
        attempts = attempt_repo.list_for_task(task_id)
        return {"task": task.to_dict(), "attempts": [a.to_dict() for a in attempts]}

    def list_tasks_for_principal(
        self,
        principal_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return only Agent tasks whose canonical payload binds the Principal.

        Legacy/system tasks without a structured ``principal_id`` are deliberately
        invisible to the read-only MCP facade.
        """
        matched = [
            task
            for task in self._task_repo.list_filtered(status=status, limit=10_000)
            if self._task_principal_id(task.payload_ref) == principal_id
        ]
        return {
            "items": [
                _public_task(task.to_dict())
                for task in matched[offset : offset + max(1, min(limit, 100))]
            ],
            "total": len(matched),
        }

    def get_task_for_principal(
        self,
        task_id: str,
        principal_id: str,
    ) -> dict[str, Any] | None:
        task = self._task_repo.get(task_id)
        attempt_repo = self._attempt_repo
        if task is None or self._task_principal_id(task.payload_ref) != principal_id:
            return None
        attempts = attempt_repo.list_for_task(task_id)
        return {
            "task": _public_task(task.to_dict()),
            "attempts": [_public_task_attempt(item.to_dict()) for item in attempts],
        }

    # ── memory ─────────────────────────────────────────────────

    def search_memory(
        self,
        q: str = "",
        limit: int = 50,
        principal_id: str = "owner",
        status: str = "confirmed",
    ) -> dict[str, Any]:
        """记忆检索（PLAN-16 M7 OPS-02）。

        status 过滤：confirmed（默认）/ candidate（待确认候选）/ all（全部未删）。
        """
        statuses = None
        if status == "confirmed":
            statuses = ("confirmed",)
        elif status == "candidate":
            statuses = ("candidate",)
        # else "all": 不过滤 status
        if q:
            scored = self._retrieval.retrieve(
                principal_id=principal_id,
                query=q,
                limit=limit,
            )
            items = [sm.to_dict() for sm in scored]
        else:
            # Use Event projections to list memories
            all_mems = self._event_projections.memories()
            filtered = [m for m in all_mems if m.get("principal_id") == principal_id]
            if statuses:
                filtered = [m for m in filtered if m.get("status") in statuses]
            filtered.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
            items = filtered[:limit]
        return {"items": items, "query": q, "count": len(items), "status": status}

    def search_memory_page(self, q: str, *, principal_id: str, limit: int, offset: int) -> dict[str, Any]:
        """Paged memory search using Event replay + FTS projection."""
        # Use Event projections for count, then FTS-like text filter
        all_mems = self._event_projections.memories()
        filtered = [
            m for m in all_mems
            if m.get("principal_id") == principal_id
            and m.get("status") == "confirmed"
        ]
        if q:
            ql = q.lower()
            filtered = [m for m in filtered if ql in (m.get("memory_id") or "").lower()]
        total = len(filtered)
        page = filtered[offset:offset + limit]
        return {"items": page, "total": total, "limit": limit, "offset": offset}

    # ── connectors ─────────────────────────────────────────────

    def list_connectors(self) -> dict[str, Any]:
        rows = self._connector_repo.find_active(limit=100)
        return {"items": [c.to_dict() for c in rows]}

    # ── channels / conversations / endpoints ───────────────────

    def list_channels(self) -> dict[str, Any]:
        """端点按渠道类型聚合。"""
        endpoints = self._event_projections.endpoints()
        if endpoints:
            counts: dict[str, int] = {}
            for endpoint in endpoints:
                if endpoint["status"] == "active":
                    channel_type = endpoint["channel_type"]
                    counts[channel_type] = counts.get(channel_type, 0) + 1
            return {
                "items": [
                    {"channel_type": channel_type, "count": count}
                    for channel_type, count in sorted(
                        counts.items(), key=lambda item: (-item[1], item[0])
                    )
                ]
            }
        rows = self._conn.execute(
            "SELECT channel_type, COUNT(*) AS n FROM endpoints "
            "WHERE status='active' GROUP BY channel_type ORDER BY n DESC"
        ).fetchall()
        return {"items": [{"channel_type": r["channel_type"], "count": r["n"]} for r in rows]}

    def list_conversations(self, limit: int = 100) -> dict[str, Any]:
        """列出会话，过滤掉所有 session 均已软删除的 conversation。

        规则：若 conversation 下存在至少一个未删除的 session，则保留；
        若 conversation 下无任何活跃 session（全部已删除或原本无 session），则排除。
        """
        conversations = self._event_projections.conversations()
        if conversations:
            active_conversation_ids = {
                session["conversation_id"]
                for session in self._event_projections.sessions(active_only=True)
            }
            return {
                "items": [
                    conversation
                    for conversation in reversed(conversations)
                    if conversation["conversation_id"] in active_conversation_ids
                ][:limit]
            }
        rows = self._conn.execute(
            "SELECT c.* FROM conversations c "
            "WHERE EXISTS ("
            "  SELECT 1 FROM sessions s "
            "  WHERE s.conversation_id = c.conversation_id AND s.deleted_at IS NULL"
            ") "
            "ORDER BY c.conversation_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"items": [dict(r) for r in rows]}

    def get_conversation_messages(
        self,
        conversation_id: str,
        limit: int = 200,
    ) -> dict[str, Any]:
        """按会话取消息（含文本），用于聊天历史回放。

        一条消息的文本由 content_parts.inline_data 拼接；按 receive_sequence 升序。

        同时支持内部 conversation_id (UUID) 和 platform_conversation_id
        (如 web:xxxxx)。前端 localStorage 存的是 platform_conversation_id，
        若直接按 messages.conversation_id 匹配会查不到（那是内部 UUID），
        导致刷新后历史消息为空。
        """
        # 新运行时优先由 Conversation/Message Event 回放。
        conversations = self._event_projections.conversations()
        if conversations:
            conversation = next(
                (
                    item
                    for item in conversations
                    if item["conversation_id"] == conversation_id
                    or item["platform_conversation_id"] == conversation_id
                ),
                None,
            )
            if conversation is None:
                return {"conversation_id": conversation_id, "items": []}
            items = []
            for message in self._event_messages.list_for_conversation(
                conversation["conversation_id"]
            )[:limit]:
                items.append(
                    {
                        "message_id": message["message_id"],
                        "role": message.get("role", ""),
                        "created_at": message.get("created_at", ""),
                        "receive_sequence": message.get("receive_sequence", 0),
                        "text": self._message_text(message),
                    }
                )
            return {"conversation_id": conversation_id, "items": items}

        # Compatibility path for pre-backfill tables.
        internal_id = self._conn.execute(
            "SELECT conversation_id FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        resolved = internal_id["conversation_id"] if internal_id else None
        if resolved is None:
            row = self._conn.execute(
                "SELECT conversation_id FROM conversations WHERE platform_conversation_id=?",
                (conversation_id,),
            ).fetchone()
            resolved = row["conversation_id"] if row else None
        if resolved is None:
            return {"conversation_id": conversation_id, "items": []}

        rows = self._conn.execute(
            "SELECT m.message_id, m.role, m.created_at, m.receive_sequence, "
            "       cp.inline_data AS text "
            "FROM messages m "
            "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
            "WHERE m.conversation_id = ? "
            "ORDER BY m.receive_sequence ASC LIMIT ?",
            (resolved, limit),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # inline_data 可能为 None（纯元数据消息）
            d["text"] = d.get("text") or ""
            items.append(d)
        return {"conversation_id": conversation_id, "items": items}

    # ── deliveries ─────────────────────────────────────────────

    def list_deliveries(
        self,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        replayed = self._event_projections.deliveries(status=status)
        return {
            "items": replayed[offset : offset + limit],
            "total": len(replayed),
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _task_principal_id(payload_ref: str | None) -> str:
        if not payload_ref:
            return ""
        try:
            value = json.loads(payload_ref)
        except (TypeError, json.JSONDecodeError):
            return ""
        return str(value.get("principal_id", "")) if isinstance(value, dict) else ""

    # ── traces ─────────────────────────────────────────────────

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        trace = self._event_store.trace(trace_id)
        if trace is None:
            return None
        trace["events"] = [_public_event(event) for event in trace["events"]]
        return trace

    def event_timeline(self, session_id: str, limit: int = 500) -> dict[str, Any]:
        """Return a session's canonical Event timeline in causal order."""
        events = self._event_store.list_events(session_id=session_id, limit=limit)
        return {
            "session_id": session_id,
            "events": [_public_event(event.to_dict()) for event in reversed(events)],
        }

    def list_canonical_events(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        before: int | None = None,
        event_type: str | None = None,
        stream_type: str | None = None,
        stream_id: str | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        attempt_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        page: EventPage = self._event_store.list_events_page(
            limit=limit,
            cursor=cursor,
            before=before,
            event_type=event_type,
            stream_type=stream_type,
            stream_id=stream_id,
            trace_id=trace_id,
            session_id=session_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            attempt_id=attempt_id,
            task_id=task_id,
        )
        return {
            "items": [_public_event(event.to_dict()) for event in page.events],
            "next_cursor": page.next_cursor,
        }

    def event_payload_metadata(self, event_id: str) -> dict[str, Any] | None:
        """Return only a guarded payload reference, never raw payload bytes."""
        event = self._event_store.get(event_id)
        if event is None:
            return None
        return {
            "event_id": event_id,
            "payload_ref": event.payload_ref,
            "payload_hash": event.payload_hash,
        }

    def read_event_payload(self, event_id: str) -> bytes | None:
        """Resolve guarded Event payload bytes without exposing them in the Event log.

        The HTTP adapter is responsible for authenticating the caller before it
        calls this method.  Missing and expired payloads remain observable from
        the immutable Event but are not silently treated as an empty payload.
        """
        event = self._event_store.get(event_id)
        if event is None:
            return None
        if not event.payload_ref:
            raise EventPayloadUnavailableError("event has no payload reference")
        data = PayloadStore(self._config.resolve_payload_dir(), self._conn).get(event.payload_ref)
        if data is None:
            raise EventPayloadUnavailableError("event payload is unavailable or expired")
        if event.payload_hash and hashlib.sha256(data).hexdigest() != event.payload_hash:
            raise EventPayloadUnavailableError("event payload hash does not match")
        return data

    # ── sessions ───────────────────────────────────────────────

    def list_sessions(self, limit: int = 100) -> dict[str, Any]:
        """列出会话，附带 turn 数、最近活跃时间、conversation_id。

        过滤掉已软删除（deleted_at IS NOT NULL）的会话。
        每个 session 用其下最新一条用户提问（user role 消息文本）作为 name，
        便于在列表中直接识别会话内容。
        """
        sessions = self._event_projections.sessions(active_only=True)
        if sessions:
            turns = self._event_projections.turns()
            turns_by_session: dict[str, list[dict[str, Any]]] = {}
            for turn in turns:
                turns_by_session.setdefault(turn["session_id"], []).append(turn)
            items: list[dict[str, Any]] = []
            for session in sessions:
                session_turns = turns_by_session.get(session["session_id"], [])
                messages = self._event_messages.list_for_session(session["session_id"])
                user_messages = [message for message in messages if message.get("role") == "user"]
                latest_user = user_messages[-1] if user_messages else None
                text = self._message_text(latest_user) if latest_user is not None else ""
                first_line = text.split("\n")[0].strip() if text else ""
                last_turn_at = max(
                    (turn.get("created_at") or 0 for turn in session_turns), default=0
                )
                items.append(
                    {
                        **session,
                        "turn_count": len(session_turns),
                        "last_turn_at": last_turn_at or None,
                        "name": first_line[:42] if first_line else session["session_id"][:12],
                        "latest_user_at": latest_user.get("created_at") if latest_user else None,
                    }
                )
            items.sort(
                key=lambda item: (item.get("last_turn_at") or item.get("created_at") or 0),
                reverse=True,
            )
            return {"items": items[:limit], "total": len(items)}

        # Compatibility path for pre-backfill tables.
        rows = self._conn.execute(
            "SELECT s.session_id, s.conversation_id, s.status, s.created_at, "
            "       COUNT(t.turn_id) AS turn_count, "
            "       MAX(t.created_at) AS last_turn_at "
            "FROM sessions s "
            "LEFT JOIN turns t ON t.session_id = s.session_id "
            "WHERE s.deleted_at IS NULL "
            "GROUP BY s.session_id "
            "ORDER BY COALESCE(MAX(t.created_at), s.created_at) DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["turn_count"] = int(d["turn_count"])
            # 取该 session 下最新一条用户提问作为会话名称
            name_row = self._conn.execute(
                "SELECT cp.inline_data AS text "
                "FROM messages m "
                "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
                "WHERE m.session_id = ? AND m.role = 'user' "
                "ORDER BY m.receive_sequence DESC LIMIT 1",
                (d["session_id"],),
            ).fetchone()
            raw_text = (name_row["text"] if name_row else None) or ""
            # inline_data 可能含多段，取第一段非空文本
            first_line = raw_text.split("\n")[0].strip() if raw_text else ""
            d["name"] = first_line[:42] if first_line else d["session_id"][:12]
            d["latest_user_at"] = None
            if name_row:
                ts_row = self._conn.execute(
                    "SELECT created_at FROM messages "
                    "WHERE session_id=? AND role='user' "
                    "ORDER BY receive_sequence DESC LIMIT 1",
                    (d["session_id"],),
                ).fetchone()
                d["latest_user_at"] = ts_row["created_at"] if ts_row else None
            items.append(d)
        return {"items": items, "total": len(items)}

    def get_session_trace(self, session_id: str) -> dict[str, Any] | None:
        """聚合一个 session 的完整运行 trace。

        包含：
        - session 基本信息
        - messages：该 session 下的完整会话消息（user/assistant/tool），按 receive_sequence，
          附带每条消息的耗时（距上一条消息的时间差，单位 ms）
        - turns：session 下每条 turn 的执行链路（RunAttempt → ModelCall），并标注 turn 整体耗时

        trace_id == turn_id（runtime/loop.py），因此一个 session 的 trace 即其全部 turn 的
        RunAttempt 与 ModelCall 的时间线聚合。

        已软删除的会话返回 None（页面不可访问）。
        """
        event_session = next(
            (
                session
                for session in self._event_projections.sessions()
                if session["session_id"] == session_id
            ),
            None,
        )
        if event_session is not None:
            if event_session["status"] != "active":
                return None
            return self._event_session_trace(event_session)

        # Compatibility path for pre-backfill tables.
        session = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=? AND deleted_at IS NULL", (session_id,)
        ).fetchone()
        if session is None:
            return None
        session_dict = dict(session)

        # ── 消息序列（含耗时） ─────────────────────────────────
        msg_rows = self._conn.execute(
            "SELECT m.message_id, m.role, m.created_at, m.receive_sequence, "
            "       cp.inline_data AS text "
            "FROM messages m "
            "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
            "WHERE m.session_id = ? "
            "ORDER BY m.receive_sequence ASC",
            (session_id,),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        prev_ts_ms: int | None = None
        for r in msg_rows:
            d = dict(r)
            text = d.get("text") or ""
            # inline_data 可能含多段（换行分隔）; 取拼接后的首行预览
            first_line = text.split("\n")[0].strip() if text else ""
            d["text"] = text
            d["preview"] = first_line[:80]
            # 计算距上一条消息的耗时（ms）
            cur_ts_ms = self._parse_ts_ms(d.get("created_at"))
            if cur_ts_ms is not None:
                d["since_prev_ms"] = (cur_ts_ms - prev_ts_ms) if prev_ts_ms is not None else 0
                prev_ts_ms = cur_ts_ms
            else:
                d["since_prev_ms"] = None
            messages.append(d)

        # ── turn 执行链路 ───────────────────────────────────────
        turns = self._turn_repo.list_by_session(session_id)
        turns_out: list[dict[str, Any]] = []
        total_model_calls = 0
        total_input_tokens = 0
        total_output_tokens = 0
        for turn in turns:
            td = turn.to_dict()
            turn_start_ms = self._parse_ts_ms(td.get("created_at"))
            attempts = self._turn_repo.list_attempts(turn.turn_id)
            attempts_out: list[dict[str, Any]] = []
            turn_end_ms = turn_start_ms
            for a in attempts:
                ad = a.to_dict()
                a_start_ms = self._parse_ts_ms(ad.get("started_at"))
                a_end_ms = self._parse_ts_ms(ad.get("finished_at"))
                ad["duration_ms"] = (
                    (a_end_ms - a_start_ms)
                    if a_start_ms is not None and a_end_ms is not None
                    else None
                )
                model_calls = [
                    mc.to_dict() for mc in self._model_call_repo.find_by_attempt(a.attempt_id)
                ]
                ad["model_calls"] = model_calls
                total_model_calls += len(model_calls)
                for mc in model_calls:
                    total_input_tokens += mc.get("input_tokens", 0) or 0
                    total_output_tokens += mc.get("output_tokens", 0) or 0
                    mc_end = mc.get("completed_at")
                    if isinstance(mc_end, int) and (turn_end_ms is None or mc_end > turn_end_ms):
                        turn_end_ms = mc_end
                attempts_out.append(ad)
            td["attempts"] = attempts_out
            # 整条 turn 的耗时（从创建到最后一次模型调用完成）
            td["duration_ms"] = (
                (turn_end_ms - turn_start_ms)
                if turn_start_ms is not None
                and turn_end_ms is not None
                and turn_end_ms >= turn_start_ms
                else None
            )
            turns_out.append(td)
        return {
            "session": session_dict,
            "messages": messages,
            "turns": turns_out,
            "summary": {
                "turn_count": len(turns_out),
                "model_call_count": total_model_calls,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "message_count": len(messages),
            },
        }

    def _event_session_trace(self, session: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        previous_at: int | None = None
        for message in self._event_messages.list_for_session(session["session_id"]):
            text = self._message_text(message)
            created_at = message.get("created_at", "")
            current_at = self._parse_ts_ms(created_at)
            messages.append(
                {
                    "message_id": message["message_id"],
                    "role": message.get("role", ""),
                    "created_at": created_at,
                    "receive_sequence": message.get("receive_sequence", 0),
                    "text": text,
                    "preview": text.split("\n")[0].strip()[:80] if text else "",
                    "since_prev_ms": (
                        current_at - previous_at
                        if current_at is not None and previous_at is not None
                        else (0 if current_at is not None else None)
                    ),
                }
            )
            if current_at is not None:
                previous_at = current_at

        turns_out: list[dict[str, Any]] = []
        total_model_calls = total_input_tokens = total_output_tokens = 0
        for turn in self._event_projections.turns():
            if turn["session_id"] != session["session_id"]:
                continue
            turn_data = dict(turn)
            attempts_out: list[dict[str, Any]] = []
            for attempt in self._event_projections.attempts(turn_id=turn["turn_id"]):
                attempt_data = dict(attempt)
                started_at = attempt_data.get("started_at")
                finished_at = attempt_data.get("finished_at")
                attempt_data["duration_ms"] = (
                    finished_at - started_at
                    if isinstance(started_at, int) and isinstance(finished_at, int)
                    else None
                )
                model_calls = [
                    model_call.to_dict()
                    for model_call in self._model_call_repo.find_by_attempt(attempt["attempt_id"])
                ]
                attempt_data["model_calls"] = model_calls
                total_model_calls += len(model_calls)
                total_input_tokens += sum(call.get("input_tokens", 0) or 0 for call in model_calls)
                total_output_tokens += sum(call.get("output_tokens", 0) or 0 for call in model_calls)
                attempts_out.append(attempt_data)
            turn_data["attempts"] = attempts_out
            turns_out.append(turn_data)
        return {
            "session": session,
            "messages": messages,
            "turns": turns_out,
            "summary": {
                "turn_count": len(turns_out),
                "model_call_count": total_model_calls,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "message_count": len(messages),
            },
        }

    def _event_conversation_trace(self, conversation: dict[str, Any]) -> dict[str, Any]:
        sessions = [
            session
            for session in self._event_projections.sessions(
                conversation_id=conversation["conversation_id"], active_only=True
            )
        ]
        active_session_ids = {session["session_id"] for session in sessions}
        messages = []
        for message in self._event_messages.list_for_conversation(conversation["conversation_id"]):
            text = self._message_text(message)
            messages.append(
                {
                    "message_id": message["message_id"],
                    "role": message.get("role", ""),
                    "direction": message.get("direction", ""),
                    "created_at": message.get("created_at", ""),
                    "receive_sequence": message.get("receive_sequence", 0),
                    "reply_to_message_id": message.get("reply_to_message_id", ""),
                    "platform_message_id": message.get("platform_message_id", ""),
                    "session_id": message.get("session_id", ""),
                    "text_len": len(text),
                    "text_preview": text[:120],
                }
            )
        turns_out = []
        for turn in self._event_projections.turns():
            if turn["session_id"] not in active_session_ids:
                continue
            turn_data = dict(turn)
            attempts = self._event_projections.attempts(turn_id=turn["turn_id"])
            for attempt in attempts:
                attempt["model_calls"] = [
                    model_call.to_dict()
                    for model_call in self._model_call_repo.find_by_attempt(attempt["attempt_id"])
                ]
                attempt["model_call_count"] = len(attempt["model_calls"])
                attempt["error_calls"] = sum(
                    call.get("status") == "error" for call in attempt["model_calls"]
                )
            turn_data["attempts"] = attempts
            turn_data["attempt_count"] = len(attempts)
            turn_data["deliveries"] = [
                delivery
                for delivery in self._event_projections.deliveries()
                if delivery["turn_id"] == turn["turn_id"]
            ]
            turn_data["delivery_count"] = len(turn_data["deliveries"])
            turns_out.append(turn_data)
        return {
            "conversation": conversation,
            "sessions": sessions,
            "messages": messages,
            "turns": turns_out,
            "diagnosis": self._diagnose_chain(messages, turns_out),
        }

    @staticmethod
    def _parse_ts_ms(value: object) -> int | None:
        """把 ISO 字符串或整数毫秒时间解析为毫秒 epoch；解析失败返回 None。"""
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            # 整数毫秒字符串
            if value.isdigit():
                return int(value)
            # ISO 格式
            from datetime import datetime

            try:
                s = value.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                return int(dt.timestamp() * 1000)
            except ValueError:
                return None
        return None

    # ── debug trace ────────────────────────────────────────────

    def trace_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        """全链路追踪：给定 conversation_id，返回从入站到投递完成的完整链路。

        用于定位"用户发消息后无响应"类问题。每条记录含时间戳与耗时，
        哪一环卡住/失败一目了然。
        """
        event_conversation = next(
            (
                conversation
                for conversation in self._event_projections.conversations()
                if conversation["conversation_id"] == conversation_id
                or conversation["platform_conversation_id"] == conversation_id
            ),
            None,
        )
        if event_conversation is not None:
            return self._event_conversation_trace(event_conversation)

        # Compatibility path for pre-backfill tables.
        conv = self._conn.execute(
            "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if conv is None:
            # 也尝试按 platform_conversation_id 查找（web channel 的订阅键）
            conv = self._conn.execute(
                "SELECT * FROM conversations WHERE platform_conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if conv is None:
            return None

        conv_dict = dict(conv)
        real_id = conv_dict["conversation_id"]

        # ── 会话（过滤已软删除） ──
        sessions = [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM sessions WHERE conversation_id=? "
                "AND deleted_at IS NULL ORDER BY created_at ASC",
                (real_id,),
            ).fetchall()
        ]

        # ── 消息（含文本） ──
        msg_rows = self._conn.execute(
            "SELECT m.message_id, m.role, m.direction, m.created_at, m.receive_sequence, "
            "       m.reply_to_message_id, m.platform_message_id, m.session_id, "
            "       cp.inline_data AS text "
            "FROM messages m "
            "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
            "WHERE m.conversation_id=? "
            "ORDER BY m.receive_sequence ASC",
            (real_id,),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        for r in msg_rows:
            d = dict(r)
            txt = d.get("text") or ""
            d["text_len"] = len(txt)
            d["text_preview"] = txt[:120]
            messages.append(d)

        # ── Turns（每条 user 消息对应一个；过滤已软删除 session） ──
        active_session_ids = {s["session_id"] for s in sessions}
        turns = self._event_projections.turns()
        turns_out: list[dict[str, Any]] = []
        for turn in turns:
            if turn["session_id"] not in active_session_ids:
                continue
            td = dict(turn)
            # attempts
            attempts = self._event_projections.attempts(turn_id=turn["turn_id"])
            attempts_out: list[dict[str, Any]] = []
            for a in attempts:
                ad = dict(a)
                # model calls
                model_calls = [
                    mc.to_dict() for mc in self._model_call_repo.find_by_attempt(a["attempt_id"])
                ]
                ad["model_calls"] = model_calls
                ad["model_call_count"] = len(model_calls)
                ad["error_calls"] = sum(1 for mc in model_calls if mc.get("status") == "error")
                attempts_out.append(ad)
            td["attempts"] = attempts_out
            td["attempt_count"] = len(attempts_out)
            # deliveries for this turn
            del_out = [
                delivery
                for delivery in self._event_projections.deliveries()
                if delivery["turn_id"] == turn["turn_id"]
            ]
            td["deliveries"] = del_out
            td["delivery_count"] = len(del_out)
            turns_out.append(td)

        # ── 链路诊断 ──
        diagnosis = self._diagnose_chain(messages, turns_out)

        return {
            "conversation": conv_dict,
            "sessions": sessions,
            "messages": messages,
            "turns": turns_out,
            "diagnosis": diagnosis,
        }

    def _diagnose_chain(
        self,
        messages: list[dict[str, Any]],
        turns: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """逐条诊断每个 user 消息的处理链路，返回可读的问题列表。"""
        issues: list[dict[str, Any]] = []
        user_msgs = [m for m in messages if m["role"] == "user"]

        for i, um in enumerate(user_msgs):
            # 找对应的 turn（按 input_message_id 匹配）
            turn = None
            for t in turns:
                # turns 没有直接存 input_message_id 在这里，按时间最近匹配
                pass
            # 简化：按序号对应（receive_sequence 单调）
            if i < len(turns):
                turn = turns[i]

            msg_diag: dict[str, Any] = {
                "message_id": um["message_id"],
                "preview": um["text_preview"][:60],
                "created_at": um["created_at"],
            }

            if turn is None:
                msg_diag["issue"] = "NO_TURN"
                msg_diag["detail"] = "用户消息未生成 Turn（inbound.accept 可能未调用或失败）"
                issues.append(msg_diag)
                continue

            msg_diag["turn_id"] = turn["turn_id"]
            msg_diag["turn_status"] = turn["status"]

            if turn["status"] == "queued":
                msg_diag["issue"] = "TURN_STUCK_QUEUED"
                msg_diag["detail"] = "Turn 始终停留在 queued，worker 未领取（worker 可能未运行）"
                issues.append(msg_diag)
                continue
            if turn["status"] == "running":
                msg_diag["issue"] = "TURN_STUCK_RUNNING"
                msg_diag["detail"] = "Turn 卡在 running，agent 执行中或 lease 过期"
                issues.append(msg_diag)
                continue
            if turn["status"] == "cancelled":
                msg_diag["issue"] = "TURN_CANCELLED"
                msg_diag["detail"] = "Turn 被外部取消"
                issues.append(msg_diag)
                continue
            if turn["status"] == "failed":
                msg_diag["issue"] = "TURN_FAILED"
                # 找失败原因
                err_calls = []
                for a in turn.get("attempts", []):
                    for mc in a.get("model_calls", []):
                        if mc.get("status") == "error":
                            err_calls.append(mc.get("error_category") or "unknown")
                msg_diag["detail"] = (
                    f"Turn 执行失败，模型调用错误: {err_calls}"
                    if err_calls
                    else "Turn 执行失败（详见 attempt）"
                )
                issues.append(msg_diag)
                continue

            # completed — 检查是否有 assistant 回复
            if turn["status"] == "completed":
                has_reply = any(
                    m["role"] == "assistant" and m.get("created_at", "") >= um.get("created_at", "")
                    for m in messages
                )
                if not has_reply:
                    msg_diag["issue"] = "NO_REPLY"
                    msg_diag["detail"] = "Turn 完成但无 assistant 回复消息"
                    issues.append(msg_diag)
                    continue

                # 检查投递
                if turn.get("delivery_count", 0) == 0:
                    msg_diag["issue"] = "NO_DELIVERY"
                    msg_diag["detail"] = "有回复但未创建 Delivery（非流式路径未推 WS 或投递未触发）"
                    issues.append(msg_diag)
                    continue

                failed_deliveries = [
                    d
                    for d in turn.get("deliveries", [])
                    if d["status"] in ("failed", "cancelled", "interrupted")
                ]
                if failed_deliveries:
                    msg_diag["issue"] = "DELIVERY_FAILED"
                    failure = failed_deliveries[0]
                    msg_diag["detail"] = (
                        f"Delivery 失败: {failure.get('last_error') or failure['status']}"
                    )
                    issues.append(msg_diag)
                    continue

            msg_diag["issue"] = "OK"
            msg_diag["detail"] = "链路正常"
            issues.append(msg_diag)

        return issues

    # ── plugins (durable Plugin Runtime state + MCP config) ───────────

    def list_plugins(self) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT plugin_id, version, status, source, isolation, permissions, "
            "error, fail_count, started_at FROM plugins ORDER BY plugin_id"
        ).fetchall()
        plugins = [
            {
                "name": row["plugin_id"],
                "version": row["version"],
                "status": row["status"],
                "source": row["source"],
                "isolation": row["isolation"],
                "permissions": json.loads(row["permissions"] or "[]"),
                "error": row["error"],
                "fail_count": row["fail_count"],
                "started_at": row["started_at"],
                "kind": "plugin",
                "enabled": row["status"] not in ("disabled", "degraded", "stopped"),
            }
            for row in rows
        ]
        servers = [
            {
                "name": s.name,
                "transport": s.transport,
                "enabled": s.enabled,
                "toolset": s.toolset,
                "kind": "mcp",
            }
            for s in self._config.capability.mcp_servers
        ]
        items = plugins + servers
        return {"items": items, "count": len(items)}

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _now_ms() -> int:
        from datetime import UTC, datetime

        return int(datetime.now(UTC).timestamp() * 1000)

    def _pending_approval_count(self) -> int:
        """Count visible pending approvals by replaying canonical approval streams."""
        streams: dict[str, list[Any]] = {}
        for event in self._event_store.read_stream_type("approval"):
            streams.setdefault(event.stream_id, []).append(event)
        return sum(
            1
            for approval_id, events in streams.items()
            if (state := replay_approval(events, approval_id)) is not None
            and state.status == "pending"
        )

    # ── dashboard summary / attention / health ──────────────────

    def dashboard_summary(self) -> dict[str, Any]:
        """聚合当前一屏所需数据：status + usage + backlog + health + proactive 摘要。"""
        from datetime import UTC, datetime

        status = self.status()
        usage_self = self.usage(hours=24)
        cfg = self._config
        now = datetime.now(UTC).isoformat()

        # backlog 计数
        pending_approvals = self._pending_approval_count()
        failed_tasks = len(self._event_projections.tasks(status="failed"))
        unknown_deliveries = len(self._event_projections.deliveries(status="unknown"))
        paused_connectors = len(
            [c for c in self._connector_repo.find_active()
             if c.status.value == "paused"]
        )

        # readiness 判定
        readiness_reasons: list[str] = []
        readiness: str = "ready"
        if pending_approvals > 0:
            readiness_reasons.append(f"{pending_approvals} 个待审批")
        if failed_tasks > 0:
            readiness_reasons.append(f"{failed_tasks} 个失败任务")
        if unknown_deliveries > 0:
            readiness_reasons.append(f"{unknown_deliveries} 个未知投递")
        if paused_connectors > 0:
            readiness_reasons.append(f"{paused_connectors} 个连接器已暂停")
        if readiness_reasons:
            readiness = "degraded"

        return {
            "schema_version": "1",
            "generated_at": now,
            "profile": status["profile"],
            "readiness": readiness,
            "readiness_reasons": readiness_reasons,
            "counts": status["counts"],
            "usage_24h": {
                "calls": usage_self["windowed"].get("calls", 0),
                "input_tokens": usage_self["windowed"].get("input_tokens", 0),
                "output_tokens": usage_self["windowed"].get("output_tokens", 0),
                "cached_tokens": usage_self["windowed"].get("cached_tokens", 0),
                "avg_latency_ms": usage_self["windowed"].get("avg_latency_ms", 0),
                "errors": usage_self.get("recent_errors", 0),
            },
            "proactive": {
                "mode": "dry_run"
                if cfg.capability.proactive.dry_run
                else ("live" if cfg.capability.proactive.enabled else "disabled"),
                "candidates_queued": len(
                    self._candidate_repo.find_queued("owner", limit=1000)
                ),
                "decisions_24h": sum(
                    1 for event in self._event_store.read_stream_type("proactive_candidate")
                    if event.event_type == "proactive.decision.made"
                    and event.occurred_at >= self._now_ms() - 86400000
                ),
                "daily_budget_used": sum(
                    1 for event in self._event_store.read_stream_type("proactive_candidate")
                    if event.event_type == "proactive.decision.made"
                    and event.occurred_at >= self._now_ms() - 86400000
                    and not event.attributes.get("dry_run")
                ),
                "daily_budget_limit": cfg.capability.proactive.max_pushes_per_day,
                "quiet_hours_active": False,
            },
            "resources": {
                "sqlite_size_mb": 0.0,
                "payload_size_mb": 0.0,
                "trace_retention_days": 7,
                "backup_freshness_hours": None,
                "disk_pressure": "ok",
            },
            "worker": status["worker"],
        }

    def attention_items(self) -> list[dict[str, Any]]:
        """生成待处理事项列表。"""
        items: list[dict[str, Any]] = []
        rows = self._pending_approval_count()
        if rows:
            items.append(
                {
                    "kind": "approval",
                    "severity": "warn",
                    "label": "待审批",
                    "count": rows,
                    "target_route": "/commands",
                }
            )
        rows = len(self._event_projections.tasks(status="failed"))
        if rows:
            items.append(
                {
                    "kind": "failed_task",
                    "severity": "danger",
                    "label": "失败任务",
                    "count": rows,
                    "target_route": "/tasks",
                }
            )
        rows = len(self._event_projections.deliveries(status="unknown"))
        if rows:
            items.append(
                {
                    "kind": "unknown_delivery",
                    "severity": "warn",
                    "label": "未知投递",
                    "count": rows,
                    "target_route": "/deliveries",
                }
            )
        rows = len([
            m for m in self._event_projections.memories()
            if m.get("status") == "candidate"
        ])
        if rows:
            items.append(
                {
                    "kind": "memory_candidate",
                    "severity": "info",
                    "label": "待确认记忆",
                    "count": rows,
                    "target_route": "/memory",
                }
            )
        rows = len([
            c for c in self._connector_repo.find_active()
            if c.status.value == "paused"
        ])
        if rows:
            items.append(
                {
                    "kind": "connector_paused",
                    "severity": "warn",
                    "label": "连接器已暂停",
                    "count": rows,
                    "target_route": "/connectors",
                }
            )
        # Proactive dry-run 待复核
        rows = sum(
            1 for event in self._event_store.read_stream_type("proactive_candidate")
            if event.event_type == "proactive.decision.made"
            and event.occurred_at >= self._now_ms() - 86400000
            and event.attributes.get("dry_run")
        )
        if rows:
            items.append(
                {
                    "kind": "dry_run_review",
                    "severity": "info",
                    "label": "dry-run 待复核",
                    "count": rows,
                    "target_route": "/proactive",
                }
            )
        return items

    def health_components(self) -> dict[str, Any]:
        """组件级健康检查：liveness + readiness 分级。"""
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        components: list[dict[str, Any]] = []

        # ── Liveness（进程存活）──
        components.append({"name": "Liveness", "status": "ok", "detail": "API 进程存活"})

        # ── SQLite（Readiness）──
        try:
            self._conn.execute("SELECT 1").fetchone()
            db_size = self._conn.execute(
                "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()"
            ).fetchone()[0]
            size_mb = db_size / (1024 * 1024)
            components.append(
                {"name": "SQLite", "status": "ok", "detail": f"连接正常 · {size_mb:.1f} MB"}
            )
        except Exception as e:
            components.append({"name": "SQLite", "status": "danger", "detail": str(e)})

        # ── Worker ──
        components.append(
            {"name": "Worker", "status": "ok", "detail": f"并发 {self._config.worker.concurrency}"}
        )

        # ── Scheduler ──
        due_count = len(self._schedule_repo.find_due(
            datetime.now(UTC), limit=100
        ))
        sched_status = "ok" if due_count == 0 else "warn"
        components.append(
            {
                "name": "Scheduler",
                "status": sched_status,
                "detail": f"{'无' if due_count == 0 else due_count + ' 个'}待触发调度",
            }
        )

        # ── Gateway ──
        components.append({"name": "Gateway", "status": "warn", "detail": "LangBot 状态未知"})

        # ── Provider ──
        model = self._config.model.main.model or "(stub)"
        configured = self._config.model.main.is_configured()
        components.append(
            {
                "name": "Provider",
                "status": "ok" if configured else "warn",
                "detail": f"{model} {'已配置' if configured else '未配置'}",
            }
        )

        # ── Connector Freshness ──
        stale = self._conn.execute(
            "SELECT COUNT(*) FROM connectors WHERE status='active' AND "
            "(last_success_at IS NULL OR last_success_at < ?)",
            (self._now_ms() - 86400000,),
        ).fetchone()[0]
        total_conn = self._conn.execute("SELECT COUNT(*) FROM connectors").fetchone()[0]
        conn_status = "ok" if stale == 0 else "warn"
        components.append(
            {
                "name": "Connector",
                "status": conn_status,
                "detail": f"{total_conn} 个 · {stale} 个超 24h 未成功",
            }
        )

        # ── Delivery Backlog ──
        pending_del = sum(
            1
            for delivery in self._event_projections.deliveries()
            if delivery["status"] in {"pending", "sending", "scheduled"}
        )
        del_status = "ok" if pending_del < 10 else ("warn" if pending_del < 50 else "danger")
        components.append(
            {"name": "Delivery", "status": del_status, "detail": f"{pending_del} 个待处理投递"}
        )

        # Overall readiness 判定
        overall = "healthy"
        if any(c["status"] == "danger" for c in components):
            overall = "blocked"
        elif any(c["status"] == "warn" for c in components):
            overall = "degraded"

        return {
            "schema_version": "1",
            "generated_at": now,
            "overall": overall,
            "components": components,
        }

    # ── proactive ───────────────────────────────────────────────

    def proactive_status(self) -> dict[str, Any]:
        """主动系统当前状态。"""
        from datetime import UTC
        from datetime import datetime as _dt

        cfg = self._config
        now_ms = int(_dt.now(UTC).timestamp() * 1000)
        # 读 policy（Event-first: repo.get_current returns default）
        try:
            policy = self._policy_repo.get_current("owner")
            policy_dry_run = policy.dry_run
            version = policy.version
            qh = getattr(policy, "quiet_hours", {"enabled": True, "start": "23:00", "end": "08:00"})
            hourly = policy.max_pushes_per_hour
            daily = policy.max_pushes_per_day
        except Exception:
            policy_dry_run, version, qh, hourly, daily = True, 1, {"enabled": True, "start": "23:00", "end": "08:00"}, 3, 10
        quiet_active = False
        if qh.get("enabled"):
            try:
                now_h = _dt.now().hour
                s, e = int(str(qh["start"]).split(":")[0]), int(str(qh["end"]).split(":")[0])
                quiet_active = (now_h >= s or now_h < e) if s > e else (s <= now_h < e)
            except (ValueError, KeyError):
                quiet_active = False
        # energy_value from cadence events
        from cogito.store.event_replay import replay_proactive_candidate

        energy_value = 0.0
        candidates_queued = len(self._candidate_repo.find_queued("owner", limit=500))
        decisions_24h = sum(
            1 for event in self._event_store.read_stream_type("proactive_candidate")
            if event.event_type == "proactive.decision.made"
            and event.occurred_at >= now_ms - 86400000
        )
        daily_used = sum(
            1 for event in self._event_store.read_stream_type("proactive_candidate")
            if event.event_type == "proactive.decision.made"
            and event.occurred_at >= now_ms - 86400000
            and not event.attributes.get("dry_run")
        )
        return {
            "enabled": bool(cfg.capability.proactive.enabled),
            "dry_run": bool(cfg.capability.proactive.dry_run or policy_dry_run),
            "global_dry_run": bool(cfg.capability.proactive.dry_run),
            "default_principal_id": "owner",
            "quiet_hours_start": int(str(qh.get("start", "23:00")).split(":")[0]),
            "quiet_hours_end": int(str(qh.get("end", "08:00")).split(":")[0]),
            "hourly_budget": hourly,
            "daily_budget": daily,
            "energy_value": energy_value,
            "policy_version": f"v{version}",
            "candidates_queued": candidates_queued,
            "decisions_24h": decisions_24h,
            "daily_budget_used": daily_used,
            "quiet_hours_active": quiet_active,
        }

    def proactive_fetch_run(self, poll_task_id: str) -> dict[str, Any] | None:
        task = self._task_repo.get(poll_task_id)
        if task is None or task.task_type != "mcp_connector.poll":
            return None
        # ingestion batch lookup from Event stream (connector.source.ingested events)
        candidate_count = 0
        decision_count = 0
        evaluating_count = 0
        for event in self._event_store.read_stream_type("source"):
            if event.attributes.get("connector_id", "") == task.payload_ref:
                candidate_count += 1
                # Check if this source has a corresponding proactive_candidate decision
                candidate_id = f"{task.payload_ref}:{event.attributes.get('source_item_id', '')}"
                for ce in self._event_store.read_stream_type("proactive_candidate"):
                    if ce.stream_id == candidate_id and ce.event_type == "proactive.decision.made":
                        decision_count += 1
        evaluating_count = sum(
            1 for ce in self._event_store.read_stream_type("proactive_candidate")
            if ce.event_type == "proactive.candidate.created"
            and ce.attributes.get("status", "") == "evaluating"
        )
        done = candidate_count > 0 and decision_count >= candidate_count and evaluating_count == 0
        return {
            "poll_task_id": poll_task_id,
            "poll_status": task.status.value,
            "candidate_count": int(candidate_count),
            "decision_count": int(decision_count),
            "evaluating_count": int(evaluating_count),
            "done": bool(done),
            "failed": task.status.value == "failed",
            "error": str(batch["error_ref"] or "") if batch is not None else "",
        }

    def list_proactive_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        """List proactive candidates from Event replay."""
        candidates = []
        for event in self._event_store.read_stream_type("proactive_candidate"):
            if event.event_type == "proactive.candidate.created":
                candidates.append({
                    "candidate_id": event.stream_id,
                    "principal_id": event.context.principal_id or event.attributes.get("principal_id", "owner"),
                    "stream_type": event.attributes.get("stream_type", ""),
                    "status": event.outcome or event.attributes.get("status", "evaluating"),
                    "origin": event.attributes.get("origin", ""),
                    "relevance_score": float(event.attributes.get("relevance", 0)),
                    "created_at": event.occurred_at,
                    "source_payload_ref": event.payload_ref or "",
                    "source_type": event.attributes.get("origin", "connector"),
                })
        candidates.sort(key=lambda c: c.get("created_at") or 0, reverse=True)
        return candidates[:limit]

    def list_proactive_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        """List proactive decisions from Event replay."""
        decisions = []
        for event in self._event_store.read_stream_type("proactive_candidate"):
            if event.event_type == "proactive.decision.made":
                decisions.append({
                    "decision_id": event.attributes.get("decision_id", ""),
                    "candidate_id": event.stream_id,
                    "principal_id": event.context.principal_id or event.attributes.get("principal_id", "owner"),
                    "action": event.attributes.get("action", ""),
                    "policy_version": event.attributes.get("policy_version", 0),
                    "dry_run": event.attributes.get("dry_run", True),
                    "decided_at": event.occurred_at,
                    "delivery_id": event.attributes.get("delivery_id", ""),
                    "rule_trace": event.attributes.get("rule_results", "{}"),
                })
        decisions.sort(key=lambda d: d.get("decided_at") or 0, reverse=True)
        return decisions[:limit]

    def list_scheduled_requests(self) -> list[dict[str, Any]]:
        return []

    def list_digests(self) -> list[dict[str, Any]]:
        rows = self._digest_repo.find_all("owner", limit=30)
        return [
            d.to_dict()
            if hasattr(d, "to_dict")
            else {
                "digest_id": d.digest_id,
                "principal_id": d.principal_id,
                "topic": getattr(d, "topic", "general"),
                "digest_date": d.digest_date,
                "item_count": d.item_count,
                "status": d.status,
            }
            for d in rows
        ]

    def proactive_feedback(self) -> dict[str, Any]:
        """PLAN-17 R6 DR-P1-06: 真值驱动 feedback 统计 (不再硬编码 0)。

        主动反馈通过 proactive_feedback signal 持久化 (proactive_signals 表);
        此处按 action/event_type 聚合; 无记录时返回真实 0。
        """
        actions: dict[str, int] = {}
        try:
            for row in self._conn.execute(
                "SELECT action, COUNT(*) AS n FROM proactive_decisions_v2 "
                "WHERE action IS NOT NULL GROUP BY action"
            ).fetchall():
                actions[row["action"]] = row["n"]
        except Exception:
            pass
        # Delivery 结果分布由 Event 生命周期重放，不读旧 receipt projection。
        receipt_kinds: dict[str, int] = {}
        receipt_kind_by_event = {
            "delivery.completed": "confirmed",
            "delivery.failed": "failed",
            "delivery.unknown": "unknown",
            "delivery.cancelled": "cancelled",
        }
        for event in self._event_store.read_stream_type("delivery"):
            kind = receipt_kind_by_event.get(event.event_type)
            if kind:
                receipt_kinds[kind] = receipt_kinds.get(kind, 0) + 1
        # proactive_feedback signals — from Event stream
        fb: dict[str, int] = {}
        for event in self._event_store.read_stream_type("proactive_candidate"):
            if event.event_type == "proactive.decision.made":
                action = event.attributes.get("action", "")
                if action in {"silent", "send_later"}:
                    fb[action] = fb.get(action, 0) + 1
        return {
            "opened": receipt_kinds.get("confirmed", 0),
            "ignored": fb.get("ignored", 0),
            "dismissed": fb.get("dismissed", 0),
            "useful": actions.get("send_now", 0),
            "not_useful": fb.get("not_useful", actions.get("discard", 0)),
            "muted": actions.get("silent", 0),
            "requested_more": fb.get("requested_more", actions.get("send_later", 0)),
        }

    # ── Drift Dashboard (R9 M6) ──────────────────────────────────────

    def drift_status(self, principal_id: str = "owner") -> dict[str, Any]:
        """Drift 模块当前状态（替代占位值，返回真实数据）。"""
        event_runs = self._event_drift_runs(principal_id)
        if event_runs:
            latest = max(event_runs, key=lambda run: int(run.get("created_at") or 0))
            active = sum(
                run.get("status") in {"admitted", "running", "waiting", "paused"}
                for run in event_runs
            )
            latest_reason = next(
                (
                    run.get("preemption_reason")
                    for run in sorted(
                        event_runs,
                        key=lambda run: int(run.get("finished_at") or run.get("created_at") or 0),
                        reverse=True,
                    )
                    if run.get("preemption_reason")
                ),
                None,
            )
            return {
                "enabled": True,
                "total_runs": len(event_runs),
                "active_runs": active,
                "latest_preemption_reason": latest_reason,
                "latest_skill": latest.get("skill_name"),
                "latest_skill_version": latest.get("skill_version"),
                "signals_pending": self._conn.execute(
                    "SELECT COUNT(*) FROM drift_preemption_signals "
                    "WHERE principal_id=? AND preempt_requested=1",
                    (principal_id,),
                ).fetchone()[0],
            }
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status IN ('admitted','running','waiting','paused') "
            "THEN 1 ELSE 0 END) AS active "
            "FROM drift_runs WHERE principal_id=?",
            (principal_id,),
        ).fetchone()
        total = row["total"] or 0
        active = row["active"] or 0

        # 最近一次抢占原因（真实值，不再是 None）
        preempt_row = self._conn.execute(
            "SELECT preemption_reason FROM drift_runs WHERE principal_id=? "
            "AND preemption_reason IS NOT NULL "
            "ORDER BY finished_at DESC, created_at DESC LIMIT 1",
            (principal_id,),
        ).fetchone()
        latest_preemption_reason = preempt_row["preemption_reason"] if preempt_row else None

        # 最近一次 Skill 选择
        skill_row = self._conn.execute(
            "SELECT skill_name, skill_version FROM drift_runs WHERE principal_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (principal_id,),
        ).fetchone()
        return {
            "enabled": True,  # 由路由层注入 config 控制；此处表示模块可用
            "total_runs": total,
            "active_runs": active,
            "latest_preemption_reason": latest_preemption_reason,  # 真实值
            "latest_skill": skill_row["skill_name"] if skill_row else None,
            "latest_skill_version": skill_row["skill_version"] if skill_row else None,
            "signals_pending": self._conn.execute(
                "SELECT COUNT(*) FROM drift_preemption_signals "
                "WHERE principal_id=? AND preempt_requested=1",
                (principal_id,),
            ).fetchone()[0],
        }

    def list_drift_runs(self, principal_id: str = "owner", limit: int = 50) -> list[dict[str, Any]]:
        event_runs = self._event_drift_runs(principal_id)
        if event_runs:
            return [
                {key: run.get(key) for key in (
                    "drift_run_id", "skill_name", "skill_version", "status",
                    "preemption_reason", "steps_taken", "budget_used_json",
                    "started_at", "finished_at", "created_at",
                )}
                for run in sorted(event_runs, key=lambda r: int(r.get("created_at") or 0), reverse=True)[:limit]
            ]
        return []

    def list_drift_skill_states(self, principal_id: str = "owner") -> list[dict[str, Any]]:
        return DriftSkillStateRepository(self._conn).all_states(principal_id)

    def drift_metrics(self, principal_id: str = "owner") -> dict[str, Any]:
        """Drift 聚合指标（仅 Event-first 路径）。"""
        event_runs = self._event_drift_runs(principal_id)
        if not event_runs:
            return {
                "total_runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "paused_runs": 0,
                "canceled_runs": 0,
                "success_rate": None,
                "avg_steps_taken": None,
                "avg_steps_per_completed": None,
                "skills_trained": 0,
                "preemption_count": 0,
                "duplicate_side_effect_count": 0,
                "unauthorized_tool_execution_count": 0,
                "model_cost_per_useful_result": None,
            }
        total = len(event_runs)
        completed = sum(1 for r in event_runs if r.get("status") == "completed")
        failed = sum(1 for r in event_runs if r.get("status") == "failed")
        paused = sum(1 for r in event_runs if r.get("status") == "paused")
        cancelled = sum(1 for r in event_runs if r.get("status") == "cancelled")
        preempted = sum(1 for r in event_runs if r.get("preemption_reason"))
        skills = len({r.get("skill_name") for r in event_runs if r.get("skill_name")})
        steps = [int(r.get("steps_taken") or 0) for r in event_runs]
        avg_steps = sum(steps) / len(steps) if steps else None
        completed_steps = [
            int(r.get("steps_taken") or 0) for r in event_runs if r.get("status") == "completed"
        ]
        avg_completed_steps = sum(completed_steps) / len(completed_steps) if completed_steps else None
        success_rate = completed / total if total > 0 else None
        return {
            "total_runs": total,
            "completed_runs": completed,
            "failed_runs": failed,
            "paused_runs": paused,
            "canceled_runs": cancelled,
            "success_rate": success_rate,
            "avg_steps_taken": avg_steps,
            "avg_steps_per_completed": avg_completed_steps,
            "skills_trained": skills,
            "preemption_count": preempted,
            "duplicate_side_effect_count": sum(
                1 for r in event_runs
                if r.get("finish_summary") and "duplicate" in str(r.get("finish_summary")).lower()
            ),
            "unauthorized_tool_execution_count": sum(
                1 for r in event_runs
                if r.get("finish_summary") and "unauthorized" in str(r.get("finish_summary")).lower()
            ),
            "model_cost_per_useful_result": None,
        }

    def _event_drift_runs(self, principal_id: str) -> list[dict[str, Any]]:
        if not self._event_store.read_stream_type("drift_run", limit=1):
            return []
        return DriftRunRepository(self._conn).list_runs(principal_id)

    # ── canonical Event Explorer ────────────────────────────────

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_canonical_events(limit=limit)

    # ── delivery detail (attempts + receipts) ────────────────────

    def get_delivery_detail(self, delivery_id: str) -> dict[str, Any] | None:
        """投递详情：delivery + attempts timeline + receipts。"""
        events = self._event_store.read_stream("delivery", delivery_id)
        projection = replay_delivery(events, delivery_id)
        if projection is not None:
            request = next(event for event in events if event.event_type == "delivery.requested")
            target_snapshot: dict[str, Any] = {}
            content_ref: str | None = None
            idempotency_key = ""
            if request.payload_ref:
                try:
                    payload = load_delivery_effect_payload(
                        PayloadStore(self._config.resolve_payload_dir(), self._conn),
                        request.payload_ref,
                    )
                    target_snapshot = payload.target_snapshot
                    content_ref = payload.content_ref
                    idempotency_key = payload.idempotency_key
                except (LookupError, ValueError):
                    pass
            attempts = [
                {
                    "attempt_id": event.context.attempt_id or event.event_id,
                    "status": "sending",
                    "started_at": event.occurred_at,
                    "receipts": [],
                    "failure_reason": None,
                }
                for event in events
                if event.event_type == "delivery.started"
            ]
            operation_sequence = [
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "outcome": event.outcome,
                    "error_category": event.error_category,
                    "platform_message_id": event.attributes.get("platform_message_id"),
                    "observed_at": event.occurred_at,
                }
                for event in events
            ]
            return {
                "delivery_id": delivery_id,
                "status": projection.status,
                "target_snapshot": target_snapshot,
                "content_ref": content_ref,
                "idempotency_key": idempotency_key,
                "attempt_count": len(attempts),
                "platform_message_id": projection.platform_message_id,
                "stream_version": projection.stream_version,
                "attempts": attempts,
                "operation_sequence": operation_sequence,
                "related_turn": request.context.turn_id or None,
                "related_message": None,
            }
        return None

    # ── proactive context (PROACTIVE_CONTEXT.md) ────────────────

    def get_proactive_context(self) -> dict[str, Any]:
        """读取当前 PROACTIVE_CONTEXT.md 内容 + 当前 policy 版本。"""
        from pathlib import Path

        workspace = Path(self._config.workspace_path)
        context_file = workspace / "PROACTIVE_CONTEXT.md"
        content = ""
        if context_file.exists():
            content = context_file.read_text(encoding="utf-8")
        # Use Event-first ProactivePolicyRepository
        try:
            policy = self._policy_repo.get_current("owner")
            version = policy.version
            dry_run = policy.dry_run
        except Exception:
            version = 1
            dry_run = True
        return {
            "content": content,
            "policy_version": version,
            "dry_run": dry_run,
            "file_exists": context_file.exists(),
        }

    def proactive_context_diff(self, new_content: str) -> dict[str, Any]:
        """计算当前文件与新内容的 diff。"""
        import difflib
        from pathlib import Path

        workspace = Path(self._config.workspace_path)
        context_file = workspace / "PROACTIVE_CONTEXT.md"
        current = ""
        if context_file.exists():
            current = context_file.read_text(encoding="utf-8")
        current_lines = current.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = list(
            difflib.unified_diff(
                current_lines,
                new_lines,
                fromfile="current",
                tofile="proposed",
                lineterm="",
            )
        )
        return {
            "has_changes": current != new_content,
            "diff_lines": diff,
            "added_lines": sum(
                1 for line in diff if line.startswith("+") and not line.startswith("+++")
            ),
            "removed_lines": sum(
                1 for line in diff if line.startswith("-") and not line.startswith("---")
            ),
        }

    # ── connector detail ─────────────────────────────────────────

    def list_mcp_connector_configs(self) -> list[dict[str, Any]]:
        """读 mcp_connector_configs 表 → MCP 服务器列表。"""
        rows = self._conn.execute(
            "SELECT m.*, c.name, c.connector_type, c.status "
            "FROM mcp_connector_configs m "
            "JOIN connectors c ON c.connector_id = m.connector_id "
            "ORDER BY m.server_name ASC LIMIT 100"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_connector_detail(self, connector_id: str) -> dict[str, Any] | None:
        """连接器详情：connector + cursor + items + ingestion stats from Event replay."""
        connector = self._connector_repo.get(connector_id)
        if connector is None:
            return None
        result = {
            "connector_id": connector.connector_id,
            "name": connector.name,
            "connector_type": connector.connector_type.value,
            "url": connector.url or "",
            "status": connector.status.value,
        }
        # cursor from Event replay
        try:
            cursor = ConnectorCursorRepository(self._conn).get(connector_id)
            result["cursor"] = {
                "etag": cursor.etag if cursor else None,
                "last_modified": cursor.last_modified if cursor else None,
                "last_polled_at": cursor.last_polled_at.isoformat() if cursor and cursor.last_polled_at else None,
            } if cursor else None
        except Exception:
            result["cursor"] = None
        # ingestion stats from Event stream
        ingested_count = 0
        for event in self._event_store.read_stream_type("source"):
            if event.attributes.get("connector_id", "") == connector_id:
                ingested_count += 1
        result["ingestion_stats"] = {"new": ingested_count, "digest": 0}
        result["items"] = []
        # Event facts from event_log
        event_ids = self._conn.execute(
            "SELECT event_id FROM event_log WHERE stream_id=? OR "
            "(json_valid(attributes_json)=1 AND json_extract(attributes_json, '$.connector_id')=?) "
            "ORDER BY occurred_at DESC, event_id DESC LIMIT 50",
            (connector_id, connector_id),
        ).fetchall()
        result["events"] = [
            _public_event(event.to_dict())
            for row in event_ids
            if (event := self._event_store.get(row["event_id"])) is not None
        ]
        return result

    # ── audit ────────────────────────────────────────────────────

    def list_audit(
        self, entity_id: str | None = None, action: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM audit_records WHERE 1=1"
        params: list[Any] = []
        if entity_id:
            q += " AND target_id=?"
            params.append(entity_id)
        if action:
            q += " AND action=?"
            params.append(action)
        q += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # ── capabilities / tool-calls / receipts / skills ────────────

    def list_capabilities(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM capabilities ORDER BY discovered_at DESC LIMIT 200"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_skills(self) -> list[dict[str, Any]]:
        """读 skills 表。"""
        try:
            rows = self._conn.execute("SELECT * FROM skills ORDER BY name ASC LIMIT 200").fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_capability(self, capability_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM capabilities WHERE capability_id=?",
            (capability_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_skill(self, name: str) -> dict[str, Any] | None:
        try:
            row = self._conn.execute(
                "SELECT * FROM skills WHERE name=?",
                (name,),
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def list_schedules(self, limit: int = 100) -> dict[str, Any]:
        items = [
            schedule.to_dict()
            for schedule in self._schedule_repo.find_all(
                max(1, min(limit, 100)),
            )
        ]
        return {"items": items, "total": len(items), "limit": min(limit, 100)}

    def list_schedules_for_principal(self, principal_id: str, *, limit: int, offset: int) -> dict[str, Any]:
        """List prompt schedules for a principal from Event replay."""
        repo = self._schedule_repo
        all_schedules = repo.find_all(limit=1000)
        filtered = [
            s for s in all_schedules
            if s.task_type == "agent.prompt"
            and s.task_payload
            and "principal_id" in (s.task_payload or "")
        ]
        # Filter by principal_id in task_payload JSON
        import json
        matched = []
        for s in filtered:
            try:
                payload = json.loads(s.task_payload)
                if payload.get("principal_id") == principal_id:
                    matched.append(s)
            except (json.JSONDecodeError, TypeError):
                continue
        matched.sort(key=lambda s: s.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        page = matched[offset:offset + limit]
        items = [_public_schedule(item.to_dict()) for item in page if item is not None]
        return {"items": items, "total": len(matched)}

    def search_knowledge(
        self,
        query: str,
        *,
        principal_id: str = "owner",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Safe lexical search over inline knowledge segments."""
        escaped = query.replace("%", "\\%").replace("_", "\\_")
        rows = self._conn.execute(
            "SELECT s.segment_id,s.document_id,s.heading_path,s.text_ref_or_inline,"
            "r.trust_label FROM knowledge_segments s "
            "JOIN knowledge_documents d ON d.document_id=s.document_id "
            "JOIN knowledge_resources r ON r.resource_id=d.resource_id "
            "WHERE r.principal_id=? AND r.status='active' AND s.deleted_at IS NULL "
            "AND s.text_ref_or_inline LIKE ? ESCAPE '\\' "
            "ORDER BY s.document_id,s.ordinal LIMIT ?",
            (principal_id, f"%{escaped}%", max(1, min(limit, 50))),
        ).fetchall()
        return {"items": [dict(row) for row in rows], "query": query}

    def search_knowledge_page(
        self,
        query: str,
        *,
        principal_id: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        escaped = query.replace("%", "\\%").replace("_", "\\_")
        joins = (
            " FROM knowledge_segments s "
            "JOIN knowledge_documents d ON d.document_id=s.document_id "
            "JOIN knowledge_resources r ON r.resource_id=d.resource_id "
        )
        where = (
            "WHERE r.principal_id=? AND r.status='active' AND s.deleted_at IS NULL "
            "AND s.text_ref_or_inline LIKE ? ESCAPE '\\'"
        )
        params = (principal_id, f"%{escaped}%")
        total_row = self._conn.execute(
            "SELECT COUNT(*)" + joins + where,
            params,
        ).fetchone()
        rows = self._conn.execute(
            "SELECT s.segment_id,s.document_id,s.heading_path,s.text_ref_or_inline,"
            "r.trust_label" + joins + where + " ORDER BY s.document_id,s.ordinal LIMIT ? OFFSET ?",
            (*params, max(1, min(limit, 100)), max(0, offset)),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": int(total_row[0]) if total_row else 0,
        }

    def list_tool_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        return [record.__dict__.copy() for record in self._tool_call_repo.list_recent(limit)]

    def list_receipts(self, limit: int = 50) -> list[dict[str, Any]]:
        return [record.__dict__.copy() for record in self._receipt_repo.list_recent(limit)]

    def list_reconcile_pending(self) -> list[dict[str, Any]]:
        return [record.__dict__.copy() for record in self._receipt_repo.find_pending_reconcile()]

    # ── storage / config ─────────────────────────────────────────

    def storage_summary(self) -> dict[str, Any]:
        """SQLite + Payload 存储统计。"""
        from pathlib import Path

        db_path = self._config.resolve_db_path()
        payload_dir = self._config.resolve_payload_dir()
        db_size = Path(db_path).stat().st_size / (1024 * 1024) if Path(db_path).exists() else 0.0
        wal_path = db_path + "-wal"
        wal_size = Path(wal_path).stat().st_size / (1024 * 1024) if Path(wal_path).exists() else 0.0
        payload_size = 0.0
        payload_obj_count = 0
        pdir = Path(payload_dir)
        if pdir.exists():
            for f in pdir.rglob("*"):
                if f.is_file():
                    payload_size += f.stat().st_size
                    payload_obj_count += 1
            payload_size /= 1024 * 1024
        objects = self._conn.execute("SELECT COUNT(*) FROM payload_objects").fetchone()[0]
        orphans = self._conn.execute(
            "SELECT COUNT(*) FROM payload_objects p WHERE p.payload_ref NOT IN "
            "(SELECT payload_ref FROM event_log WHERE payload_ref IS NOT NULL)"
        ).fetchone()[0]
        return {
            "db_path": db_path,
            "db_size_mb": round(db_size, 2),
            "wal_size_mb": round(wal_size, 2),
            "payload_dir": payload_dir,
            "payload_size_mb": round(payload_size, 2),
            "object_count": objects,
            "orphan_count": orphans,
            "backup_count": self._conn.execute(
                "SELECT COUNT(*) FROM scheduled_delivery_request"
            ).fetchone()[0]
            if False
            else 0,
            "latest_backup_at": None,
            "latest_restore_drill_at": None,
        }

    def list_backups(self) -> list[dict[str, Any]]:
        """备份记录（来自 backups 表）。"""
        rows = self._conn.execute(
            "SELECT * FROM backups ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_config_versions(self) -> list[dict[str, Any]]:
        latest = self._config_version_repo.latest()
        if latest is None:
            from datetime import UTC, datetime

            return [
                {
                    "version_id": "current",
                    "config_version": self._config.config_version,
                    "content_hash": self._config.content_hash or "—",
                    "active": True,
                    "created_at": datetime.now(UTC).isoformat(),
                    "source_layers": ["config.toml"],
                }
            ]
        return [
            {
                "version_id": latest.version_id,
                "config_version": str(latest.applied_at),
                "content_hash": latest.content_hash,
                "active": True,
                "created_at": latest.applied_at,
                "source_layers": latest.source_layers,
            }
        ]

    # ── Knowledge 查询（PLAN-14 R-14）──────────────────────────

    def list_knowledge_resources(
        self, *, principal_id: str = "owner", limit: int = 50, status_filter: str = ""
    ) -> list[dict]:
        """列出知识资源摘要。"""
        from cogito.service.explain import ExplainService

        return ExplainService(self._conn).list_knowledge_resources(
            principal_id=principal_id,
            limit=limit,
            status_filter=status_filter,
        )

    def get_knowledge_resource(self, resource_id: str) -> dict | None:
        """获取单资源详情 + 段统计。"""
        from cogito.service.explain import ExplainService

        return ExplainService(self._conn).get_knowledge_resource(resource_id)

    def explain_knowledge_retrieval(self, resource_id: str) -> dict | None:
        """解释资源是否可检索、检索路径覆盖。"""
        from cogito.service.explain import ExplainService

        return ExplainService(self._conn).explain_knowledge_retrieval(resource_id)

    def explain_memory_weight(self, memory_id: str) -> dict | None:
        """解释记忆权重分项（PLAN-13 Enable 后暴露）。"""
        from cogito.service.explain import ExplainService

        return ExplainService(self._conn).explain_memory_weight(memory_id)

    def list_memory_sources(self, memory_id: str) -> list[dict]:
        """列出记忆来源集合。"""
        from cogito.service.explain import ExplainService

        return ExplainService(self._conn).list_memory_sources(memory_id)

    def get_memory_detail(self, memory_id: str) -> dict | None:
        """获取记忆详情安全摘要。"""
        from cogito.service.explain import ExplainService

        return ExplainService(self._conn).get_memory_detail(memory_id)

    # ── PLAN-16 M7 OPS-04: Memory/Knowledge 专项指标 ───────────

    def cognition_metrics(self) -> dict[str, Any]:
        """Memory/Knowledge 专项运行指标（PLAN-16 M7 OPS-04 完整）。"""
        # PLAN-16 完整：通过 metrics_access registry 读取真实注入的计数器
        from cogito.infrastructure.metrics_access import get_cognition_metrics
        from cogito.service.cognition_metrics_service import CognitionMetricsService

        return CognitionMetricsService(self._conn, metrics=get_cognition_metrics()).snapshot()

    # ── Context Snapshot 查询（PLAN-16 M7 OPS-03）──────────────

    def get_context_snapshot(self, snapshot_id: str) -> dict | None:
        """返回某次 Turn 构建的上下文快照（含 items / 来源 / 分数 / 排除统计）。"""
        from cogito.store.context_snapshot_repo import ContextSnapshotRepository

        record = ContextSnapshotRepository(self._conn).get(snapshot_id)
        if record is None:
            return None
        return {
            "snapshot_id": record.snapshot_id,
            "session_id": record.session_id,
            "attempt_id": record.attempt_id,
            "attempt_type": record.attempt_type,
            "message_upper_bound": record.message_upper_bound,
            "query_plan_version": record.query_plan_version,
            "selection_policy_version": record.selection_policy_version,
            "token_budget": record.token_budget,
            "tokens_used": record.tokens_used,
            "excluded_summary": record.excluded_summary,
            "per_source_tokens": record.per_source_tokens,
            "exclusion_stats": record.exclusion_stats,
            "excluded": list(record.excluded),
            "created_at": record.created_at,
            "items": [
                {
                    "index": it.item_index,
                    "source": it.source,
                    "score": it.score,
                    "tokens": it.tokens,
                    "trust_label": it.trust_label,
                    "retrieval_path": it.retrieval_path,
                    "provenance": it.provenance,
                }
                for it in record.items
            ],
        }

    def explain_context_selection(self, snapshot_id: str) -> dict | None:
        """解释某次 Turn 选中/排除的原因（PLAN-16 M7 OPS-03）。

        通过快照已存储的来源版本 / 分数 / 检索路径 / policy version 给出可解释摘要。
        """
        snap = self.get_context_snapshot(snapshot_id)
        if snap is None:
            return None
        selected_by_source: dict[str, list[dict]] = {}
        for it in snap["items"]:
            selected_by_source.setdefault(it["source"], []).append(it)
        return {
            "snapshot_id": snap["snapshot_id"],
            "session_id": snap["session_id"],
            "query_plan_version": snap["query_plan_version"],
            "selection_policy_version": snap["selection_policy_version"],
            "token_budget": snap["token_budget"],
            "tokens_used": snap["tokens_used"],
            "per_source_tokens": snap["per_source_tokens"],
            "exclusion_stats": snap["exclusion_stats"],
            "excluded": list(snap.get("excluded", [])),
            "selected_by_source": {
                src: [
                    {
                        "index": it["index"],
                        "score": it["score"],
                        "retrieval_path": it["retrieval_path"],
                        "trust_label": it["trust_label"],
                        "provenance": it["provenance"],
                    }
                    for it in items
                ]
                for src, items in selected_by_source.items()
            },
            "total_selected": len(snap["items"]),
        }


def _public_task(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "task_id",
        "task_type",
        "status",
        "priority",
        "scheduled_at",
        "origin",
        "created_at",
    )
    return {key: value.get(key) for key in keys}


def _public_task_attempt(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "task_attempt_id",
        "task_id",
        "attempt_no",
        "status",
        "started_at",
        "finished_at",
    )
    return {key: value.get(key) for key in keys}


def _public_schedule(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "schedule_id",
        "schedule_type",
        "expression",
        "timezone",
        "enabled",
        "next_fire_at",
        "last_fire_at",
        "version",
        "created_at",
    )
    return {key: value.get(key) for key in keys}


# ── PLAN-09 M4b 兼容别名：保留 QueryService 类名，同时暴露
#    SqliteQueryService 以便 interaction_web.query.py 引用 ──
SqliteQueryService = QueryService


def _public_event(value: dict[str, Any]) -> dict[str, Any]:
    """Remove write-only idempotency metadata from Event read APIs."""
    result = dict(value)
    result.pop("idempotency_key", None)
    return result
