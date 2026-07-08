"""Proactive Delivery 闭环 —— send_later → ScheduledDeliveryRequest → Delivery。

M8: 把 proactive_decisions_v2.action='send_later' 的决策转化为持久请求，
到 scheduled_at 时由 proactive.delivery.ready Task 重新校验 Policy 后创建
Delivery (TargetSnapshot 固定 per ACCESS-DELIVERY §4.2)。

本期单 Owner、单 Endpoint（默认 preferred_endpoint_id），无法获取
用户偏好 TargetSnapshot 时放行由 DeliveryWorker+Gateway 按
suggested_target_json 决定。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Any

from cogito.domain.delivery import Delivery, DeliveryStatus
from cogito.service.delivery_service import DeliveryRequest, DeliveryService
from cogito.store.proactive_repo import ProactiveDecision, ProactivePolicyRepository
from cogito.store.repositories import DeliveryRepository

_LOGGER = logging.getLogger(__name__)


class SqliteDeliveryService(DeliveryService):
    """DeliveryService 的 SQLite 实现 —— 非流式 pending delivery。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def enqueue(self, request: DeliveryRequest) -> Any:
        conn = self._conn
        conn.row_factory = sqlite3.Row
        delivery = Delivery(
            delivery_id=f"dev-{uuid.uuid4().hex[:16]}",
            target_snapshot=request.target,
            content_ref=request.content_ref,
            status=DeliveryStatus.pending,
            idempotency_key=request.idempotency_key or "",
            created_at=None,
        )
        repo = DeliveryRepository(conn)
        repo.insert(delivery)
        conn.commit()
        return delivery.delivery_id

    async def cancel(self, delivery_id: str) -> None:
        self._conn.execute(
            "UPDATE deliveries SET status='cancelled' WHERE delivery_id=?",
            (delivery_id,),
        )
        self._conn.commit()

    async def retry(self, delivery_id: str) -> None:
        self._conn.execute(
            "UPDATE deliveries SET status='pending', next_attempt_at=NULL "
            "WHERE delivery_id=?",
            (delivery_id,),
        )
        self._conn.commit()


def create_scheduled_request(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    content_ref: str,
    suggested_target: dict[str, Any],
    reason: str,
    scheduled_at_ms: int,
    policy_version: int = 1,
    expires_at_ms: int | None = None,
) -> str:
    """从 send_later 决策创建 scheduled_delivery_request。"""
    req_id = f"sdr-{uuid.uuid4().hex[:16]}"
    now = int(time.time() * 1000)
    idem = f"proactive-send-later:{candidate_id}:{scheduled_at_ms}"
    conn.execute(
        "INSERT INTO scheduled_delivery_requests "
        "(request_id, principal_id, candidate_id, content_ref, suggested_target_json, "
        " reason, status, scheduled_at, expires_at, policy_version, idempotency_key, "
        " created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            req_id, "owner", candidate_id, content_ref,
            json.dumps(suggested_target, ensure_ascii=False),
            reason[:500],
            "pending",
            scheduled_at_ms,
            expires_at_ms,
            policy_version,
            idem,
            now,
        ),
    )
    conn.commit()
    return req_id


def prepare_delivery_from_request(
    conn: sqlite3.Connection,
    request_id: str,
) -> dict[str, Any] | None:
    """从 scheduled_request 准备一个 DeliveryRequest；重新校验 Policy。

    {
      "content_ref": ...,
      "suggested_target": {...},
      "candidate_id": ...,
    }
    None 表示请求已被取消/过期（不应继续）。
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM scheduled_delivery_requests WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    status = row["status"]
    now = int(time.time() * 1000)
    # 校验是否仍 pending 且未过期
    if status != "pending":
        return None
    if row["expires_at"] is not None and row["expires_at"] < now:
        conn.execute(
            "UPDATE scheduled_delivery_requests SET status='expired' WHERE request_id=?",
            (request_id,),
        )
        conn.commit()
        return None
    # scheduled_at 必须已到
    if row["scheduled_at"] > now:
        return None
    # 校验最新 policy (仍默认允许；未来版本化)
    # 简化：不做deny/policy重判（本版本schedule时已经判过）
    return {
        "content_ref": row["content_ref"],
        "suggested_target": json.loads(row["suggested_target_json"] or "{}"),
        "candidate_id": row["candidate_id"],
        "principal_id": row["principal_id"],
    }


def mark_request_converted(
    conn: sqlite3.Connection,
    request_id: str,
    delivery_id: str,
) -> None:
    now = int(time.time() * 1000)
    conn.execute(
        "UPDATE scheduled_delivery_requests "
        "SET status='converted', converted_at=? WHERE request_id=?",
        (now, request_id),
    )
    conn.commit()


def mark_request_expired(conn: sqlite3.Connection, request_id: str) -> None:
    conn.execute(
        "UPDATE scheduled_delivery_requests SET status='expired' WHERE request_id=?",
        (request_id,),
    )
    conn.commit()


def find_due_requests(conn: sqlite3.Connection, *, limit: int = 10) -> list[str]:
    now = int(time.time() * 1000)
    rows = conn.execute(
        "SELECT request_id FROM scheduled_delivery_requests "
        "WHERE status='pending' AND scheduled_at <= ? AND "
        "      (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY scheduled_at ASC LIMIT ?",
        (now, now, limit),
    ).fetchall()
    return [r[0] for r in rows]
