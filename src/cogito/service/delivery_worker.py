"""Delivery Worker — 领取 pending/retry_scheduled Delivery、通过 Gateway 发送、重试和 Reconcile。

Fake Gateway 提供测试用的假发送通道。

ACCESS-DELIVERY / 4.3 Delivery 状态
pending → sending → sent
              ├→ retry_scheduled
              ├→ failed
              └→ unknown → sent (reconcile)

可靠性语义：
- lease_next 仅领取 pending 或已到期的 retry_scheduled
- 条件更新验证 lease_owner + lease_version
- lease_expires_at = now + TTL（不等于 now）
- 外部结果明确失败时按策略重试
- 外部结果 unknown 时只能 reconcile，不能自动重试
- 达到 MAX_TENTATIVE 后精确进入 failed
- 外部调用前后使用同一 Clock 读取时间，确保第二次校验反映调用期间时间变化
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import NamedTuple, Protocol

from cogito.contracts.clock import Clock, ProductionClock
from cogito.service.unit_of_work import UnitOfWork
from cogito.contracts.clock import epoch_ms

# ── Gateway Protocol ──


class Gateway(Protocol):
    """消息发送通道协议。"""

    def send(self, target: str, content_ref: str) -> bool | None:
        """发送消息。返回 True=成功, False=失败, None=未知。"""
        ...


class FakeGateway:
    """Fake 发送通道 —— 记录发送请求，可配置成功/失败。"""

    def __init__(self, *, fail: bool = False, unknown: bool = False) -> None:
        self._fail = fail
        self._unknown = unknown
        self.sent: list[tuple[str, str]] = []  # (target, content_ref)

    def send(self, target: str, content_ref: str) -> bool | None:
        if self._unknown:
            return None
        if self._fail:
            return False
        self.sent.append((target, content_ref))
        return True

    def reset(self) -> None:
        self.sent.clear()
        self._fail = False
        self._unknown = False


# ── 重试策略 ──

MAX_DELIVERY_TENTATIVE = 3
DELIVERY_BACKOFF_BASE = 10
DELIVERY_BACKOFF_MULTIPLIER = 3
DELIVERY_MAX_BACKOFF = 3600

# ── Lease TTL ──

DELIVERY_LEASE_TTL_S = 120


class DeliveryLease(NamedTuple):
    delivery_id: str
    target_snapshot: str
    content_ref: str | None
    idempotency_key: str
    created_at: str
    lease_version: int
    attempt_count: int


def compute_delivery_backoff(attempt_count: int) -> float:
    delay = DELIVERY_BACKOFF_BASE * (DELIVERY_BACKOFF_MULTIPLIER ** (attempt_count - 1))
    return min(delay, DELIVERY_MAX_BACKOFF)


class DeliveryWorker:
    """投递 Worker。"""

    def __init__(self, conn: sqlite3.Connection, gateway: Gateway,
                 lease_ttl_s: int = DELIVERY_LEASE_TTL_S,
                 clock: Clock | None = None) -> None:
        self._conn = conn
        self._gateway = gateway
        self._lease_ttl_s = lease_ttl_s
        self._clock = clock or ProductionClock()

    def _now(self, override: datetime | None = None) -> datetime:
        """返回当前时间：优先使用方法级 override，否则使用 Clock。"""
        return override if override is not None else self._clock.now()

    def lease_next(self, worker_id: str, clock: datetime | None = None) -> DeliveryLease | None:
        """领取下一个待投递的 Delivery。

        可领取：pending 或已到期的 retry_scheduled。
        lease_expires_at = now + TTL。
        """
        now = self._now(clock)
        now_int = epoch_ms(now)
        lease_expires = now_int + self._lease_ttl_s * 1000

        with UnitOfWork(self._conn) as uow:
            row = self._conn.execute("""
                SELECT * FROM deliveries
                WHERE (
                    status = 'pending'
                    OR (
                        status = 'retry_scheduled'
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    )
                )
                ORDER BY created_at ASC
                LIMIT 1
            """, (now_int,)).fetchone()

            if row is None:
                return None

            old_version = row["lease_version"]
            new_attempt_count = row["attempt_count"] + 1

            updated = self._conn.execute(
                "UPDATE deliveries SET status='sending', lease_owner=?, "
                "lease_version=lease_version+1, attempt_count=?, lease_expires_at=? "
                "WHERE delivery_id=? AND status=? AND lease_version=?",
                (worker_id, new_attempt_count, lease_expires,
                 row["delivery_id"], row["status"], old_version),
            )
            if updated.rowcount == 0:
                return None

            attempt_id = uuid.uuid4().hex
            attempt_no = self._next_attempt_no(row["delivery_id"])

            self._conn.execute(
                "INSERT INTO delivery_attempts (attempt_id, delivery_id, attempt_no, status, "
                "started_at, lease_owner, lease_version) "
                "VALUES (?, ?, ?, 'sending', ?, ?, ?)",
                (attempt_id, row["delivery_id"], attempt_no, now_int, worker_id, old_version + 1),
            )

            uow.commit()

            return DeliveryLease(
                delivery_id=row["delivery_id"],
                target_snapshot=row["target_snapshot"],
                content_ref=row["content_ref"],
                idempotency_key=row["idempotency_key"],
                created_at=row["created_at"],
                lease_version=old_version + 1,
                attempt_count=new_attempt_count,
            )

    def _request_hash(self, delivery_id: str, content_ref: str, lease_version: int) -> str:
        """计算请求的简短哈希。"""
        import hashlib
        raw = f"{delivery_id}:{content_ref}:{lease_version}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _write_receipt(self, uow: UnitOfWork, delivery_id: str, attempt_id: str,
                       request_hash: str, receipt_kind: str, platform_message_id: str | None,
                       safe_result: str | None, observed_at: int, lease_version: int) -> None:
        """写入 delivery_receipt 记录。"""
        import uuid
        self._conn.execute(
            "INSERT OR IGNORE INTO delivery_receipts "
            "(receipt_id, delivery_id, delivery_attempt_id, operation_seq, request_hash, "
            "receipt_kind, platform_message_id, safe_result, observed_at, lease_version) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, delivery_id, attempt_id, request_hash,
             receipt_kind, platform_message_id, safe_result, observed_at, lease_version),
        )

    def deliver(self, lease: DeliveryLease, worker_id: str, clock: datetime | None = None) -> str:
        """发送 Delivery。

        先验证 Lease 有效性再调用 Gateway，避免过期 Worker 产生重复副作用。

        流程：
        1. 短事务读取并验证当前执行权（status=sending, lease_owner, lease_version, 未过期）
        2. 若无效 → 返回 "stale"，不调用 Gateway
        3. Gateway 调用前读取一次时间（pre-call time）
        4. 事务外调用 Gateway（通过 to_thread + run_coroutine_threadsafe）
        5. 事务外调用 Gateway
        6. 使用 post-call time 再次验证 Lease 后提交结果
        7. 提交结果时写入对应 Receipt（confirmed/uncertain）
        """
        target = lease.target_snapshot
        content_ref = lease.content_ref or ""

        # ── 第一步：Gateway 调用前的时间（pre-call time）──
        pre_call = self._now(clock)
        pre_call_int = epoch_ms(pre_call)

        # ── 第二步：预验证 Lease（短事务，不持锁跨网络）──
        with UnitOfWork(self._conn):
            current = self._conn.execute(
                "SELECT status, lease_owner, lease_version, lease_expires_at "
                "FROM deliveries WHERE delivery_id=?",
                (lease.delivery_id,),
            ).fetchone()

        if current is None:
            return "stale"
        if current["status"] != "sending":
            return "stale"
        if current["lease_owner"] != worker_id:
            return "stale"
        if current["lease_version"] != lease.lease_version:
            return "stale"
        lease_expires = current["lease_expires_at"]
        if lease_expires is None or lease_expires <= pre_call_int:
            return "stale"  # NULL or expired — no Gateway call

        # ── 第三步：事务外调用 Gateway（结构化版本）──
        from cogito.channel.base import ChannelSendResult
        send_result: ChannelSendResult
        if hasattr(self._gateway, "send_request"):
            try:
                send_result = self._gateway.send_request(target, content_ref)
            except Exception:
                send_result = ChannelSendResult(status="unknown", error_code="gateway_exception")
        else:
            # 遗留 bool|None 兼容
            legacy_result = self._gateway.send(target, content_ref)
            if legacy_result is True:
                send_result = ChannelSendResult(
                    status="sent",
                    platform_message_id=f"fake_{lease.delivery_id[:8]}",
                )
            elif legacy_result is False:
                send_result = ChannelSendResult(status="temporary", error_code="legacy_false")
            else:
                send_result = ChannelSendResult(status="unknown", error_code="legacy_none")

        # ── 第四步：Gateway 返回后重新读取时间（post-call time）──
        post_call = self._now(clock)
        post_call_int = epoch_ms(post_call)

        # ── 计算请求哈希 ──
        req_hash = hashlib.sha256(
            f"{lease.delivery_id}:{content_ref}:{lease.lease_version}".encode()
        ).hexdigest()[:16]

        # ── 第五步：使用 post-call time 再次验证 Lease 后提交结果 ──
        with UnitOfWork(self._conn) as uow:
            current = self._conn.execute(
                "SELECT status, lease_owner, lease_version, lease_expires_at "
                "FROM deliveries WHERE delivery_id=?",
                (lease.delivery_id,),
            ).fetchone()

            attempt_row = self._conn.execute(
                "SELECT attempt_id FROM delivery_attempts "
                "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                (lease.delivery_id, worker_id, lease.lease_version),
            ).fetchone()
            attempt_id = attempt_row["attempt_id"] if attempt_row else ""

            # 使用 post_call_int 验证 Lease（可能已在 Gateway 调用期间过期或被 Recovery 推进）
            if current is None \
                    or current["status"] != "sending" \
                    or current["lease_owner"] != worker_id \
                    or current["lease_version"] != lease.lease_version \
                    or current["lease_expires_at"] is None \
                    or current["lease_expires_at"] <= post_call_int:
                # Gateway was called but we can't commit — enter unknown
                # 写 uncertain Receipt 作为持久证据
                safe_result = f"{send_result.status}:{send_result.error_code or ''}"
                self._write_receipt(uow, lease.delivery_id, attempt_id, req_hash,
                                    "uncertain", send_result.platform_message_id,
                                    safe_result, post_call_int, lease.lease_version)
                # 使用 delivery_id 主键直接更新（lease 可能已被 Recovery 推进版本）
                self._conn.execute(
                    "UPDATE deliveries SET status='unknown', lease_owner=NULL, "
                    "lease_expires_at=NULL, lease_version=lease_version+1 "
                    "WHERE delivery_id=? AND status='sending'",
                    (lease.delivery_id,),
                )
                uow.commit()
                return "unknown"

            platform_msg_id = send_result.platform_message_id

            if send_result.status == "sent":
                self._conn.execute(
                    "UPDATE deliveries SET status='sent', platform_message_id=? "
                    "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                    (platform_msg_id, lease.delivery_id, worker_id, lease.lease_version),
                )
                self._conn.execute(
                    "UPDATE delivery_attempts SET status='succeeded', finished_at=? "
                    "WHERE attempt_id=?",
                    (post_call_int, attempt_id),
                )
                # 写 confirmed Receipt
                self._write_receipt(uow, lease.delivery_id, attempt_id, req_hash,
                                    "confirmed", platform_msg_id, "ok",
                                    post_call_int, lease.lease_version)
                uow.commit()
                return "sent"

            if send_result.status == "permanent":
                self._conn.execute(
                    "UPDATE deliveries SET status='failed' "
                    "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                    (lease.delivery_id, worker_id, lease.lease_version),
                )
                self._conn.execute(
                    "UPDATE delivery_attempts SET status='failed', finished_at=? "
                    "WHERE attempt_id=?",
                    (post_call_int, attempt_id),
                )
                self._write_receipt(uow, lease.delivery_id, attempt_id, req_hash,
                                    "permanent", None, send_result.error_code or "permanent",
                                    post_call_int, lease.lease_version)
                uow.commit()
                return "failed"

            if send_result.status == "temporary":
                if lease.attempt_count >= MAX_DELIVERY_TENTATIVE:
                    self._conn.execute(
                        "UPDATE deliveries SET status='failed' "
                        "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                        (lease.delivery_id, worker_id, lease.lease_version),
                    )
                else:
                    delay = send_result.retry_after_seconds or compute_delivery_backoff(lease.attempt_count)
                    next_at = datetime.fromtimestamp(post_call.timestamp() + delay, tz=UTC)
                    next_at_int = epoch_ms(next_at)
                    self._conn.execute(
                        "UPDATE deliveries SET status='retry_scheduled', next_attempt_at=? "
                        "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                        (next_at_int, lease.delivery_id, worker_id, lease.lease_version),
                    )
                self._conn.execute(
                    "UPDATE delivery_attempts SET status='failed', finished_at=? "
                    "WHERE attempt_id=?",
                    (post_call_int, attempt_id),
                )
                self._write_receipt(uow, lease.delivery_id, attempt_id, req_hash,
                                    "temporary", None, send_result.error_code or "temporary",
                                    post_call_int, lease.lease_version)
                uow.commit()
                return "failed"  # temporary 失败本轮返回 failed，但实际进入 retry_scheduled

            # send_result.status == "unknown"
            self._conn.execute(
                "UPDATE deliveries SET status='unknown' "
                "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                (lease.delivery_id, worker_id, lease.lease_version),
            )
            self._conn.execute(
                "UPDATE delivery_attempts SET status='failed', finished_at=? "
                "WHERE attempt_id=?",
                (post_call_int, attempt_id),
            )
            # 写 uncertain Receipt
            self._write_receipt(uow, lease.delivery_id, attempt_id, req_hash,
                                "uncertain", None,
                                send_result.error_code or "unknown",
                                post_call_int, lease.lease_version)
            uow.commit()
            return "unknown"

    def reconcile(self, delivery_id: str, platform_message_id: str,
                  worker_id: str = "", clock: datetime | None = None) -> bool:
        """Reconcile 路径：unknown → sent。

        同时写入 reconciled Receipt 作为持久证据。
        已存在 confirmed Receipt 时不做回退（幂等）。
        """
        observed_at = epoch_ms(self._now(clock))

        with UnitOfWork(self._conn) as uow:
            if worker_id:
                updated = self._conn.execute(
                    "UPDATE deliveries SET status='sent', platform_message_id=? "
                    "WHERE delivery_id=? AND status='unknown' AND lease_owner=?",
                    (platform_message_id, delivery_id, worker_id),
                )
            else:
                updated = self._conn.execute(
                    "UPDATE deliveries SET status='sent', platform_message_id=? "
                    "WHERE delivery_id=? AND status='unknown'",
                    (platform_message_id, delivery_id),
                )

            if updated.rowcount > 0:
                # 查找最新的 attempt_id
                attempt_row = self._conn.execute(
                    "SELECT attempt_id FROM delivery_attempts "
                    "WHERE delivery_id=? ORDER BY attempt_no DESC LIMIT 1",
                    (delivery_id,),
                ).fetchone()
                attempt_id = attempt_row["attempt_id"] if attempt_row else ""

                # 计算下一个 operation_seq
                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(operation_seq), 0) + 1 FROM delivery_receipts WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                op_seq = seq_row[0]

                # 查找当前 lease_version
                lv_row = self._conn.execute(
                    "SELECT lease_version FROM deliveries WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                lease_ver = lv_row["lease_version"] if lv_row else 0

                self._conn.execute(
                    "INSERT OR IGNORE INTO delivery_receipts "
                    "(receipt_id, delivery_id, delivery_attempt_id, operation_seq, request_hash, "
                    "receipt_kind, platform_message_id, safe_result, observed_at, lease_version) "
                    "VALUES (?, ?, ?, ?, '', 'reconciled', ?, 'reconciled', ?, ?)",
                    (uuid.uuid4().hex, delivery_id, attempt_id, op_seq,
                     platform_message_id, observed_at, lease_ver),
                )

            uow.commit()
            return updated.rowcount > 0

    def _next_attempt_no(self, delivery_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM delivery_attempts WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        return row[0]
