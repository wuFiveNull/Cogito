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
    vision_service_factory: Callable[[], Any] | None = None
    memory_service_factory: Callable[[sqlite3.Connection], Any] | None = None
    knowledge_service_factory: Callable[[sqlite3.Connection], Any] | None = None
    workspace_path: str = ""
    logger: logging.Logger = field(default_factory=lambda: _LOGGER)
    # MCP 生命周期（由 application.run_worker 注入；连接器 poll 用）
    mcp_manager: Any = None  # MCPServerManager
    # 主动 Delivery 闭环（send_later → Delivery）
    delivery_service: Any = None  # DeliveryService 实现
    # 主动推送配置（来自 config.capability.proactive）
    proactive_config: Any = None  # ProactiveConfig
    # 用户活动读取（PresenceReader Port）；fail-safe 时返回 None
    presence_reader: Any = None  # PresenceReader
    # 决定时生效的配置版本（供 Decision 审计追溯）
    config_version_id: str = ""
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
    registry.register("memory.recompute_weight", _handle_memory_recompute_weight)
    registry.register("memory.consolidate", _handle_memory_consolidate)
    registry.register("knowledge.ingest", _handle_knowledge_ingest)
    registry.register("knowledge.embed", _handle_knowledge_embed)
    registry.register("knowledge.invalidate", _handle_knowledge_invalidate)
    registry.register("knowledge.rebuild_index", _handle_knowledge_rebuild_index)
    registry.register("knowledge.sync_source", _handle_knowledge_sync_source)
    registry.register("summary.generate", _handle_summary_generate)
    registry.register("connector.poll", _handle_connector_poll)
    registry.register("mcp_connector.poll", _handle_mcp_connector_poll)
    registry.register("proactive.delivery.ready", _handle_proactive_delivery_ready)
    registry.register("proactive.digest.publish", _handle_proactive_digest_publish)
    registry.register("proactive.evaluate", _handle_proactive_evaluate)
    registry.register("drift.run", _handle_drift_run)
    registry.register("vision.analyze", _handle_vision_analyze)
    return registry


def _knowledge_service(ctx: TaskHandlerContext, conn: sqlite3.Connection):
    if ctx.knowledge_service_factory:
        return ctx.knowledge_service_factory(conn)
    from cogito.service.knowledge.service import KnowledgeService
    return KnowledgeService(conn)


def _refresh_knowledge_views_task(ctx: TaskHandlerContext, conn: sqlite3.Connection) -> None:
    """Task handler 安全刷新 KNOWLEDGE.md 视图（失败不回滚事务）。"""
    if not ctx.workspace_path:
        return
    try:
        from cogito.service.knowledge_views import KnowledgeViewsGenerator
        KnowledgeViewsGenerator(conn, workspace_path=ctx.workspace_path).generate_all()
    except Exception as e:
        _LOGGER.warning("Knowledge view refresh failed (task %s): %s", ctx._task_id, e)


def _task_json(task: Task) -> dict[str, Any]:
    try:
        value = json.loads(task.payload_ref or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {task.task_type} payload") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid {task.task_type} payload")
    return value


def _handle_knowledge_ingest(task: Task, ctx: TaskHandlerContext) -> str:
    if not ctx.connection_factory:
        return "knowledge ingest (skipped: no connection_factory)"
    data = _task_json(task)
    conn = ctx.connection_factory()
    try:
        service = _knowledge_service(ctx, conn)
        resource_id = str(data.get("resource_id", ""))
        raw_text = str(data.get("raw_text", ""))
        if not resource_id or not raw_text:
            raise ValueError("knowledge.ingest requires resource_id and raw_text")
        document, segments = service.ingest(resource_id, raw_text)
        _refresh_knowledge_views_task(ctx, conn)
        return f"knowledge ingested: {document.document_id} ({len(segments)} segments)"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_knowledge_embed(task: Task, ctx: TaskHandlerContext) -> str:
    if not ctx.connection_factory:
        return "knowledge embed (skipped: no connection_factory)"
    conn = ctx.connection_factory()
    try:
        import asyncio
        count = asyncio.run(_knowledge_service(ctx, conn).embed_pending())
        return f"knowledge embedded: {count}"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_knowledge_invalidate(task: Task, ctx: TaskHandlerContext) -> str:
    if not ctx.connection_factory:
        return "knowledge invalidate (skipped: no connection_factory)"
    data = _task_json(task)
    conn = ctx.connection_factory()
    try:
        resource_id = str(data.get("resource_id", ""))
        if not resource_id:
            raise ValueError("knowledge.invalidate requires resource_id")
        count = _knowledge_service(ctx, conn).invalidate(resource_id, str(data.get("reason", "")))
        _refresh_knowledge_views_task(ctx, conn)
        return f"knowledge invalidated: {resource_id} ({count} segments)"
    finally:
        conn.close()


def _handle_knowledge_rebuild_index(task: Task, ctx: TaskHandlerContext) -> str:
    if not ctx.connection_factory:
        return "knowledge rebuild (skipped: no connection_factory)"
    conn = ctx.connection_factory()
    try:
        from cogito.service.knowledge.embedding import rebuild_index
        result = rebuild_index(conn, fts=True, embeddings=False)
        conn.commit()
        _refresh_knowledge_views_task(ctx, conn)
        return f"knowledge index rebuilt: {result}"
    finally:
        conn.close()


def _handle_knowledge_sync_source(task: Task, ctx: TaskHandlerContext) -> str:
    if not ctx.connection_factory:
        return "knowledge sync (skipped: no connection_factory)"
    data = _task_json(task)
    conn = ctx.connection_factory()
    try:
        from cogito.service.knowledge.sync import sync_resource
        resource_id = sync_resource(
            conn,
            stable_source_id=str(data.get("stable_source_id", "")),
            source_kind=str(data.get("source_kind", "connector")),
            content_hash=str(data.get("content_hash", "")),
            raw_text=str(data.get("raw_text", "")),
            principal_id=str(data.get("principal_id", "")),
            trust_label=str(data.get("trust_label", "unverified")),
        )
        _refresh_knowledge_views_task(ctx, conn)
        return f"knowledge synced: {resource_id}"
    finally:
        conn.close()


def _handle_drift_run(task: Task, ctx: TaskHandlerContext) -> str:
    """drift.run: 执行已选 Skill 只读维护动作 + finish_drift 强制收尾。

    委托 drift_runner.handle_drift_run (注册为 TaskHandler 以复用 TaskWorker/Lease)。
    """
    from cogito.service.drift_runner import handle_drift_run
    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "drift.run skipped: no connection"
    try:
        result = handle_drift_run(task, ctx)
        try:
            conn.close()
        except Exception:
            pass
        return result
    except Exception:
        _LOGGER.exception("drift.run failed")
        try:
            conn.close()
        except Exception:
            pass
        raise


def _handle_vision_analyze(task: Task, ctx: TaskHandlerContext) -> str:
    """Execute one durable vision analysis attempt through the shared cache service."""
    if ctx.vision_service_factory is None:
        raise RuntimeError("vision service is not configured")
    try:
        payload = json.loads(task.payload_ref or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("invalid vision.analyze payload") from exc
    analysis_id = str(payload.get("analysis_id", ""))
    if not analysis_id:
        raise ValueError("vision.analyze payload missing analysis_id")

    import asyncio

    service = ctx.vision_service_factory()
    result = asyncio.run(service.analyze(analysis_id))
    return f"vision analysis {result.analysis_id}: {result.status.value}"


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
        mark_request_converted,
        prepare_delivery_from_request,
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
    """Run one durable, idempotent memory extraction window."""
    payload = MemoryExtractionPayload.from_payload_ref(task.payload_ref or "{}")
    _LOGGER.info(
        "Task memory.extract: %s session=%s range=[%d..%d]",
        task.task_id, payload.session_id,
        payload.from_sequence, payload.to_sequence,
    )

    if not ctx.connection_factory:
        return "extracted (skipped: no connection_factory)"
    if ctx.model_router is None:
        raise RuntimeError("memory.extract model router is not configured")

    conn = ctx.connection_factory()
    try:
        conn.row_factory = sqlite3.Row
        from cogito.service.memory_extractor import ExtractMessage, MemoryExtractor
        from cogito.service.memory_service import SqliteMemoryService
        from cogito.store.memory_repo import MemoryRepository
        from cogito.store.watermark_repo import PROC_MEMORY_EXTRACT, WatermarkRepository

        wm_repo = WatermarkRepository(conn)
        wm = wm_repo.get(PROC_MEMORY_EXTRACT, payload.conversation_id, payload.session_id)
        if wm and wm.processed_upto_sequence >= payload.to_sequence:
            return "extracted (already processed)"

        rows = conn.execute(
            "SELECT m.message_id, m.role, m.receive_sequence, m.sender_principal_id, "
            "m.trust_label, cp.inline_data "
            "FROM messages m LEFT JOIN content_parts cp ON cp.message_id=m.message_id "
            "WHERE m.session_id=? AND m.receive_sequence BETWEEN ? AND ? "
            "ORDER BY m.receive_sequence, cp.ordinal, cp.part_id",
            (payload.session_id, payload.from_sequence, payload.to_sequence),
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = grouped.setdefault(row["message_id"], {
                "role": row["role"], "sequence": row["receive_sequence"],
                "principal": row["sender_principal_id"] or "",
                "trust": row["trust_label"] or "unverified", "parts": [],
            })
            if row["inline_data"]:
                value["parts"].append(row["inline_data"])
        messages = [
            ExtractMessage(
                message_id=mid, role=value["role"], content="\n".join(value["parts"]),
                receive_sequence=value["sequence"], sender_principal_id=value["principal"],
                trust_label=value["trust"],
            )
            for mid, value in grouped.items()
        ]
        messages.sort(key=lambda value: value.receive_sequence)

        service = (
            ctx.memory_service_factory(conn)
            if ctx.memory_service_factory else
            SqliteMemoryService(conn, MemoryRepository(conn))
        )
        extractor = MemoryExtractor(
            conn, service, ctx.model_router,
            model_role=payload.model_role, strict=True,
        )
        import asyncio
        written = asyncio.run(extractor.extract_from_messages(
            messages, principal_id=payload.principal_id,
            session_id=payload.session_id,
            from_sequence=payload.from_sequence, to_sequence=payload.to_sequence,
        ))

        # Zero candidates is still a successfully processed window.
        latest = wm_repo.get(PROC_MEMORY_EXTRACT, payload.conversation_id, payload.session_id)
        if latest is None:
            wm_repo.upsert(
                PROC_MEMORY_EXTRACT, payload.conversation_id, payload.session_id,
                input_version=payload.input_version,
            )
            latest = wm_repo.get(PROC_MEMORY_EXTRACT, payload.conversation_id, payload.session_id)
        if latest and latest.processed_upto_sequence < payload.to_sequence:
            ok = wm_repo.advance(
                PROC_MEMORY_EXTRACT, payload.conversation_id, payload.session_id,
                to_sequence=payload.to_sequence, input_version=payload.input_version,
                expected_from_sequence=latest.processed_upto_sequence,
                expected_version=latest.version,
            )
            if not ok:
                current = wm_repo.get(
                    PROC_MEMORY_EXTRACT, payload.conversation_id, payload.session_id,
                )
                if current is None or current.processed_upto_sequence < payload.to_sequence:
                    raise RuntimeError("memory.extract watermark CAS failed")
        conn.commit()
        return f"extracted: {len(written)} candidates (upto={payload.to_sequence})"
    except Exception:
        conn.rollback()
        _LOGGER.exception("memory.extract failed")
        raise
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


def _handle_memory_recompute_weight(task: Task, ctx: TaskHandlerContext) -> str:
    """Recompute cached weights from the versioned policy and append-only signals."""
    if not ctx.connection_factory:
        return "memory weights (skipped: no connection_factory)"
    conn = ctx.connection_factory()
    try:
        from cogito.store.memory_repo import MemoryRepository
        from cogito.store.signal_repo import SignalRepository
        from cogito.store.weight_policy import MemoryWeightPolicy

        count = MemoryRepository(conn).recompute_all_weights(
            now=datetime.now(UTC),
            policy=MemoryWeightPolicy(),
            signals_repo=SignalRepository(conn),
        )
        conn.commit()
        # PLAN-14 R-08: 权重重算完成
        try:
            from cogito.domain.events import DomainEvent
            from cogito.store.repositories import OutboxRepository
            OutboxRepository(conn).insert(DomainEvent(
                event_type="MemoryWeightRecomputed",
                aggregate_type="memory_weight",
                aggregate_id=f"recompute-{ctx._task_id}",
                aggregate_version=1,
                payload={"recomputed_count": count, "task_id": ctx._task_id},
                payload_ref=__import__("json").dumps({"recomputed_count": count}),
                origin="memory_recompute_weight_handler",
            ))
        except Exception:
            pass
        return f"memory weights recomputed: {count}"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    """Run canonical maintenance without the retired direct-delete scoring path."""
    if not ctx.connection_factory:
        return "consolidated (skipped: no connection_factory)"

    conn = ctx.connection_factory()
    try:
        from cogito.store.memory_repo import MemoryRepository
        result_data = MemoryRepository(conn).consolidate()

        try:
            generator = MemoryViewsGenerator(conn)
            generator.generate_all()
        except Exception as e:
            _LOGGER.warning("Failed to refresh views after consolidation: %s", e)

        result = f"consolidated: {result_data}"
        _LOGGER.info("memory.consolidate %s: %s", task.task_id, result)
        return result
    except Exception:
        conn.rollback()
        _LOGGER.exception("memory.consolidate failed")
        raise
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


def _handle_proactive_evaluate(task: Task, ctx: TaskHandlerContext) -> str:
    """proactive.evaluate: 批量评估 evaluating candidates → 决策 + 持久化。

    流程：
    - 取 proactive_config (默认 dry_run=True)
    - 取 policy (ProactivePolicyRepository.get_current)
    - 取 energy (energy_model.compute_energy)
    - 遍历 evaluating candidates (limit 10)
    - 每 candidate 调 decide → action
    - action=send_now → enqueue Delivery (via delivery_service) + 写 decision_v2
    - action=send_later → enqueue_send_later + 写 decision_v2
    - action=digest → enqueue_digest_publish + 写 decision_v2
    - 其它 silent/discard → 写 decision_v2 + 更新 candidate.status
    """
    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "evaluate skipped: no connection"
    try:
        result = _evaluate_candidates_sync(conn, ctx)
        try:
            conn.close()
        except Exception:
            pass
        return result
    except Exception:
        _LOGGER.exception("proactive.evaluate failed")
        try:
            conn.close()
        except Exception:
            pass
        raise


def _evaluate_candidates_sync(conn, ctx) -> str:
    import time

    from cogito.service.energy_model import compute_energy
    from cogito.service.proactive_decision import decide, enqueue_send_later, persist_decision
    from cogito.service.proactive_digest_service import enqueue_digest_publish
    from cogito.store.proactive_repo import (
        ProactiveCandidateRepository,
        ProactiveDecisionRepository,
        ProactivePolicyRepository,
    )

    config = ctx.proactive_config
    if config is None:
        # 默认 dry-run + 默认 policy
        from cogito.config import ProactiveConfig
        config = ProactiveConfig()

    # 取 policy
    policy = ProactivePolicyRepository(conn).get_current(
        principal_id=config.default_principal_id,
    )

    # ── M1: energy 使用真实用户活动快照（同批固定，避免逐 Candidate 漂移）──
    now = int(time.time() * 1000)
    last_user_dt = None
    if ctx.presence_reader is not None:
        try:
            last_user_dt = ctx.presence_reader.get_last_user_activity(
                config.default_principal_id,
            )
        except Exception:
            last_user_dt = None  # fail-safe: 不按最低能量增强主动性
    last_user_at = epoch_ms_from_datetime(last_user_dt)
    energy_value = compute_energy(last_user_dt)

    # 取 evaluating candidates
    candidates = ProactiveCandidateRepository(conn).find_evaluating(
        principal_id=config.default_principal_id, limit=10,
    )
    if not candidates:
        return "evaluate: no candidates"

    actions = {"send_now": 0, "send_later": 0, "digest": 0, "silent": 0, "discard": 0, "other": 0}
    # 本批 dry_run 取自 config 不可变快照；real mode 下才创建真实副作用
    dry_run = bool(config.dry_run)
    energy_model_version = "v1"

    for c in candidates:
        action, trace = decide(
            c, policy,
            energy_value=energy_value,
            existing_hourly_sent=ProactiveDecisionRepository(conn).count_hourly_sent(
                config.default_principal_id, now // (3600 * 1000),
            ),
            existing_daily_sent=ProactiveDecisionRepository(conn).count_daily_sent(
                config.default_principal_id, now // (86400 * 1000),
            ),
        )
        # 持久化 decision（显式 dry_run + 审计字段）
        persist_decision(
            conn, c, policy, action, trace,
            dry_run=dry_run,
            energy_value=energy_value,
            last_user_at=last_user_at,
            energy_model_version=energy_model_version,
            config_version_id=ctx.config_version_id or None,
        )
        # 根据 action 触发后续
        if action == "send_now":
            if dry_run or ctx.delivery_service is None:
                pass  # dry_run: 仅记录，不创建真实 Delivery
            else:
                import asyncio

                from cogito.service.delivery_service import DeliveryRequest
                async def _do():
                    return await ctx.delivery_service.enqueue(DeliveryRequest(
                        target={"channel": "web", "principal_id": c.principal_id},
                        content_ref=c.summary,
                        idempotency_key=f"proactive-now:{c.candidate_id}",
                    ))
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(1) as p:
                        p.submit(asyncio.run, _do()).result()
                else:
                    asyncio.run(_do())
            ProactiveCandidateRepository(conn).update_status(
                c.candidate_id, "decided", consumed_at=now,
            )
            actions["send_now"] += 1

        elif action == "send_later":
            enqueue_send_later(
                conn,
                candidate_id=c.candidate_id,
                content_ref=c.summary,
                suggested_target={"channel": "web", "principal_id": c.principal_id},
                reason=f"decide={action}",
                delay_minutes=policy.digest_max_delay_minutes,
                policy_version=policy.version,
            )
            ProactiveCandidateRepository(conn).update_status(
                c.candidate_id, "decided", consumed_at=now,
            )
            actions["send_later"] += 1

        elif action == "digest":
            enqueue_digest_publish(
                conn,
                principal_id=c.principal_id,
                digest_date=time.strftime("%Y-%m-%d", time.gmtime(now / 1000)),
                topic=c.topic,
                delay_minutes=policy.digest_max_delay_minutes,
            )
            ProactiveCandidateRepository(conn).update_status(
                c.candidate_id, "consumed", consumed_at=now,
            )
            actions["digest"] += 1

        else:
            # silent / discard / ask_permission / create_task
            ProactiveCandidateRepository(conn).update_status(
                c.candidate_id, "consumed", consumed_at=now,
            )
            actions["other"] += 1

    conn.commit()
    return f"evaluate: {actions}"


def epoch_ms_from_datetime(dt) -> int | None:
    """将 datetime 转为 epoch ms；None / 非 datetime → None。"""
    if dt is None:
        return None
    from cogito.contracts.clock import epoch_ms
    try:
        return epoch_ms(dt)
    except Exception:
        return None
