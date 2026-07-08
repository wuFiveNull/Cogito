"""QueryService —— 只读查询薄服务层。

ACCESS-DELIVERY §2.2 Query API 的数据来源。
所有只读视图在这里封装，供 FastAPI handler 调用。
handler 绝不直接操作数据库；所有数据访问都经由此服务与现有 repo/service。

内存检索统一走 RetrievalService；任务/轮次/对话等走对应 Repository；
模型调用量走 ModelCallRepository.usage_summary。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from cogito.config import Config
from cogito.service.retrieval_service import RetrievalService
from cogito.store.connector_repo import ConnectorRepository
from cogito.store.model_call_repo import ModelCallRepository
from cogito.store.repositories import TurnRepository
from cogito.store.task_repo import TaskAttemptRepository, TaskRepository
from cogito.store.time_utils import epoch_ms


class QueryService:
    """只读查询服务——handler 的唯一数据入口。"""

    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self._conn = conn
        self._config = config
        self._task_repo = TaskRepository(conn)
        self._attempt_repo = TaskAttemptRepository(conn)
        self._turn_repo = TurnRepository(conn)
        self._connector_repo = ConnectorRepository(conn)
        self._model_call_repo = ModelCallRepository(conn)
        self._retrieval = RetrievalService(conn)

    # ── status / usage ─────────────────────────────────────────

    def status(self, recovery_counts: dict[str, int] | None = None) -> dict[str, Any]:
        """系统运行状态快照。"""
        cfg = self._config
        turn_total = self._turn_repo.count()
        task_total = self._task_repo.count()
        memory_total = self._conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE deleted_at IS NULL"
        ).fetchone()[0]
        return {
            "profile": cfg.runtime.profile,
            "model_configured": cfg.model.main.is_configured(),
            "model": cfg.model.main.model or "(stub)",
            "db_path": cfg.resolve_db_path(),
            "counts": {
                "turns": turn_total,
                "tasks": task_total,
                "conversations": self._conn.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()[0],
                "sessions": self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "endpoints": self._conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0],
                "memory_items": memory_total,
                "connectors": self._conn.execute("SELECT COUNT(*) FROM connectors").fetchone()[0],
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
        # 最近失败数
        failed_row = self._conn.execute(
            "SELECT COUNT(*) FROM model_calls WHERE status='error' AND started_at >= ?",
            (since,),
        ).fetchone()
        return {
            "window_hours": hours,
            "windowed": windowed,
            "total": summary,
            "recent_errors": int(failed_row[0]) if failed_row else 0,
        }

    # ── turns ──────────────────────────────────────────────────

    def list_turns(self, status: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        rows = self._turn_repo.list_(status=status, limit=limit, offset=offset)
        total = self._turn_repo.count(status=status)
        return {
            "items": [t.to_dict() for t in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        turn = self._turn_repo.get(turn_id)
        if turn is None:
            return None
        attempts = self._turn_repo.list_attempts(turn_id)
        return {"turn": turn.to_dict(), "attempts": [a.to_dict() for a in attempts]}

    # ── tasks ──────────────────────────────────────────────────

    def list_tasks(self, status: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        rows = self._task_repo.list_filtered(status=status, limit=limit, offset=offset)
        total = self._task_repo.count(status=status)
        return {
            "items": [t.to_dict() for t in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self._task_repo.get(task_id)
        if task is None:
            return None
        attempts = self._attempt_repo.list_for_task(task_id)
        return {"task": task.to_dict(), "attempts": [a.to_dict() for a in attempts]}

    # ── memory ─────────────────────────────────────────────────

    def search_memory(
        self,
        q: str = "",
        limit: int = 50,
        principal_id: str = "owner",
    ) -> dict[str, Any]:
        if q:
            scored = self._retrieval.retrieve(
                principal_id=principal_id, query=q, limit=limit,
            )
            items = [sm.to_dict() for sm in scored]
        else:
            rows = self._conn.execute(
                "SELECT * FROM memory_items "
                "WHERE deleted_at IS NULL AND status='confirmed' "
                "ORDER BY importance DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            items = [dict(r) for r in rows]
        return {"items": items, "query": q, "count": len(items)}

    # ── connectors ─────────────────────────────────────────────

    def list_connectors(self) -> dict[str, Any]:
        rows = self._connector_repo.find_active(limit=100)
        return {"items": [c.to_dict() for c in rows]}

    # ── channels / conversations / endpoints ───────────────────

    def list_channels(self) -> dict[str, Any]:
        """端点按渠道类型聚合。"""
        rows = self._conn.execute(
            "SELECT channel_type, COUNT(*) AS n FROM endpoints "
            "WHERE status='active' GROUP BY channel_type ORDER BY n DESC"
        ).fetchall()
        return {"items": [{"channel_type": r["channel_type"], "count": r["n"]} for r in rows]}

    def list_conversations(self, limit: int = 100) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT * FROM conversations ORDER BY conversation_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"items": [dict(r) for r in rows]}

    def get_conversation_messages(
        self, conversation_id: str, limit: int = 200,
    ) -> dict[str, Any]:
        """按会话取消息（含文本），用于聊天历史回放。

        一条消息的文本由 content_parts.inline_data 拼接；按 receive_sequence 升序。
        """
        rows = self._conn.execute(
            "SELECT m.message_id, m.role, m.created_at, m.receive_sequence, "
            "       cp.inline_data AS text "
            "FROM messages m "
            "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
            "WHERE m.conversation_id = ? "
            "ORDER BY m.receive_sequence ASC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # inline_data 可能为 None（纯元数据消息）
            d["text"] = d.get("text") or ""
            items.append(d)
        return {"conversation_id": conversation_id, "items": items}

    # ── deliveries ─────────────────────────────────────────────

    def list_deliveries(self, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM deliveries WHERE status=? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
            total = self._conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status=?", (status,),
            ).fetchone()[0]
        else:
            rows = self._conn.execute(
                "SELECT * FROM deliveries ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            total = self._conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        return {"items": [dict(r) for r in rows], "total": total}

    # ── traces ─────────────────────────────────────────────────

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        rows = self._model_call_repo.find_by_trace(trace_id)
        if not rows:
            return None
        # 关联的 run_attempts
        attempts = [
            dict(r) for r in self._conn.execute(
                "SELECT * FROM run_attempts WHERE attempt_id IN "
                "(SELECT attempt_id FROM model_calls WHERE trace_id=?) "
                "ORDER BY started_at ASC",
                (trace_id,),
            ).fetchall()
        ]
        return {
            "trace_id": trace_id,
            "model_calls": [r.to_dict() for r in rows],
            "attempts": attempts,
        }

    # ── sessions ───────────────────────────────────────────────

    def list_sessions(self, limit: int = 100) -> dict[str, Any]:
        """列出会话，附带 turn 数、最近活跃时间、conversation_id。"""
        rows = self._conn.execute(
            "SELECT s.session_id, s.conversation_id, s.status, s.created_at, "
            "       COUNT(t.turn_id) AS turn_count, "
            "       MAX(t.created_at) AS last_turn_at "
            "FROM sessions s "
            "LEFT JOIN turns t ON t.session_id = s.session_id "
            "GROUP BY s.session_id "
            "ORDER BY COALESCE(MAX(t.created_at), s.created_at) DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["turn_count"] = int(d["turn_count"])
            items.append(d)
        return {"items": items, "total": len(items)}

    def get_session_trace(self, session_id: str) -> dict[str, Any] | None:
        """聚合一个 session 的完整运行 trace：session 基本信息 + 所有 turns（含 attempts 与 model_calls）。

        trace_id == turn_id（runtime/loop.py），因此一个 session 的 trace 即其全部 turn 的
        RunAttempt 与 ModelCall 的时间线聚合。
        """
        session = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if session is None:
            return None
        turns = self._turn_repo.list_by_session(session_id)
        turns_out: list[dict[str, Any]] = []
        total_model_calls = 0
        total_input_tokens = 0
        total_output_tokens = 0
        for turn in turns:
            td = turn.to_dict()
            attempts = self._turn_repo.list_attempts(turn.turn_id)
            attempts_out: list[dict[str, Any]] = []
            for a in attempts:
                ad = a.to_dict()
                model_calls = [mc.to_dict() for mc in self._model_call_repo.find_by_attempt(a.attempt_id)]
                ad["model_calls"] = model_calls
                total_model_calls += len(model_calls)
                for mc in model_calls:
                    total_input_tokens += mc.get("input_tokens", 0) or 0
                    total_output_tokens += mc.get("output_tokens", 0) or 0
                attempts_out.append(ad)
            td["attempts"] = attempts_out
            turns_out.append(td)
        return {
            "session": dict(session),
            "turns": turns_out,
            "summary": {
                "turn_count": len(turns_out),
                "model_call_count": total_model_calls,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
            },
        }

    # ── plugins (capability mcp servers config 快照) ───────────

    def list_plugins(self) -> dict[str, Any]:
        servers = [
            {
                "name": s.name,
                "transport": s.transport,
                "enabled": s.enabled,
                "toolset": s.toolset,
            }
            for s in self._config.capability.mcp_servers
        ]
        return {"items": servers, "count": len(servers)}
