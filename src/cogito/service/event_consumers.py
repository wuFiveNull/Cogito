"""Outbox Consumer 注册表 —— SourceEventIngested → ProactiveCandidate 投影。

参照 TASK-SCHEDULER / 1.7 Consumer 接口 + PROACTIVE-IDLE / 4 主动候选：
- Consumer handle(envelope) 必须幂等
- handle 失败不阻塞其他事件（retry/dead_letter）
- handle 只允许：更新自身 Projection、创建 Command/Task、记指标；
  禁止在消费事务里直接发送网络/创建 Delivery（由 Decision Engine 建 Task）

Seam 接入：application.process_background_once 在 OutboxWorker.publish() 前
派发到对应 consumer；成功才 publish，失败则 retry。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime

from cogito.service.outbox_worker import OutboxLease

_LOGGER = logging.getLogger(__name__)


# ── Consumer 协议 ─────────────────────────────────────────────────────────────

class EventConsumer:
    """单事件消费者的协议接口。

    实现 .can_handle(lease) → bool 与 .handle(conn, lease) → bool。
    """

    name: str = ""

    def can_handle(self, lease: OutboxLease) -> bool:
        raise NotImplementedError

    def handle(self, conn: sqlite3.Connection, lease: OutboxLease) -> bool:
        raise NotImplementedError


# ── 注册表 ───────────────────────────────────────────────────────────────────

class EventConsumerRegistry:
    """事件类型 → Consumer 的注册表。"""

    def __init__(self) -> None:
        self._consumers: list[EventConsumer] = []

    def register(self, consumer: EventConsumer) -> None:
        self._consumers.append(consumer)

    def find(self, lease: OutboxLease) -> EventConsumer | None:
        for c in self._consumers:
            if c.can_handle(lease):
                return c
        return None


# ── SourceEventIngested → ProactiveCandidate 投影 ────────────────────────────

class SourceEventIngestedConsumer(EventConsumer):
    """把 SourceEvent（外部 MCP 摄取）投影为 ProactiveCandidate。

    幂等键：
        principal_id + stream_type + sorted(source_event_ids) + policy_ver
    同事务内：Candidate + Candidate_Delivery + Outbox(SourceEventConsumed) +
    Inbox(pending→succeeded) 原子提交。

    首期简化：从 payload_ref（= connector_item.item_id）读 connector_items 取
    title/body/topic，结合 principal_id（data connector 默认 "owner"）生成
    Candidate。初始评分用 relevance（MCP handler 已算好）。
    """

    name = "proactive-candidate-projector"

    def __init__(self, *, default_principal_id: str = "owner") -> None:
        self._default_principal_id = default_principal_id

    def can_handle(self, lease: OutboxLease) -> bool:
        return lease.event_type == "SourceEventIngested"

    def handle(self, conn: sqlite3.Connection, lease: OutboxLease) -> bool:
        """投影一个 SourceEvent 成 Candidate。返回 True 表成功。"""
        conn.row_factory = sqlite3.Row
        # 1. 幂等：Inbox 唯一键 (consumer_name, event_id)
        existing = conn.execute(
            "SELECT status FROM event_consumptions "
            "WHERE consumer_name=? AND event_id=?",
            (self.name, lease.event_id),
        ).fetchone()
        if existing is not None:
            return True  # 已消费过

        # 2. 读取 connector_items
        item = conn.execute(
            "SELECT item_id, title, summary, source_item_id, relevance, status, "
            "       topic_json, published_at, content_hash "
            "FROM connector_items WHERE item_id=?",
            (lease.payload_ref or "",),
        ).fetchone()
        if item is None:
            _LOGGER.warning("SourceEventIngested consumer: item %s not found",
                            lease.payload_ref)
            return False

        # 候选只对 status='digest'（已决定进摘要）产生
        if item["status"] != "digest":
            _LOGGER.info("SourceEventIngested consumer: item status=%s, no candidate",
                         item["status"])
            return True  # 非 digest 静默跳过，不重试

        # 优先用 connector_items 上专用 topic 列（MCP handler 写入）
        item_row = conn.execute(
            "SELECT topic FROM connector_items WHERE item_id=?",
            (lease.payload_ref or "",),
        ).fetchone()
        topic = item_row["topic"] if item_row and item_row["topic"] else "general"

        candidate_id = uuid.uuid4().hex
        stream_type = "content"  # 默认；未来 enhancement 会看 source type
        title = item["title"]
        summary = item["summary"]
        policy_version = int(lease.schema_version or "1")

        # 幂等键 = principal + stream + sorted(event_ids) + policy_ver
        idempotency_key = _mk_idempotency(
            self._default_principal_id,
            stream_type,
            [lease.event_id],
            policy_version,
        )

        # 3. 检查是否已有候选已消费
        dup = conn.execute(
            "SELECT candidate_id FROM proactive_candidates WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if dup is not None:
            _LOGGER.info("candidate already projected: %s", dup["candidate_id"])
            # 仍写 Inbox succeeded 防重试
            _mark_consumed(conn, self.name, lease.event_id)
            return True

        # 4. 初始评分：relevance 由 MCP handler 算好，energy 后续注入
        relevance = float(item["relevance"] or 0.0)
        novelty = 0.5  # 占位；后续 embedding/time-window 增强
        urgency = relevance  # 无 energy 基准
        confidence = 0.7  # MCP 有稳定 id + schema 校验

        now = datetime.now(UTC)
        with conn:  # 单事务原子
            conn.execute(
                "INSERT INTO proactive_candidates "
                "(candidate_id, principal_id, stream_type, topic, summary, "
                " novelty, relevance, urgency, confidence, recommended_action, "
                " policy_version, idempotency_key, source_event_ids_json, "
                " source_payload_ref, expires_at_value, created_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    self._default_principal_id,
                    stream_type,
                    topic[:200],
                    _make_candidate_summary(title, summary),
                    novelty,
                    relevance,
                    urgency,
                    confidence,
                    "evaluate",  # 待 Decision Engine 处理
                    policy_version,
                    idempotency_key,
                    json.dumps([lease.event_id]),
                    lease.payload_ref,
                    None,  # 由 DefaultExpiry 决定
                    int(now.timestamp() * 1000),
                    "evaluating",
                ),
            )
            # Inbox 标记
            conn.execute(
                "INSERT INTO event_consumptions "
                "(consumer_name, event_id, status, attempts, processed_at) "
                "VALUES (?, ?, 'succeeded', 1, ?)",
                (self.name, lease.event_id, int(now.timestamp() * 1000)),
            )
        return True


class TurnCompletedMemoryExtractionConsumer(EventConsumer):
    """Project a committed TurnCompleted fact into one durable extraction Task."""

    name = "memory-extraction-scheduler"

    def can_handle(self, lease: OutboxLease) -> bool:
        return lease.event_type == "TurnCompleted"

    def handle(self, conn: sqlite3.Connection, lease: OutboxLease) -> bool:
        consumed = conn.execute(
            "SELECT 1 FROM event_consumptions WHERE consumer_name=? AND event_id=?",
            (self.name, lease.event_id),
        ).fetchone()
        if consumed is not None:
            return True

        try:
            payload = json.loads(lease.payload_ref or "{}")
        except json.JSONDecodeError:
            payload = {}
        turn_id = str(payload.get("turn_id") or lease.aggregate_id)
        row = conn.execute(
            "SELECT t.session_id, m.conversation_id, m.sender_principal_id "
            "FROM turns t JOIN messages m ON m.message_id=t.input_message_id "
            "WHERE t.turn_id=?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return False

        session_id = str(payload.get("session_id") or row["session_id"] or "")
        conversation_id = str(payload.get("conversation_id") or row["conversation_id"] or "")
        principal_id = str(payload.get("principal_id") or row["sender_principal_id"] or "")
        if not session_id or not principal_id:
            with conn:
                _mark_consumed(conn, self.name, lease.event_id)
            return True

        from cogito.service.memory_extractor import EXTRACTOR_VERSION, ExtractionTriggerPolicy
        from cogito.service.task_handlers import make_idempotency_key
        from cogito.service.task_service import SqliteTaskService
        from cogito.store.watermark_repo import PROC_MEMORY_EXTRACT, WatermarkRepository

        watermark = WatermarkRepository(conn).get(
            PROC_MEMORY_EXTRACT, conversation_id, session_id,
        )
        from_seq = (watermark.processed_upto_sequence + 1) if watermark else 1
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(receive_sequence), 0) AS upto, COUNT(*) AS n "
            "FROM messages WHERE session_id=? AND receive_sequence>=?",
            (session_id, from_seq),
        ).fetchone()
        to_seq = int(seq_row["upto"] or 0)
        new_count = int(seq_row["n"] or 0)

        with conn:
            if to_seq >= from_seq and ExtractionTriggerPolicy().should_trigger(
                trigger_type="turn_completed", new_message_count=new_count,
            ):
                task_payload = {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "principal_id": principal_id,
                    "from_sequence": from_seq,
                    "to_sequence": to_seq,
                    "input_version": 0,
                    "prompt_version": EXTRACTOR_VERSION,
                    "model_role": "memory_extractor",
                }
                key = make_idempotency_key(
                    "memory.extract", conversation_id, session_id,
                    from_seq, to_seq, EXTRACTOR_VERSION,
                )
                try:
                    SqliteTaskService(conn).create(
                        "memory.extract",
                        json.dumps(task_payload, ensure_ascii=False),
                        idempotency_key=key,
                        origin="turn_completed",
                        priority=40,
                        retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
                    )
                except sqlite3.IntegrityError:
                    pass
            _mark_consumed(conn, self.name, lease.event_id)
        return True




def _mk_idempotency(principal: str, stream_type: str, event_ids: list[str],
                    policy_ver: int) -> str:
    import hashlib
    ids = sorted(set(event_ids))
    raw = f"{principal}|{stream_type}|{'|'.join(ids)}|{policy_ver}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_candidate_summary(title: str, body: str) -> str:
    return f"{title[:100]}: {body[:300]}"


def _mark_consumed(conn: sqlite3.Connection, consumer_name: str, event_id: str) -> None:
    import time
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT OR REPLACE INTO event_consumptions "
        "(consumer_name, event_id, status, attempts, processed_at) "
        "VALUES (?, ?, 'succeeded', 0, ?)",
        (consumer_name, event_id, now_ms),
    )


def build_default_registry(default_principal_id: str = "owner") -> EventConsumerRegistry:
    """构造默认注册表（首期仅一个 consumer）。"""
    registry = EventConsumerRegistry()
    registry.register(SourceEventIngestedConsumer(
        default_principal_id=default_principal_id,
    ))
    registry.register(TurnCompletedMemoryExtractionConsumer())
    return registry
