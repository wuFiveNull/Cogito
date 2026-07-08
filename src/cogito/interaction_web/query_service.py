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
                "sessions": self._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL"
                ).fetchone()[0],
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
        """列出会话，过滤掉所有 session 均已软删除的 conversation。

        规则：若 conversation 下存在至少一个未删除的 session，则保留；
        若 conversation 下无任何活跃 session（全部已删除或原本无 session），则排除。
        """
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
        """列出会话，附带 turn 数、最近活跃时间、conversation_id。

        过滤掉已软删除（deleted_at IS NOT NULL）的会话。
        每个 session 用其下最新一条用户提问（user role 消息文本）作为 name，
        便于在列表中直接识别会话内容。
        """
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
                    (a_end_ms - a_start_ms) if a_start_ms is not None and a_end_ms is not None else None
                )
                model_calls = [mc.to_dict() for mc in self._model_call_repo.find_by_attempt(a.attempt_id)]
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
                if turn_start_ms is not None and turn_end_ms is not None and turn_end_ms >= turn_start_ms
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
            dict(r) for r in self._conn.execute(
                "SELECT * FROM sessions WHERE conversation_id=? AND deleted_at IS NULL ORDER BY created_at ASC",
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
        turns = self._conn.execute(
            "SELECT * FROM turns WHERE session_id IN "
            "(SELECT session_id FROM sessions WHERE conversation_id=? AND deleted_at IS NULL) "
            "ORDER BY created_at ASC",
            (real_id,),
        ).fetchall()
        turns_out: list[dict[str, Any]] = []
        for t in turns:
            td = dict(t)
            # attempts
            attempts = self._conn.execute(
                "SELECT * FROM run_attempts WHERE turn_id=? ORDER BY attempt_no ASC",
                (t["turn_id"],),
            ).fetchall()
            attempts_out: list[dict[str, Any]] = []
            for a in attempts:
                ad = dict(a)
                # model calls
                model_calls = [mc.to_dict() for mc in self._model_call_repo.find_by_attempt(a["attempt_id"])]
                ad["model_calls"] = model_calls
                ad["model_call_count"] = len(model_calls)
                ad["error_calls"] = sum(1 for mc in model_calls if mc.get("status") == "error")
                attempts_out.append(ad)
            td["attempts"] = attempts_out
            td["attempt_count"] = len(attempts_out)
            # deliveries for this turn
            deliveries = self._conn.execute(
                "SELECT * FROM deliveries WHERE "
                "json_extract(target_snapshot, '$.conversation_id')=? "
                "AND created_at >= ? "
                "ORDER BY created_at ASC",
                (real_id, t["created_at"]),
            ).fetchall()
            del_out: list[dict[str, Any]] = []
            for d in deliveries:
                dd = dict(d)
                dd["last_error"] = dd.get("last_error")
                del_out.append(dd)
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
                msg_diag["detail"] = f"Turn 执行失败，模型调用错误: {err_calls}" if err_calls else "Turn 执行失败（详见 attempt）"
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

                failed_deliveries = [d for d in turn.get("deliveries", []) if d["status"] in ("failed", "cancelled", "interrupted")]
                if failed_deliveries:
                    msg_diag["issue"] = "DELIVERY_FAILED"
                    msg_diag["detail"] = f"Delivery 失败: {failed_deliveries[0].get('last_error') or failed_deliveries[0]['status']}"
                    issues.append(msg_diag)
                    continue

            msg_diag["issue"] = "OK"
            msg_diag["detail"] = "链路正常"
            issues.append(msg_diag)

        return issues

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
