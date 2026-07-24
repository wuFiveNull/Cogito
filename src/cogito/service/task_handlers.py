"""TaskHandlerRegistry — Task 类型到处理函数的映射。

里程碑 B2+B3：Task Payload 定义 + 异步 Handler 上下文注入。

首批 Handler：
- memory.extract: 从会话中提取记忆候选
- summary.generate: 生成/更新会话摘要
- memory.consolidate: 记忆合并与归档

DOMAIN-CONTRACTS / 1.13 MemoryItem：状态转换规则
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cogito.domain.task import Task
from cogito.infrastructure.metrics_access import _metrics  # OPS-04: 任务级指标
from cogito.service.memory_extractor import MemoryExtractionWriteError, _is_database_lock_error
from cogito.service.memory_views import MemoryViewsGenerator

_LOGGER = logging.getLogger(__name__)


def _sqlite_lock_diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    """收集当前连接的无副作用锁诊断信息。

    SQLite 不提供可移植的“持锁连接 ID”查询。此快照用于结合其他进程的
    同类日志判断竞争源，且不执行 checkpoint 或任何写操作。
    """
    info: dict[str, Any] = {
        "process": os.getpid(),
        "thread": threading.get_ident(),
        "connection": hex(id(conn)),
        "in_transaction": conn.in_transaction,
        "isolation_level": conn.isolation_level,
    }
    try:
        info["database"] = [
            row[2] for row in conn.execute("PRAGMA database_list").fetchall() if row[2]
        ]
        info["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        info["busy_timeout_ms"] = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    except sqlite3.Error as exc:
        info["diagnostic_error"] = str(exc)
    return info

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


def make_idempotency_key(
    task_type: str,
    conversation_id: str,
    session_id: str,
    from_seq: int,
    to_seq: int,
    prompt_version: str,
) -> str:
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
    # 为后台任务的短生命周期 SQLite 连接创建对应的 DeliveryService。
    # 不提供时保留 delivery_service，供测试和非 SQLite 实现兼容。
    delivery_service_factory: Callable[[sqlite3.Connection], Any] | None = None
    # 主动推送配置（来自 config.capability.proactive）
    proactive_config: Any = None  # ProactiveConfig
    # 用户活动读取（PresenceReader Port）；fail-safe 时返回 None
    presence_reader: Any = None  # PresenceReader
    # 决定时生效的配置版本（供 Decision 审计追溯）
    config_version_id: str = ""
    # 当前 Task 元信息（IngestionBatch 日志需要）
    _task_id: str = ""
    _attempt_id: str = ""
    # PLAN-16 M3 MEM-01: handler 主动声明的记忆依赖（成功后被强化）
    declared_memory_dependencies: list[str] = field(default_factory=list)
    # PLAN-16 M4 完整 payload 边界：PayloadStore 工厂（提供时 resolver 化段落文本）
    payload_store_factory: Callable[[], Any] | None = None
    capability_registry: Any = None
    tool_executor: Any = None
    parent_toolsets: set[str] = field(default_factory=set)
    # Cooperative cancellation for synchronous handlers running in a worker thread.
    shutdown_requested: Callable[[], bool] | None = None

    def declare_memory_dependencies(self, memory_ids: list[str]) -> None:
        """声明本 Task 使用并强化的记忆（PLAN-16 M3 MEM-01）。

        禁止从任意文本猜测依赖：handler 必须显式传入受影响的 memory_id。
        """
        if memory_ids:
            self.declared_memory_dependencies = list(memory_ids)


# Handler 签名：同步或 async 函数，接收 Task 和上下文，返回结果文本。
# 调用异步模型/httpx 的 handler 必须为 async def，由 worker 在主 loop 上 await，
# 以复用主 loop 的 httpx 连接池（避免 loop 不匹配）。
TaskHandler = Callable[[Task, TaskHandlerContext], Awaitable[str] | str]


@dataclass(frozen=True)
class TaskHandlerWait:
    status: str
    waiting_id: str


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
    registry.register("agent.prompt", _handle_agent_prompt)
    registry.register("tool.reconcile", _handle_tool_reconcile)
    registry.register("agent.delegate", _handle_agent_delegate)
    return registry


async def _handle_agent_delegate(
    task: Task,
    ctx: TaskHandlerContext,
) -> str | TaskHandlerWait:
    if (
        ctx.connection_factory is None
        or ctx.model_router is None
        or ctx.capability_registry is None
        or ctx.tool_executor is None
    ):
        raise RuntimeError("agent.delegate runtime is not configured")
    try:
        payload = json.loads(task.payload_ref or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid agent.delegate payload") from exc
    from cogito.contracts.clock import epoch_ms
    from cogito.contracts.context import ContextItem, ContextSnapshot
    from cogito.domain.event import Event, EventClass, EventContext
    from cogito.runtime.loop import AgentLoop, LoopResultType, ResourceBudget
    from cogito.store.checkpoint_repo import CheckpointRepository
    from cogito.store.event_replay import replay_run_attempt, replay_turn
    from cogito.store.event_store import EventStore

    conn = ctx.connection_factory()
    try:
        link = conn.execute(
            "SELECT * FROM child_task_links WHERE task_id=?",
            (task.task_id,),
        ).fetchone()
        if link is None:
            raise ValueError("child task link not found")
        turn_id = link["turn_id"] or uuid.uuid4().hex
        attempt_id = uuid.uuid4().hex
        now = epoch_ms(datetime.now(UTC))
        events = EventStore(conn)
        if not link["turn_id"]:
            events.append(
                Event(
                    event_type="runtime.turn.accepted",
                    stream_type="turn",
                    stream_id=turn_id,
                    producer="agent-delegate-task",
                    event_class=EventClass.DOMAIN,
                    context=EventContext(
                        trace_id=str(payload.get("trace_id", "")),
                        principal_id=str(payload.get("principal_id", "")),
                        conversation_id=str(payload.get("conversation_id", "")),
                        session_id=str(payload.get("session_id", "")),
                        turn_id=turn_id,
                    ),
                    summary="Delegated child Turn accepted",
                    attributes={"input_message_id": str(payload.get("input_message_id", "")), "priority": 40},
                    outcome="accepted",
                    occurred_at=now,
                    idempotency_key=f"delegation:{task.task_id}:turn-accepted",
                ),
                expected_version=0,
            )
            attempt_no = 1
            conn.execute(
                "UPDATE child_task_links SET turn_id=?,status='running',version=version+1 "
                "WHERE task_id=?",
                (turn_id, task.task_id),
            )
        else:
            attempt_no = 1 + max(
                (
                    replay_run_attempt(events.read_stream("run_attempt", event.stream_id), event.stream_id).attempt_no
                    for event in events.read_stream_type("run_attempt")
                    if event.context.turn_id == turn_id
                    and replay_run_attempt(events.read_stream("run_attempt", event.stream_id), event.stream_id)
                    is not None
                ),
                default=0,
            )
            turn = replay_turn(events.read_stream("turn", turn_id), turn_id)
            if turn is None or turn.status not in {"queued", "waiting_user", "failed"}:
                raise RuntimeError("delegated child Turn is not runnable")
            events.append(
                Event(
                    event_type="runtime.turn.started",
                    stream_type="turn",
                    stream_id=turn_id,
                    producer="agent-delegate-task",
                    event_class=EventClass.OPERATION,
                    context=EventContext(
                        trace_id=str(payload.get("trace_id", "")),
                        principal_id=str(payload.get("principal_id", "")),
                        conversation_id=str(payload.get("conversation_id", "")),
                        session_id=turn.session_id,
                        turn_id=turn_id,
                        attempt_id=attempt_id,
                    ),
                    summary="Delegated child Turn started",
                    attributes={"active_attempt_id": attempt_id},
                    outcome="running",
                    occurred_at=now,
                    idempotency_key=f"delegation:{task.task_id}:turn-started:{attempt_no}",
                ),
                expected_version=turn.stream_version,
            )
            conn.execute(
                "UPDATE child_task_links SET status='running',version=version+1 WHERE task_id=?",
                (task.task_id,),
            )
        turn = replay_turn(events.read_stream("turn", turn_id), turn_id)
        if turn is None:
            raise RuntimeError("delegated child Turn was not created")
        if turn.status != "running":
            events.append(
                Event(
                    event_type="runtime.turn.started",
                    stream_type="turn",
                    stream_id=turn_id,
                    producer="agent-delegate-task",
                    event_class=EventClass.OPERATION,
                    context=EventContext(session_id=turn.session_id, turn_id=turn_id, attempt_id=attempt_id),
                    summary="Delegated child Turn started",
                    attributes={"active_attempt_id": attempt_id},
                    outcome="running",
                    occurred_at=now,
                    idempotency_key=f"delegation:{task.task_id}:turn-started:{attempt_no}",
                ),
                expected_version=turn.stream_version,
            )
        events.append(
            Event(
                event_type="runtime.attempt.started",
                stream_type="run_attempt",
                stream_id=attempt_id,
                producer="agent-delegate-task",
                event_class=EventClass.OPERATION,
                context=EventContext(
                    trace_id=str(payload.get("trace_id", "")),
                    principal_id=str(payload.get("principal_id", "")),
                    conversation_id=str(payload.get("conversation_id", "")),
                    session_id=str(payload.get("session_id", "")),
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                    task_id=task.task_id,
                ),
                summary="Delegated child RunAttempt started",
                attributes={"attempt_no": attempt_no, "worker_id": "agent-delegate-task", "lease_version": 1},
                outcome="running",
                occurred_at=now,
                idempotency_key=f"delegation:{task.task_id}:attempt:{attempt_no}:started",
            ),
            expected_version=0,
        )
        conn.commit()
        budget = ResourceBudget(**dict(payload.get("budget") or {}))

        payload_store = None
        if ctx.payload_store_factory is not None:
            try:
                payload_store = ctx.payload_store_factory(conn)
            except TypeError:
                payload_store = ctx.payload_store_factory()
        checkpoints = CheckpointRepository(conn, payload_store=payload_store)

        def save_checkpoint(data: dict[str, Any]) -> None:
            checkpoints.save(turn_id, data)
            conn.commit()

        policy_allowed = None
        if bool(payload.get("read_only", False)):
            policy_allowed = {
                tool.capability_id
                for tool in ctx.capability_registry.all_tools()
                if tool.side_effect_class == "none"
            }
        loop = AgentLoop(
            ctx.model_router,
            registry=ctx.capability_registry,
            executor=ctx.tool_executor,
            toolsets=set(payload.get("toolsets", [])),
            budget=budget,
            checkpoint_callback=save_checkpoint,
            checkpoint_loader=checkpoints.load_latest,
            agent_mode="reactive",
            policy_allowed_capabilities=policy_allowed,
        )
        snapshot = ContextSnapshot(
            snapshot_id=f"delegation:{payload['delegation_id']}:{payload['client_id']}",
            turn_id=turn_id,
            attempt_id=attempt_id,
            input_message_id=str(payload.get("input_message_id", "")),
            session_id=str(payload.get("session_id", "")),
            conversation_id=str(payload.get("conversation_id", "")),
            principal_id=str(payload.get("principal_id", "")),
            items=(
                ContextItem(
                    item_type="system_policy",
                    item_id=f"{turn_id}:system",
                    source="system",
                    role="system",
                    trust_label="system",
                    content=(
                        "You are a bounded child Agent. Return a concise structured result to the "
                        "parent only. Never address the end user or send messages. Your assigned "
                        f"role is {payload.get('role', 'general')}. "
                        f"{payload.get('role_instruction', '')}"
                    ),
                ),
                ContextItem(
                    item_type="message",
                    item_id=f"{turn_id}:prompt",
                    source=str(payload.get("session_id", "")),
                    role="user",
                    trust_label="user",
                    content=str(payload["prompt"]),
                ),
            ),
        )

        def child_cancelled() -> bool:
            row = conn.execute(
                "SELECT status FROM agent_delegations WHERE delegation_id=?",
                (payload["delegation_id"],),
            ).fetchone()
            return row is None or row["status"] not in {"queued", "running"}

        result = await loop.run(snapshot, cancel_flag=child_cancelled)
        finished = epoch_ms(datetime.now(UTC))
        if result.result_type == LoopResultType.waiting_approval:
            attempt = replay_run_attempt(events.read_stream("run_attempt", attempt_id), attempt_id)
            turn = replay_turn(events.read_stream("turn", turn_id), turn_id)
            if attempt is None or turn is None:
                raise RuntimeError("delegated child lifecycle disappeared")
            events.append(
                Event(
                    event_type="runtime.attempt.completed",
                    stream_type="run_attempt", stream_id=attempt_id,
                    producer="agent-delegate-task", event_class=EventClass.OPERATION,
                    context=EventContext(session_id=turn.session_id, turn_id=turn_id, attempt_id=attempt_id, task_id=task.task_id),
                    summary="Delegated child RunAttempt reached approval wait",
                    outcome="completed", occurred_at=finished,
                    idempotency_key=f"delegation:{task.task_id}:attempt:{attempt_no}:waiting",
                ), expected_version=attempt.stream_version,
            )
            events.append(
                Event(
                    event_type="runtime.turn.waiting_user", stream_type="turn", stream_id=turn_id,
                    producer="agent-delegate-task", event_class=EventClass.OPERATION,
                    context=EventContext(session_id=turn.session_id, turn_id=turn_id, attempt_id=attempt_id, task_id=task.task_id),
                    summary="Delegated child Turn waiting for approval", outcome="waiting_user", occurred_at=finished,
                    idempotency_key=f"delegation:{task.task_id}:turn:waiting",
                ), expected_version=turn.stream_version,
            )
            conn.execute(
                "UPDATE child_task_links SET status='waiting_user',version=version+1 WHERE task_id=?",
                (task.task_id,),
            )
            conn.commit()
            return TaskHandlerWait("waiting_user", result.approval_id)
        status = (
            "completed"
            if result.is_success
            else ("cancelled" if result.result_type == LoopResultType.cancelled else "failed")
        )
        attempt = replay_run_attempt(events.read_stream("run_attempt", attempt_id), attempt_id)
        turn = replay_turn(events.read_stream("turn", turn_id), turn_id)
        if attempt is None or turn is None:
            raise RuntimeError("delegated child lifecycle disappeared")
        attempt_event = "runtime.attempt.completed" if result.is_success else (
            "runtime.attempt.cancelled" if status == "cancelled" else "runtime.attempt.failed"
        )
        turn_event = "runtime.turn.completed" if result.is_success else (
            "runtime.turn.cancelled" if status == "cancelled" else "runtime.turn.failed"
        )
        events.append(
            Event(
                event_type=attempt_event, stream_type="run_attempt", stream_id=attempt_id,
                producer="agent-delegate-task", event_class=EventClass.OPERATION,
                context=EventContext(session_id=turn.session_id, turn_id=turn_id, attempt_id=attempt_id, task_id=task.task_id),
                summary="Delegated child RunAttempt finished", outcome="completed" if result.is_success else status,
                occurred_at=finished, idempotency_key=f"delegation:{task.task_id}:attempt:{attempt_no}:finished",
            ), expected_version=attempt.stream_version,
        )
        events.append(
            Event(
                event_type=turn_event, stream_type="turn", stream_id=turn_id,
                producer="agent-delegate-task", event_class=EventClass.DOMAIN,
                context=EventContext(session_id=turn.session_id, turn_id=turn_id, attempt_id=attempt_id, task_id=task.task_id),
                summary="Delegated child Turn finished", outcome=status,
                occurred_at=finished, idempotency_key=f"delegation:{task.task_id}:turn:finished",
            ), expected_version=turn.stream_version,
        )
        result_payload = {
            "client_id": payload.get("client_id", ""),
            "role": payload.get("role", "general"),
            "result_summary": result.text[:2_000],
            "result": result.text,
            "toolsets": payload.get("toolsets", []),
            "budget": payload.get("budget", {}),
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
            "error": result.error_message,
        }
        result_json = json.dumps(result_payload, ensure_ascii=False)
        result_ref = result_json
        if ctx.payload_store_factory is not None:
            store = ctx.payload_store_factory()
            stored = store.put(
                result_json.encode("utf-8"),
                content_type="application/json",
                retention_class="hot",
            )
            result_ref = str(getattr(stored, "payload_id", "")) or result_json
        task_result = json.dumps(
            {
                "result_summary": result.text[:2_000],
                "result_ref": result_ref,
                "usage": result_payload["usage"],
                "error": result.error_message,
            },
            ensure_ascii=False,
        )
        conn.execute(
            "UPDATE child_task_links SET status=?,result_summary=?,result_ref=?,usage_json=?,"
            "error=?,completed_at=?,version=version+1 WHERE task_id=?",
            (
                status,
                result.text[:2_000],
                result_ref,
                json.dumps(result_payload["usage"]),
                result.error_message,
                datetime.now(UTC).isoformat(),
                task.task_id,
            ),
        )
        conn.commit()
        if not result.is_success:
            raise RuntimeError(result.error_message or result.text or "child Agent failed")
        return task_result
    finally:
        conn.close()


async def _handle_tool_reconcile(task: Task, ctx: TaskHandlerContext) -> str:
    """Classify an uncertain side effect for explicit/manual reconciliation.

    This handler deliberately never invokes the original Tool. A capability-
    specific reconciler can later resolve the receipt; absent one, the receipt is
    moved to ``manual_required`` so automatic retries remain impossible.
    """
    try:
        payload = json.loads(task.payload_ref or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid tool.reconcile payload") from exc
    receipt_id = str(payload.get("receipt_id", ""))
    if not receipt_id or ctx.connection_factory is None:
        raise ValueError("tool.reconcile requires receipt_id and connection")
    conn = ctx.connection_factory()
    try:
        from cogito.store.receipt_repo import SideEffectReceiptRepository

        receipt = SideEffectReceiptRepository(conn).get(receipt_id)
        if receipt is None:
            raise ValueError(f"receipt {receipt_id} not found")
        if receipt.status != "unknown" or receipt.reconcile_status != "pending":
            return f"receipt {receipt_id} already reconciled"
        repository = SideEffectReceiptRepository(conn)
        registry = ctx.capability_registry
        capability = registry.get(receipt.capability_id) if registry is not None else None
        reconciler = capability.reconcile_fn if capability is not None else None
        if reconciler is None:
            repository.update_reconcile(
                receipt_id,
                "manual_required",
                "No capability-specific reconciler is registered",
            )
            conn.commit()
            return f"receipt {receipt_id} requires manual reconciliation"
        outcome = reconciler(
            {
                "receipt_id": receipt.receipt_id,
                "capability_id": receipt.capability_id,
                "operation_id": receipt.operation_id,
                "request_hash": receipt.request_hash,
                "attempt_id": receipt.attempt_id,
                "summary": receipt.summary,
            }
        )
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if isinstance(outcome, dict):
            status = str(outcome.get("status", "manual_required"))
            summary = str(outcome.get("summary", ""))
        else:
            status, summary = str(outcome), ""
        if status == "succeeded":
            repository.update_status(receipt_id, "succeeded")
            repository.update_reconcile(receipt_id, "reconciled", summary)
        elif status == "not_executed":
            repository.update_status(receipt_id, "failed")
            repository.update_reconcile(receipt_id, "not_executed", summary)
        else:
            repository.update_reconcile(
                receipt_id,
                "manual_required",
                summary or "Reconciler was inconclusive",
            )
        conn.commit()
        return f"receipt {receipt_id} reconcile outcome: {status}"
    finally:
        conn.close()


async def _handle_agent_prompt(task: Task, ctx: TaskHandlerContext) -> str:
    """Execute a scheduled background prompt without direct Channel delivery."""
    from cogito.model.contracts import ModelRequest

    payload = _task_json(task)
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("agent.prompt requires a prompt")
    if ctx.model_router is None:
        raise RuntimeError("model router unavailable")
    response = await ctx.model_router.generate(
        ModelRequest(
            messages=(
                {"role": "system", "content": "You are a bounded background Cogito agent."},
                {"role": "user", "content": prompt},
            )
        ),
        model_role=str(payload.get("model_role", "main")),
    )
    return response.text


def _knowledge_service(ctx: TaskHandlerContext, conn: sqlite3.Connection):
    if ctx.knowledge_service_factory:
        return ctx.knowledge_service_factory(conn)
    from cogito.service.knowledge.service import KnowledgeService

    return KnowledgeService(conn, payload_store_factory=ctx.payload_store_factory)


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


def _embed_model_from(ctx: TaskHandlerContext) -> str:
    """从 context 获取当前 embedding model id（用于 embed 任务幂等键）。"""
    try:
        factory = ctx.knowledge_service_factory
        if factory is None:
            return ""
        svc = factory(ctx.connection_factory()) if ctx.connection_factory else None
        if svc is None:
            return ""
        embedder = getattr(svc, "_embedder", None)
        if embedder is None:
            return ""
        return getattr(embedder, "model_id", "") or ""
    except Exception:
        return ""


def _count_pending_segments(conn: sqlite3.Connection, svc: Any) -> int:
    """计算仍有待嵌入的段数（PLAN-16 embed 排水循环）。"""
    try:
        from cogito.store.knowledge_repo import list_unembedded_segments

        return len(
            list_unembedded_segments(
                conn, model=getattr(getattr(svc, "_embedder", None), "model_id", "") or None
            )
        )
    except Exception:
        return 0


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
        # PLAN-16 M4 KNOW-05: ingest 后提交独立的 embedding Task，使 segment 进入嵌入路径
        from cogito.service.knowledge.sync import enqueue_knowledge_embed

        enqueue_knowledge_embed(conn, origin="knowledge_ingest", embed_model=_embed_model_from(ctx))
        # PLAN-16 M2 TX-05: KnowledgeService 不再内部 commit，handler 统一提交。
        conn.commit()
        # OPS-04 完整：记录 knowledge ingest 指标
        _metrics().record_knowledge_ingest(status="ok" if segments else "empty")
        return f"knowledge ingested: {document.document_id} ({len(segments)} segments)"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _handle_knowledge_embed(task: Task, ctx: TaskHandlerContext) -> str:
    if not ctx.connection_factory:
        return "knowledge embed (skipped: no connection_factory)"
    conn = ctx.connection_factory()
    try:
        svc = _knowledge_service(ctx, conn)
        # PayloadStore factory 已在 _knowledge_service 构造 KnowledgeService 时注入，
        # embed_pending 会通过该实例字段解析段落正文。
        # 在主 loop 上 await，复用 httpx 连接池。
        count = await svc.embed_pending()
        # OPS-04 完整：记录 knowledge embedding 指标
        _metrics().record_knowledge_embedding(status="ok" if count else "empty")
        # 仅在本轮确实写入了向量时继续排水。没有 Embedder（或 Provider
        # 返回空向量）时 count=0 而 remaining>0；若仍入队会形成无限自触发循环。
        remaining = _count_pending_segments(conn, svc)
        if count > 0 and remaining > 0:
            from cogito.service.knowledge.sync import enqueue_knowledge_embed

            enqueue_knowledge_embed(
                conn, origin="knowledge_embed_drain", embed_model=_embed_model_from(ctx)
            )
        conn.commit()
        if count == 0 and remaining > 0:
            return f"knowledge embedding deferred: no vectors written (remaining_pending={remaining})"
        return f"knowledge embedded: {count} (remaining_pending={remaining})"
    except Exception:
        conn.rollback()
        # OPS-04 完整：记录 embedding 失败
        _metrics().record_knowledge_embedding(status="failed")
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
        # PLAN-16 M2 TX-05: KnowledgeService 不再内部 commit，handler 统一提交。
        conn.commit()
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
        from cogito.service.knowledge.sync import enqueue_knowledge_embed, sync_resource

        resource_id = sync_resource(
            conn,
            stable_source_id=str(data.get("stable_source_id", "")),
            source_kind=str(data.get("source_kind", "connector")),
            content_hash=str(data.get("content_hash", "")),
            raw_text=str(data.get("raw_text", "")),
            payload_ref=str(data.get("payload_ref", "")),
            principal_id=str(data.get("principal_id", "")),
            trust_label=str(data.get("trust_label", "unverified")),
            make_payload_store=ctx.payload_store_factory,
        )
        _refresh_knowledge_views_task(ctx, conn)
        # 完整：sync 后明确 enqueue 带版本幂等键的 embed Task（PLAN-16 KNOW-05/07）
        enqueue_knowledge_embed(
            conn, origin="knowledge_sync_source", embed_model=str(data.get("embed_model", ""))
        )
        # PLAN-16 M2 TX-05: KnowledgeService/sync 不再内部 commit，handler 统一提交。
        conn.commit()
        # OPS-04 完整：记录 knowledge ingest 指标（sync_source 走独立摄取路径）
        _metrics().record_knowledge_ingest(status="ok")
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
        # PLAN-17 R5 P0-06: emitter insert (DriftResult + Outbox) 依赖外部 commit，
        # 最后一原子提交避免 conn.close 丢弃。
        try:
            conn.commit()
        except Exception:
            pass
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


async def _handle_vision_analyze(task: Task, ctx: TaskHandlerContext) -> str:
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

    service = ctx.vision_service_factory()
    # 在主 loop 上 await，复用 httpx 连接池
    result = await service.analyze(analysis_id)
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

    return handle_mcp_connector_poll(task, ctx)


def _proactive_web_target(conn, principal_id: str) -> dict[str, str] | None:
    """Resolve a proactive push to the owner's most recently active Web chat.

    The browser Web channel persists real conversations using an internal
    principal ID, while proactive candidates commonly use the configured
    default ``owner`` ID.  Therefore the target is selected from the latest
    inbound user message on an active Web conversation instead of fabricating
    an invisible ``proactive:<principal>`` WebSocket-only inbox.
    """
    row = conn.execute(
        "SELECT c.platform_conversation_id "
        "FROM conversations c "
        "JOIN endpoints e ON e.endpoint_id=c.conversation_endpoint_id "
        "JOIN messages m ON m.conversation_id=c.conversation_id "
        "WHERE e.channel_type='web' AND e.status='active' "
        "AND c.status='active' AND c.platform_conversation_id<>'' "
        "AND m.role='user' AND m.direction='inbound' "
        "ORDER BY m.created_at DESC, m.receive_sequence DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "channel": "web",
        "adapter_id": "web",
        "conversation_id": str(row["platform_conversation_id"]),
        "principal_id": principal_id,
    }


def _task_delivery_service(ctx: TaskHandlerContext, conn: sqlite3.Connection) -> Any:
    """Return a delivery service bound to the task's SQLite connection.

    Reusing the application's long-lived DeliveryService opens a second writer
    while this task connection has an active proactive-decision transaction,
    which can cause SQLite ``database is locked`` failures.
    """
    if ctx.delivery_service_factory is not None:
        return ctx.delivery_service_factory(conn)
    return ctx.delivery_service


def _proactive_delivery_content_ref(
    conn: sqlite3.Connection,
    *,
    target: dict[str, Any],
    text: str,
    principal_id: str,
    idempotency_key: str,
) -> str:
    """Persist a live Web proactive message and return its content reference.

    A Delivery alone only reaches the in-memory WebSocket adapter. Persisting
    the assistant Message first makes a proactive notification visible after a
    page reload and prevents the Chat history effect from erasing it.
    """
    existing = conn.execute(
        "SELECT message_id FROM messages WHERE "
        "json_extract(capability_snapshot_json, '$.idempotency_key')=? "
        "ORDER BY created_at DESC, receive_sequence DESC LIMIT 1",
        (idempotency_key,),
    ).fetchone()
    if existing is not None and existing["message_id"]:
        return str(existing["message_id"])

    if target.get("channel") != "web":
        return text
    platform_conversation_id = str(target.get("conversation_id") or "")
    if not platform_conversation_id:
        return text
    conversation = conn.execute(
        "SELECT conversation_id FROM conversations WHERE platform_conversation_id=?",
        (platform_conversation_id,),
    ).fetchone()
    if conversation is None:
        _LOGGER.warning(
            "Skipping proactive message persistence: Web conversation not found: %s",
            platform_conversation_id,
        )
        return text

    conversation_id = str(conversation["conversation_id"])
    session = conn.execute(
        "SELECT session_id FROM sessions WHERE conversation_id=? AND status='active' "
        "ORDER BY created_at DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    reply_to = conn.execute(
        "SELECT message_id FROM messages WHERE conversation_id=? AND role='user' "
        "ORDER BY receive_sequence DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()

    from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
    from cogito.store.repositories import MessageRepository

    message = Message(
        conversation_id=conversation_id,
        session_id=str(session["session_id"]) if session is not None else "",
        sender_principal_id="cogito",
        sender_endpoint_id="web",
        role=MessageRole.assistant,
        direction=MessageDirection.outbound,
        content_parts=[ContentPart(content_type="text", inline_data=text, trust_label="internal")],
        reply_to_message_id=str(reply_to["message_id"]) if reply_to is not None else None,
        reply_route={
            "channel_instance_id": target.get("adapter_id") or "web",
            "platform_conversation_id": platform_conversation_id,
        },
        capability_snapshot={"origin": "proactive", "idempotency_key": idempotency_key},
        trust_label="internal",
    )
    repo = MessageRepository(conn)
    message.receive_sequence = repo.next_receive_sequence(conversation_id)
    repo.insert(message)
    for part in message.content_parts:
        repo.insert_content_part(part, message.message_id)
    return message.message_id


async def _handle_proactive_delivery_ready(task: Task, ctx: TaskHandlerContext) -> str:
    """proactive.delivery.ready: scheduled_delivery_request 到期 → Delivery。

    payload_ref=request_id。取 content_factory 创建 new Delivery 实例，入
    canonical Delivery effect 队列（async enqueue，在主 loop 上 await）。
    """
    request_id = task.payload_ref
    if not request_id:
        return "delivery.ready skipped: empty request_id"

    conn = ctx.connection_factory() if ctx.connection_factory else None
    if conn is None:
        return "delivery.ready skipped: no connection"

    try:
        result = await _deliver_scheduled_request_async(
            conn,
            request_id,
            _task_delivery_service(ctx, conn),
            proactive_config=ctx.proactive_config,
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


async def _deliver_scheduled_request_async(
    conn,
    request_id,
    delivery_service,
    *,
    proactive_config=None,
) -> str:
    from cogito.service.proactive_delivery_service import (
        mark_request_converted,
        prepare_delivery_from_request,
    )

    info = prepare_delivery_from_request(conn, request_id)
    if info is None:
        return "delivery.ready: request expired/cancelled/not-yet-due"

    content_ref = info["content_ref"]
    from cogito.store.proactive_repo import ProactivePolicyRepository

    principal_id = info.get("principal_id") or "owner"
    policy = ProactivePolicyRepository(conn).get_current(principal_id)
    dry_run = bool(proactive_config is None or proactive_config.dry_run or policy.dry_run)
    if dry_run or delivery_service is None:
        # dry_run 模式：记录但不真实投递
        _LOGGER.info(
            "[dry_run] would send scheduled request %s: %s",
            request_id,
            (content_ref or "")[:80],
        )
        mark_request_converted(conn, request_id, "dry-run-noop")
        return "converted (dry_run)"

    from cogito.service.delivery_service import DeliveryRequest

    target = dict(info["suggested_target"] or {})
    if target.get("channel") == "web":
        # Old scheduled requests may still point to the invisible
        # ``proactive:<principal>`` mailbox. Resolve them at send time too.
        if not target.get("conversation_id") or str(target["conversation_id"]).startswith(
            "proactive:"
        ):
            target = _proactive_web_target(conn, principal_id)
            if target is None:
                _LOGGER.warning(
                    "Skipping proactive delivery %s: no active Web conversation for %s",
                    request_id,
                    principal_id,
                )
                mark_request_converted(conn, request_id, "skipped-no-active-web-conversation")
                return "converted (skipped: no active web conversation)"

    # 在主 loop 上 await，避免跨 loop
    delivery_key = f"proactive-scheduled:{request_id}"
    delivery_id = await delivery_service.enqueue(
        DeliveryRequest(
            target=target,
            content_ref=_proactive_delivery_content_ref(
                conn,
                target=target,
                text=content_ref or "",
                principal_id=principal_id,
                idempotency_key=delivery_key,
            ),
            idempotency_key=delivery_key,
        )
    )
    mark_request_converted(conn, request_id, delivery_id)
    return f"converted -> {delivery_id}"


async def _handle_proactive_digest_publish(task: Task, ctx: TaskHandlerContext) -> str:
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
        result = await _publish_digest_async(
            conn,
            principal_id,
            digest_date,
            topic,
            _task_delivery_service(ctx, conn),
            proactive_config=ctx.proactive_config,
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


async def _publish_digest_async(
    conn,
    principal_id,
    digest_date,
    topic,
    delivery_service,
    *,
    proactive_config=None,
) -> str:
    from cogito.service.proactive_digest_service import assemble_and_render, mark_digest_sent

    rendered = assemble_and_render(
        conn,
        principal_id=principal_id,
        digest_date=digest_date,
        topic=topic,
    )
    if rendered is None:
        return "digest.publish: nothing to send"
    digest_id, text = rendered
    from cogito.store.proactive_repo import ProactivePolicyRepository

    policy = ProactivePolicyRepository(conn).get_current(principal_id)
    dry_run = bool(proactive_config is None or proactive_config.dry_run or policy.dry_run)
    if dry_run or delivery_service is None:
        _LOGGER.info(
            "[dry_run] would send digest %s (topic=%s, chars=%d)",
            digest_id,
            topic,
            len(text),
        )
        mark_digest_sent(conn, digest_id)
        return f"sent (dry_run): {digest_id}"

    from cogito.service.delivery_service import DeliveryRequest

    # 在主 loop 上 await，避免跨 loop 复用 httpx 连接池
    target = _proactive_web_target(conn, principal_id)
    if target is None:
        _LOGGER.warning(
            "Skipping proactive digest %s: no active Web conversation for %s",
            digest_id,
            principal_id,
        )
        return "digest.publish skipped: no active web conversation"

    delivery_key = f"proactive-digest:{digest_id}"
    delivery_id = await delivery_service.enqueue(
        DeliveryRequest(
            target=target,
            content_ref=_proactive_delivery_content_ref(
                conn,
                target=target,
                text=text,
                principal_id=principal_id,
                idempotency_key=delivery_key,
            ),
            idempotency_key=delivery_key,
        )
    )
    mark_digest_sent(conn, digest_id)
    return f"sent -> {delivery_id}"


# ── memory.extract （B5: 替换 stub 为真实流程）──


async def _handle_memory_extract(task: Task, ctx: TaskHandlerContext) -> str:
    """Run one durable, idempotent memory extraction window.

    该 handler 必须运行在 TaskWorker 所在的事件循环中。ModelRouter 复用的
    httpx.AsyncClient 已绑定该 loop；在线程中另起 asyncio.run() 会使连接池
    内的 asyncio 原语跨 loop 使用，进而导致 ``bound to a different event
    loop``。SQLite 操作保持短事务，网络等待期间不会阻塞主 loop。
    """
    payload = MemoryExtractionPayload.from_payload_ref(task.payload_ref or "{}")
    _LOGGER.info(
        "Task memory.extract: %s session=%s range=[%d..%d]",
        task.task_id,
        payload.session_id,
        payload.from_sequence,
        payload.to_sequence,
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
            value = grouped.setdefault(
                row["message_id"],
                {
                    "role": row["role"],
                    "sequence": row["receive_sequence"],
                    "principal": row["sender_principal_id"] or "",
                    "trust": row["trust_label"] or "unverified",
                    "parts": [],
                },
            )
            if row["inline_data"]:
                value["parts"].append(row["inline_data"])
        messages = [
            ExtractMessage(
                message_id=mid,
                role=value["role"],
                content="\n".join(value["parts"]),
                receive_sequence=value["sequence"],
                sender_principal_id=value["principal"],
                trust_label=value["trust"],
            )
            for mid, value in grouped.items()
        ]
        messages.sort(key=lambda value: value.receive_sequence)

        service = (
            ctx.memory_service_factory(conn)
            if ctx.memory_service_factory
            else SqliteMemoryService(conn, MemoryRepository(conn))
        )
        extractor = MemoryExtractor(
            conn,
            service,
            ctx.model_router,
            model_role=payload.model_role,
            strict=True,
        )
        try:
            written = await extractor.extract_from_messages(
                messages,
                principal_id=payload.principal_id,
                session_id=payload.session_id,
                from_sequence=payload.from_sequence,
                to_sequence=payload.to_sequence,
            )
        except MemoryExtractionWriteError as exc:
            if _is_database_lock_error(exc):
                _LOGGER.error(
                    "memory.extract SQLite write lock: task=%s attempt=%s session=%s "
                    "range=[%d..%d] diagnostics=%s",
                    task.task_id,
                    ctx._attempt_id,
                    payload.session_id,
                    payload.from_sequence,
                    payload.to_sequence,
                    _sqlite_lock_diagnostics(conn),
                )
            raise

        # MEM-01: 声明本 Task 实际创建的记忆（成功后写 task_succeeded 信号）
        ctx.declare_memory_dependencies(extractor.created_memory_ids)

        # Zero candidates is still a successfully processed window.
        latest = wm_repo.get(PROC_MEMORY_EXTRACT, payload.conversation_id, payload.session_id)
        if latest is None:
            wm_repo.upsert(
                PROC_MEMORY_EXTRACT,
                payload.conversation_id,
                payload.session_id,
                input_version=payload.input_version,
            )
            latest = wm_repo.get(PROC_MEMORY_EXTRACT, payload.conversation_id, payload.session_id)
        if latest and latest.processed_upto_sequence < payload.to_sequence:
            ok = wm_repo.advance(
                PROC_MEMORY_EXTRACT,
                payload.conversation_id,
                payload.session_id,
                to_sequence=payload.to_sequence,
                input_version=payload.input_version,
                expected_from_sequence=latest.processed_upto_sequence,
                expected_version=latest.version,
            )
            if not ok:
                current = wm_repo.get(
                    PROC_MEMORY_EXTRACT,
                    payload.conversation_id,
                    payload.session_id,
                )
                if current is None or current.processed_upto_sequence < payload.to_sequence:
                    raise RuntimeError("memory.extract watermark CAS failed")
        conn.commit()
        # OPS-04 完整：记录 extraction 真正完成（含水卫推进与候选写入）
        _metrics().record_extraction_completed()
        return f"extracted: {len(written)} candidates (upto={payload.to_sequence})"
    except Exception:
        conn.rollback()
        _LOGGER.exception("memory.extract failed")
        # OPS-04 完整：记录 extraction failed
        _metrics().record_extraction_failed()
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
        # 权重与规范 Event 在同一事务提交；完整信号不进入 Event 日志。
        from cogito.domain.event import Event, EventClass, EventContext
        from cogito.store.event_store import EventStore

        EventStore(conn).append(
            Event(
                event_type="memory.weight.recomputed",
                stream_type="memory_weight",
                stream_id=f"recompute-{ctx._task_id}",
                producer="memory-recompute-weight-handler",
                event_class=EventClass.OPERATION,
                context=EventContext(task_id=ctx._task_id),
                summary="Memory weights recomputed",
                attributes={"recomputed_count": count},
                outcome="recomputed",
                idempotency_key=f"memory-weight-recompute:{ctx._task_id}",
            )
        )
        conn.commit()
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
        task.task_id,
        payload.session_id,
        payload.from_sequence,
        payload.to_sequence,
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


async def _handle_proactive_evaluate(task: Task, ctx: TaskHandlerContext) -> str:
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
        result = await _evaluate_candidates_async(conn, ctx)
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


async def _evaluate_candidates_async(conn, ctx) -> str:
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
        principal_id=config.default_principal_id,
        limit=10,
    )
    if not candidates:
        return "evaluate: no candidates"

    actions = {"send_now": 0, "send_later": 0, "digest": 0, "silent": 0, "discard": 0, "other": 0}
    # global/config 与 Principal policy 任一要求 dry-run 都禁止真实副作用。
    dry_run = bool(config.dry_run or policy.dry_run)
    delivery_service = _task_delivery_service(ctx, conn)
    energy_model_version = "v1"

    for c in candidates:
        action, trace = decide(
            c,
            policy,
            energy_value=energy_value,
            existing_hourly_sent=ProactiveDecisionRepository(conn).count_hourly_sent(
                config.default_principal_id,
                now // (3600 * 1000),
            ),
            existing_daily_sent=ProactiveDecisionRepository(conn).count_daily_sent(
                config.default_principal_id,
                now // (86400 * 1000),
            ),
            existing_alert_hourly_sent=ProactiveDecisionRepository(conn).count_alert_hourly_sent(
                config.default_principal_id,
                now // (3600 * 1000),
            ),
        )
        # 持久化 decision（显式 dry_run + 审计字段）
        persist_decision(
            conn,
            c,
            policy,
            action,
            trace,
            dry_run=dry_run,
            energy_value=energy_value,
            last_user_at=last_user_at,
            energy_model_version=energy_model_version,
            config_version_id=ctx.config_version_id or None,
        )
        # 根据 action 触发后续
        if action == "send_now":
            if dry_run or delivery_service is None:
                pass  # dry_run: 仅记录，不创建真实 Delivery
            else:
                from cogito.service.delivery_service import DeliveryRequest

                # 在主 loop 上 await，避免跨 loop 复用 httpx 连接池
                target = _proactive_web_target(conn, c.principal_id)
                if target is None:
                    _LOGGER.warning(
                        "Skipping proactive candidate %s: no active Web conversation for %s",
                        c.candidate_id,
                        c.principal_id,
                    )
                else:
                    delivery_key = f"proactive-now:{c.candidate_id}"
                    await delivery_service.enqueue(
                        DeliveryRequest(
                            target=target,
                            content_ref=_proactive_delivery_content_ref(
                                conn,
                                target=target,
                                text=c.summary,
                                principal_id=c.principal_id,
                                idempotency_key=delivery_key,
                            ),
                            idempotency_key=delivery_key,
                        )
                    )
            ProactiveCandidateRepository(conn).update_status(
                c.candidate_id,
                "decided",
                consumed_at=now,
            )
            actions["send_now"] += 1

        elif action == "send_later":
            if not dry_run:
                enqueue_send_later(
                    conn,
                    candidate_id=c.candidate_id,
                    content_ref=c.summary,
                    # The latest Web conversation can legitimately be absent
                    # when this task runs.  Keep a resolvable Web target and
                    # resolve it when the scheduled request becomes due.
                    suggested_target=_proactive_web_target(conn, c.principal_id)
                    or {"channel": "web", "principal_id": c.principal_id},
                    reason=f"decide={action}",
                    delay_minutes=policy.digest_max_delay_minutes,
                    policy_version=policy.version,
                )
            ProactiveCandidateRepository(conn).update_status(
                c.candidate_id,
                "decided",
                consumed_at=now,
            )
            actions["send_later"] += 1

        elif action == "digest":
            if not dry_run:
                enqueue_digest_publish(
                    conn,
                    principal_id=c.principal_id,
                    digest_date=time.strftime("%Y-%m-%d", time.gmtime(now / 1000)),
                    topic=c.topic,
                    delay_minutes=policy.digest_max_delay_minutes,
                )
            ProactiveCandidateRepository(conn).update_status(
                c.candidate_id,
                "consumed",
                consumed_at=now,
            )
            actions["digest"] += 1

        else:
            # silent / discard / ask_permission / create_task
            ProactiveCandidateRepository(conn).update_status(
                c.candidate_id,
                "consumed",
                consumed_at=now,
            )
            actions["other"] += 1

    # 每次 handler 有界处理 10 条；若仍有 evaluating 候选，创建下一段 drain
    # Task，避免一次 AIHOT 拉取超过 Outbox/handler batch 后永久滞留。
    remaining = ProactiveCandidateRepository(conn).count_by_principal(
        config.default_principal_id,
        status="evaluating",
    )
    if remaining > 0:
        from cogito.domain.task import Task, TaskStatus
        from cogito.store.task_repo import TaskRepository

        drain_key = f"proactive-evaluate-drain:{getattr(ctx, '_task_id', 'manual')}"
        task_repo = TaskRepository(conn)
        if not task_repo.exists_by_idempotency(drain_key):
            task_repo.insert(
                Task(
                    task_id=f"task-pe-drain-{uuid.uuid4().hex[:16]}",
                    task_type="proactive.evaluate",
                    payload_ref="",
                    status=TaskStatus.queued,
                    priority=15,
                    idempotency_key=drain_key,
                    origin="proactive-evaluate-drain",
                )
            )
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
