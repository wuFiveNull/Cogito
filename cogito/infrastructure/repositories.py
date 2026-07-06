"""仓库协议（Repository Protocols）。

每个聚合一个仓库。仓库负责聚合的持久化和查询。
方法签名只定义输入输出类型，不定义实现。
"""

from datetime import datetime
from typing import Protocol


# =============================================================================
# Conversation 仓库
# =============================================================================


class ConversationRepository(Protocol):
    """Conversation 聚合的持久化。"""

    async def get(self, conversation_id: str) -> object | None: ...
    async def get_by_platform(self, platform_conversation_id: str, channel_instance_id: str) -> object | None: ...
    async def save(self, conversation: object) -> None: ...
    async def list_by_principal(self, principal_id: str, limit: int = 50) -> list[object]: ...


# =============================================================================
# Turn 仓库
# =============================================================================


class TurnRepository(Protocol):
    """Turn 聚合（Turn + RunAttempt + Checkpoint）的持久化。"""

    async def get(self, turn_id: str) -> object | None: ...
    async def get_active_by_session(self, session_id: str) -> object | None: ...
    async def save(self, turn: object) -> None: ...
    async def list_by_conversation(self, conversation_id: str, limit: int = 50) -> list[object]: ...

    # RunAttempt
    async def get_attempt(self, attempt_id: str) -> object | None: ...
    async def save_attempt(self, attempt: object) -> None: ...
    async def list_attempts(self, turn_id: str) -> list[object]: ...

    # Checkpoint
    async def get_checkpoint(self, checkpoint_id: str) -> object | None: ...
    async def get_latest_checkpoint(self, turn_id: str) -> object | None: ...
    async def save_checkpoint(self, checkpoint: object) -> None: ...

    # Recovery
    async def find_abandoned_attempts(self, before: datetime) -> list[object]: ...


# =============================================================================
# Task 仓库
# =============================================================================


class TaskRepository(Protocol):
    """Task 聚合（Task + TaskAttempt + Schedule + Lease）的持久化。"""

    async def get(self, task_id: str) -> object | None: ...
    async def get_next_due(self, now: datetime, limit: int = 10) -> list[object]: ...
    async def save(self, task: object) -> None: ...

    # Lease
    async def acquire_lease(
        self, task_id: str, owner: str, expires_at: datetime
    ) -> object | None:
        """条件获取 Lease：状态可运行、时间到期、无有效 Lease。返回更新后的 Task。"""
        ...

    async def release_lease(self, task_id: str, owner: str, version: int) -> bool:
        """条件释放 Lease：owner 和 version 必须匹配。"""
        ...

    # TaskAttempt
    async def get_attempt(self, task_attempt_id: str) -> object | None: ...
    async def save_attempt(self, attempt: object) -> None: ...

    # Schedule
    async def get_schedule(self, schedule_id: str) -> object | None: ...
    async def save_schedule(self, schedule: object) -> None: ...
    async def list_due_schedules(self, now: datetime, limit: int = 100) -> list[object]: ...


# =============================================================================
# Event 仓库
# =============================================================================


class EventRepository(Protocol):
    """Event 聚合的持久化与查询。"""

    async def append(self, event: object) -> None:
        """追加事件（只追加，不修改）。"""
        ...

    async def get_by_aggregate(
        self, aggregate_type: str, aggregate_id: str, since_version: int = 0
    ) -> list[object]: ...

    async def get_by_type_since(
        self, event_type: str, since: datetime, limit: int = 100
    ) -> list[object]: ...

    async def save(self, event: object) -> None: ...


# =============================================================================
# Delivery 仓库
# =============================================================================


class DeliveryRepository(Protocol):
    """Delivery 聚合的持久化。"""

    async def get(self, delivery_id: str) -> object | None: ...
    async def get_pending(self, limit: int = 10) -> list[object]: ...
    async def save(self, delivery: object) -> None: ...
    async def list_by_target(self, endpoint_id: str, limit: int = 50) -> list[object]: ...


# =============================================================================
# Memory 仓库
# =============================================================================


class MemoryRepository(Protocol):
    """MemoryItem 聚合的持久化。"""

    async def get(self, memory_id: str) -> object | None: ...
    async def save(self, memory: object) -> None: ...

    async def query(
        self,
        principal_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[object]: ...

    async def find_duplicates(self, subject: str, predicate: str) -> list[object]: ...
    async def list_by_source(self, source_type: str, source_id: str) -> list[object]: ...


# =============================================================================
# Trace 仓库
# =============================================================================


class TraceRepository(Protocol):
    """Trace 和 Span 的持久化。"""

    async def save_trace(self, trace: object) -> None: ...
    async def save_span(self, span: object) -> None: ...
    async def get_trace(self, trace_id: str) -> object | None: ...
    async def list_spans(self, trace_id: str) -> list[object]: ...


# =============================================================================
# Payload 仓库
# =============================================================================


class PayloadRepository(Protocol):
    """Payload Store 的持久化接口。

    写入协议：临时文件 → 计算 hash/size → fsync/close → 原子 rename → 写 metadata。
    """

    async def put(self, content: bytes, content_type: str) -> object:  # PayloadRef
        ...

    async def get(self, ref: str) -> bytes | None:
        ...

    async def get_ref(self, sha256: str) -> object | None:  # PayloadRef
        ...

    async def delete(self, ref: str) -> bool:
        ...
