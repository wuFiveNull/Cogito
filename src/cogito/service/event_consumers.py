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
from typing import Any

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
            "SELECT status FROM event_consumptions WHERE consumer_name=? AND event_id=?",
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
            _LOGGER.warning("SourceEventIngested consumer: item %s not found", lease.payload_ref)
            return False

        # 候选只对 status='digest'（已决定进摘要）产生
        if item["status"] != "digest":
            _LOGGER.info(
                "SourceEventIngested consumer: item status=%s, no candidate", item["status"]
            )
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
        # MCP 摄取已用 source_id + content_hash 做精确去重；进入此处即为 exact-new。
        # 语义 novelty 后续可再降分，但不能用低于默认阈值的占位值阻断全量候选。
        novelty = 1.0
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
            # 同一 ingestion batch 只创建一个立即评估 Task。Outbox 在 Task 前排水，
            # 因此本批已投影 Candidate 会先全部落库，再由 bounded handler 评估。
            trigger_id = lease.correlation_id or lease.event_id
            eval_idempotency = f"proactive-evaluate-source:{trigger_id}"
            exists = conn.execute(
                "SELECT 1 FROM tasks WHERE idempotency_key=?",
                (eval_idempotency,),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO tasks "
                    "(task_id, task_type, payload_ref, status, priority, "
                    "idempotency_key, origin, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        f"task-pe-src-{uuid.uuid4().hex[:16]}",
                        "proactive.evaluate",
                        trigger_id,
                        "queued",
                        15,
                        eval_idempotency,
                        "source-event-immediate-eval",
                        int(now.timestamp() * 1000),
                    ),
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

        with conn:
            from cogito.service.memory_extractor import request_extraction

            request_extraction(
                conn,
                conversation_id=conversation_id,
                session_id=session_id,
                principal_id=principal_id,
                trigger_type="turn_completed",
                priority=40,
            )
            _mark_consumed(conn, self.name, lease.event_id)
        return True


class SessionCompletedMemoryExtractionConsumer(EventConsumer):
    """Project a committed SessionCompleted fact into one durable extraction Task.

    session_closed 触发：会话结束（归档/删除）时，把该 session 剩余未提取
    窗口作为一次性高优先级 extraction Task 提交，确保关闭前内容不丢失。
    """

    name = "session-extraction-scheduler"

    def can_handle(self, lease: OutboxLease) -> bool:
        return lease.event_type == "SessionCompleted"

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
        session_id = str(payload.get("session_id") or lease.aggregate_id)
        conversation_id = str(payload.get("conversation_id") or "")
        principal_id = str(payload.get("principal_id") or "")
        if not session_id or not principal_id:
            with conn:
                _mark_consumed(conn, self.name, lease.event_id)
            return True

        with conn:
            from cogito.service.memory_extractor import request_extraction

            request_extraction(
                conn,
                conversation_id=conversation_id,
                session_id=session_id,
                principal_id=principal_id,
                trigger_type="session_closed",
                priority=80,
            )
            _mark_consumed(conn, self.name, lease.event_id)
        return True


class MemorySourceInvalidatedConsumer(EventConsumer):
    """Knowledge 来源失效 → 经 MemoryService 决定 keep/review/expire（PLAN-16 M5 KNOW-07）。

    KnowledgeService.erase 仅发布 MemorySourceInvalidated 事件，不再直写 memory 表；
    本消费者调用 MemoryService.handle_memory_source_invalidated 完成跨聚合传播。
    """

    name = "memory-source-invalidation-handler"

    def can_handle(self, lease: OutboxLease) -> bool:
        return lease.event_type == "MemorySourceInvalidated"

    def handle(self, conn: sqlite3.Connection, lease: OutboxLease) -> bool:
        """处理 Knowledge→Memory 失效传播（PLAN-16 完整 schema）。

        统一事件 schema：必要字段 resource_id + memory_id（由 KnowledgeService.erase
        发布）。缺必要字段 → 返回 False（事件标记失败重试），而非静默标成功。
        """
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
        memory_id = str(payload.get("memory_id") or "").strip()
        resource_id = str(payload.get("resource_id") or "").strip()
        reason = str(payload.get("reason") or "knowledge_deleted")
        # 完整 schema 校验：缺必要字段返回 False（事件会 retry/入 dead-letter）
        if not memory_id or not resource_id:
            _LOGGER.warning(
                "MemorySourceInvalidated missing required fields "
                "(memory_id=%r, resource_id=%r), failing event for retry",
                memory_id,
                resource_id,
            )
            return False

        with conn:
            from cogito.service.memory_service import SqliteMemoryService

            SqliteMemoryService(conn).handle_memory_source_invalidated(
                memory_id,
                source_resource_id=resource_id,
                reason=reason,
            )
            _mark_consumed(conn, self.name, lease.event_id)
        return True


def _mk_idempotency(principal: str, stream_type: str, event_ids: list[str], policy_ver: int) -> str:
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


class InboundImmediateEvalConsumer(EventConsumer):
    """PLAN-17 R6 PA-P1-02: 入站 Turn 到达时立即 (不走 cadence 节流) 创建
    幂等 proactive.evaluate Task — 让 InboundMessageAccepted 事件激活一次主动评估。

    Scheduler 注释提到 schedule_immediate_evaluate 但源码不存在；本 Consumer 是
    其实生产实现。幂等键基于 turn_id + 日切窗口避免同一天对同 turn 重复触发;
    若 day 内已有同类任务则静默跳过 (ACK-like window)。
    """

    name = "inbound-immediate-eval"

    def __init__(self, *, default_principal_id: str = "owner") -> None:
        self._default_principal_id = default_principal_id

    def can_handle(self, lease: OutboxLease) -> bool:
        return lease.event_type == "InboundMessageAccepted"

    def handle(self, conn: sqlite3.Connection, lease: OutboxLease) -> bool:
        if (
            conn.execute(
                "SELECT 1 FROM event_consumptions WHERE consumer_name=? AND event_id=?",
                (self.name, lease.event_id),
            ).fetchone()
            is not None
        ):
            return True

        import time as _time

        # 取 turn_id (aggregate_id)
        turn_id = lease.aggregate_id

        # 幂等: anchor on turn + day window — 同 turn 当天只 1 次
        day_window = int(_time.time() * 1000) // 86400000
        idempotency = f"proactive-evaluate-immediate:{turn_id}:{day_window}"
        existing = conn.execute(
            "SELECT task_id FROM tasks WHERE idempotency_key=?",
            (idempotency,),
        ).fetchone()
        if existing is not None:
            with conn:
                _mark_consumed(conn, self.name, lease.event_id)
            return True

        try:
            from cogito.domain.task import Task, TaskStatus

            task = Task(
                task_id=f"task-pe-imm-{uuid.uuid4().hex[:16]}",
                task_type="proactive.evaluate",
                payload_ref="",
                status=TaskStatus.queued,
                priority=15,
                idempotency_key=idempotency,
                origin="inbound-immediate-eval",
            )
            conn.execute(
                "INSERT INTO tasks "
                "(task_id, task_type, status, priority, "
                " idempotency_key, origin, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    task.task_id,
                    task.task_type,
                    task.status.value,
                    task.priority,
                    task.idempotency_key,
                    task.origin,
                    int(_time.time() * 1000),
                ),
            )
            with conn:
                _mark_consumed(conn, self.name, lease.event_id)
            return True
        except Exception:
            import sys
            import traceback as _tb

            print("CONSUMER INSERT ERR:", file=sys.stderr)
            _tb.print_exc()
            return False


class DriftResultCommittedConsumer(EventConsumer):
    """PLAN-17 R5 P0-06: Drift 完成后自动投影为 ProactiveCandidate。

    校验：DriftRun completed；Principal 一致；manifest.can_emit_candidate；
    config.allow_candidate_projection；evidence/trust；同 Run 最多一个用户可见
    Candidate。dry_run 仅保存 preview/result，不写真实 Candidate。投影成功后回写
    DriftRun.candidate_id。Drift 不直接调用 DeliveryService。
    """

    name = "drift-result-projector"

    def __init__(self, *, default_principal_id: str = "owner", drift_config: Any = None) -> None:
        self._default_principal_id = default_principal_id
        self._drift_config = drift_config

    def can_handle(self, lease: OutboxLease) -> bool:
        return lease.event_type == "DriftResultCommitted"

    def handle(self, conn: sqlite3.Connection, lease: OutboxLease) -> bool:
        # 幂等：event_consumptions 表 (consumer_name, event_id) 唯一键
        if (
            conn.execute(
                "SELECT 1 FROM event_consumptions WHERE consumer_name=? AND event_id=?",
                (self.name, lease.event_id),
            ).fetchone()
            is not None
        ):
            return True
        # payload_ref 直接存 drift_run_id (str)
        drift_run_id = (lease.payload_ref or "").strip()

        run = conn.execute(
            "SELECT principal_id, status, skill_name FROM drift_runs WHERE drift_run_id=?",
            (drift_run_id,),
        ).fetchone()
        if run is None:
            return False
        if run["status"] != "completed":
            _LOGGER.info(
                "DriftResultCommitted: run %s not completed (status=%s); skip",
                drift_run_id,
                run["status"],
            )
            return True
        principal_id = run["principal_id"] or self._default_principal_id

        # 校验 config 拒绝投影 (dry-run / allow_candidate_projection / allow_candidate_emission)
        allow = True
        if self._drift_config is not None:
            dry = bool(getattr(self._drift_config, "dry_run", False))
            proj = bool(getattr(self._drift_config, "allow_candidate_projection", False))
            emit = bool(getattr(self._drift_config, "allow_candidate_emission", False))
            allow = (not dry) and proj and emit
        if not allow:
            _LOGGER.info(
                "[drift_project] skip projection: dry=%s projection=%s emission=%s run=%s",
                dry if self._drift_config is not None else None,
                proj if self._drift_config is not None else None,
                emit if self._drift_config is not None else None,
                drift_run_id,
            )
            # 消费 Outbox event 防止重试 (审计证据 #6: dry_run 只保存 preview/result)
            with conn:
                _mark_consumed(conn, self.name, lease.event_id)
            return True

        # 每 run 最多一个用户可见 Candidate (由 DriftProjectionService 双重校验)
        from cogito.store.drift_result_repo import DriftResultRepository

        drr = DriftResultRepository(conn).latest_for_run(drift_run_id)
        draft_payload = (drr.candidate_draft if drr else None) or {}
        if not draft_payload:
            with conn:
                _mark_consumed(conn, self.name, lease.event_id)
            return True
        from cogito.domain.drift import DriftCandidateDraft

        try:
            draft = DriftCandidateDraft(
                topic=str(draft_payload.get("topic", "drift.result")),
                summary=str(draft_payload.get("summary", "")),
                evidence_refs=tuple(draft_payload.get("evidence_refs", ())),
                trust_label=str(draft_payload.get("trust_label", "system_generated")),
                urgency=float(draft_payload.get("urgency", 0.5)),
                confidence=float(draft_payload.get("confidence", 0.5)),
                relevance=float(draft_payload.get("relevance", 0.6)),
                expires_at=draft_payload.get("expires_at"),
            )
        except Exception:
            _LOGGER.warning("invalid candidate draft for run %s", drift_run_id, exc_info=True)
            with conn:
                _mark_consumed(conn, self.name, lease.event_id)
            return True

        dry_run = bool(self._drift_config and getattr(self._drift_config, "dry_run", False))
        from cogito.service.drift_projection import DriftProjectionService

        svc = DriftProjectionService(conn, dry_run=dry_run)
        try:
            candidate_id = svc.project(
                drift_run_id=drift_run_id, draft=draft, principal_id=principal_id
            )
        except Exception:
            import traceback as _tb

            _LOGGER.warning("drift projection failed for run %s", drift_run_id, exc_info=True)
            _tb.print_exc()
            return False  # 触发 Outbox retry

        if candidate_id:
            # 回写 candidate_id on DriftRun + DriftResult
            conn.execute(
                "UPDATE drift_runs SET candidate_id=? WHERE drift_run_id=?",
                (candidate_id, drift_run_id),
            )
            if drr is not None:
                DriftResultRepository(conn).mark_emitted(drr.drift_result_id, candidate_id)
            _LOGGER.info("drift projected: run=%s candidate=%s", drift_run_id, candidate_id)

        with conn:
            _mark_consumed(conn, self.name, lease.event_id)
        return True


def build_default_registry(
    default_principal_id: str = "owner", drift_config: Any = None
) -> EventConsumerRegistry:
    """构造默认注册表。

    PLAN-17 R5 P0-06: DriftResultCommittedConsumer 注册；dry_run 时只保存 preview。
    """
    registry = EventConsumerRegistry()
    registry.register(
        SourceEventIngestedConsumer(
            default_principal_id=default_principal_id,
        )
    )
    registry.register(TurnCompletedMemoryExtractionConsumer())
    registry.register(SessionCompletedMemoryExtractionConsumer())
    registry.register(MemorySourceInvalidatedConsumer())
    registry.register(InboundImmediateEvalConsumer(default_principal_id=default_principal_id))
    registry.register(
        DriftResultCommittedConsumer(
            default_principal_id=default_principal_id, drift_config=drift_config
        )
    )
    return registry
