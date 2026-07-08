"""Proactive Digest 服务 —— M9。

Digest 分桶 key: principal_id + digest_date + topic （ACCESS-DELIVERY §4.2、PROACTIVE-IDLE §6）。
 Candidate 消费后进入 Digest 桶；`proactive.digest.publish` Task 在到期时
封桶 → 渲染 → 走 DeliveryService 发送。

本期实现：确定性模板渲染（按 relevance 倒序 + topic 分桶）。
模型 polishing 占位（按设计可选）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import UTC, datetime

from cogito.domain.digest import Digest, DigestStatus

_LOGGER = logging.getLogger(__name__)


def assemble_and_render(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    digest_date: str,       # YYYY-MM-DD
    topic: str = "general",
    model_router=None,      # 可选，本版本未用
) -> tuple[str, str] | None:
    """把 status='digest' 且未消费的 connector_items 加入/创建 digest 桶，
    渲染 markdown 文本。返回 (digest_id, content_text) 或 None。"""
    conn.row_factory = sqlite3.Row
    # 1. 查找该 (principal, date, topic) 桶
    from cogito.store.digest_repo import DigestRepository
    repo = DigestRepository(conn)
    existing = repo.find_by_date_topic(principal_id, digest_date, topic)

    # 2. 计算时间窗口
    day_start = datetime.strptime(digest_date, "%Y-%m-%d").replace(tzinfo=UTC)
    day_start_ms = int(day_start.timestamp() * 1000)
    day_end_ms = day_start_ms + 86400 * 1000

    # 3. 取 items
    rows = conn.execute(
        "SELECT item_id, title, summary_text, summary, relevance, source_item_id "
        "FROM connector_items "
        "WHERE connector_id IN (SELECT connector_id FROM connectors WHERE status='active') "
        "  AND status='digest' "
        "  AND created_at BETWEEN ? AND ? "
        "ORDER BY relevance DESC NULLS LAST, created_at DESC",
        (day_start_ms, day_end_ms),
    ).fetchall()

    # 按 topic 过滤（简单 json topic 字段匹配）
    items = []
    for r in rows:
        if topic != "general":
            # 直接从 topic 列 (M4 已把 topic 写进 topic 列)
            trow = conn.execute(
                "SELECT topic_json FROM connector_items WHERE item_id=?",
                (r["item_id"],),
            ).fetchone()
            item_topic = "general"
            if trow and trow[0]:
                try:
                    meta = json.loads(trow[0])
                    item_topic = meta.get("category", "general")
                except Exception:
                    item_topic = "general"
            if item_topic != topic:
                continue
        items.append(r)

    if not items:
        return None

    # 4. 创建或取 digest
    if existing is None:
        digest_id = f"dig-{uuid.uuid4().hex[:16]}"
        d = Digest(
            digest_id=digest_id,
            principal_id=principal_id,
            digest_date=digest_date,
            status=DigestStatus.pending,
            item_count=len(items),
        )
        conn.execute(
            "INSERT INTO digests (digest_id, principal_id, digest_date, status, "
            "item_count, created_at, topic) VALUES (?,?,?,?,?,?,?)",
            (d.digest_id, d.principal_id, d.digest_date, d.status.value,
             d.item_count, int(time.time() * 1000), topic),
        )
    else:
        digest_id = existing.digest_id

    # 5. add items (幂等)
    for it in items:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO digest_items (digest_id, item_id) VALUES (?,?)",
                (digest_id, it["item_id"]),
            )
        except Exception:
            pass
    conn.commit()

    # 6. 渲染 markdown（确定性模板）
    return digest_id, render_digest_markdown(
        conn, digest_id, principal_id, digest_date, topic,
    )


def render_digest_markdown(
    conn: sqlite3.Connection,
    digest_id: str,
    principal_id: str,
    digest_date: str,
    topic: str,
) -> str:
    """白名单模板渲染：title + summary_text，按 relevance 倒序。"""
    items = conn.execute(
        "SELECT ci.title, ci.summary_text, ci.summary, ci.source_item_id "
        "FROM digest_items di "
        "JOIN connector_items ci ON ci.item_id = di.item_id "
        "WHERE di.digest_id=? "
        "ORDER BY ci.relevance DESC NULLS LAST, ci.created_at DESC",
        (digest_id,),
    ).fetchall()
    title = f"AI 动态 {digest_date} [{topic}]"
    lines = [
        f"# {title}",
        "",
        f"_由 Cogito 自动生成 / {len(items)}_条_",
        "",
    ]
    for i, it in enumerate(items, 1):
        head = it["title"] or it["source_item_id"] or "(untitled)"
        summary_text = it["summary_text"] or it["summary"] or ""
        lines.append(f"## {i}. {head}")
        if summary_text:
            lines.append("")
            lines.append(summary_text[:500])
        lines.append("")
    return "\n".join(lines)


def enqueue_digest_publish(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    digest_date: str,
    topic: str = "general",
    delay_minutes: int = 360,
) -> str:
    """创建 proactive.digest.publish Task，到期时消费 items 进 digest 桶。"""
    import uuid

    from cogito.domain.task import Task, TaskStatus
    from cogito.store.task_repo import TaskRepository
    payload = f"{principal_id}|{digest_date}|{topic}"
    task = Task(
        task_id=f"task-pdp-{uuid.uuid4().hex[:16]}",
        task_type="proactive.digest.publish",
        payload_ref=payload,
        status=TaskStatus.queued,
        priority=20,
        scheduled_at=int(time.time() * 1000) + int(delay_minutes) * 60 * 1000,
        idempotency_key=f"pdp:{payload}",
        origin="proactive-engine",
    )
    TaskRepository(conn).insert(task)
    conn.commit()
    return task.task_id


def mark_digest_sent(conn: sqlite3.Connection, digest_id: str) -> None:
    """标记 digest='sent' 并把关联的 connector_items 更新 status='sent'。"""
    conn.execute(
        "UPDATE digests SET status='_sent', rendered_at=? WHERE digest_id=?",
        (int(time.time() * 1000), digest_id),
    )
    items = conn.execute(
        "SELECT item_id FROM digest_items WHERE digest_id=?", (digest_id,),
    ).fetchall()
    for it in items:
        conn.execute(
            "UPDATE connector_items SET status='sent' WHERE item_id=?",
            (it[0],),
        )
    conn.commit()
