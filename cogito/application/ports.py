"""服务协议（Protocol 类）。

所有服务接口使用 typing.Protocol 定义，只包含方法签名和文档字符串。
实现类不需要显式继承这些协议（结构化子类型）。
"""

from collections.abc import AsyncIterator
from typing import Protocol

from cogito.application.contracts import (
    CapabilityPolicy,
    ChannelCapabilities,
    ChannelEditRequest,
    ChannelRef,
    ChannelSendRequest,
    ChannelSendResult,
    ConnectorBatch,
    ConnectorCapabilities,
    ConnectorCursor,
    DeliveryRef,
    DeliveryRequest,
    HealthStatus,
    MemoryCandidate,
    MemoryQuery,
    MemoryResult,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelStreamChunk,
    PolicyDecision,
    ResumeCommand,
    SessionRef,
    TaskContext,
    TaskOutcome,
    TurnAccepted,
)
from cogito.application.envelopes import ChannelEnvelope, EventEnvelope
from cogito.domain.entities import Approval, Conversation, MemoryItem, Message
from cogito.domain.events import BaseEvent


# =============================================================================
# Turn 服务
# =============================================================================


class TurnService(Protocol):
    """Turn 生命周期管理：接受入站消息、取消、恢复。"""

    async def accept(self, envelope: ChannelEnvelope) -> TurnAccepted:
        """接受 Channel 入站消息，创建 Turn。"""
        ...

    async def cancel(self, turn_id: str, reason: str) -> None:
        """请求取消活跃或排队的 Turn。

        持久化 cancel_requested_at 并递增 Turn 版本。
        无法撤销已提交的副作用。
        """
        ...

    async def resume(self, turn_id: str, command: ResumeCommand) -> None:
        """从 waiting_user 或 waiting_external 状态恢复 Turn。"""
        ...


# =============================================================================
# Memory 服务
# =============================================================================


class MemoryService(Protocol):
    """长期记忆生命周期管理。"""

    async def retrieve(self, query: MemoryQuery) -> MemoryResult:
        """根据查询条件检索记忆。"""
        ...

    async def propose(self, candidates: list[MemoryCandidate]) -> list[MemoryItem]:
        """提议新的记忆候选项（candidate 状态）。"""
        ...

    async def confirm(self, memory_id: str) -> MemoryItem:
        """确认记忆候选项为活跃记忆。"""
        ...

    async def reject(self, memory_id: str) -> None:
        """拒绝记忆候选项。"""
        ...


# =============================================================================
# Delivery 服务
# =============================================================================


class DeliveryService(Protocol):
    """消息投递生命周期管理。

    与 Turn/RunAttempt 解耦，发送失败不回滚推理结果。
    """

    async def enqueue(self, request: DeliveryRequest) -> DeliveryRef:
        """入队投递请求。"""
        ...

    async def cancel(self, delivery_id: str) -> None:
        """取消未完成的投递。"""
        ...

    async def retry(self, delivery_id: str) -> None:
        """重试失败的投递。"""
        ...


# =============================================================================
# Event 发布者
# =============================================================================


class EventPublisher(Protocol):
    """领域事件发布。

    Event 表示已发生的事实，Consumer 必须幂等。
    """

    def publish(self, events: list[BaseEvent]) -> None:
        """发布领域事件到消息总线。"""
        ...


# =============================================================================
# Channel 驱动
# =============================================================================


class ChannelDriver(Protocol):
    """平台 Channel 的抽象驱动。

    拥有平台连接、协议转换、附件处理。
    不拥有 Agent 逻辑、Memory 或主动策略。
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, request: ChannelSendRequest) -> ChannelSendResult:
        """发送消息到平台。"""
        ...

    async def edit(self, request: ChannelEditRequest) -> ChannelSendResult:
        """编辑已发送的消息。"""
        ...

    async def delete(self, request: ChannelEditRequest) -> None:
        """删除已发送的消息。"""
        ...

    def capabilities(self) -> ChannelCapabilities:
        """返回 Channel 能力描述。"""
        ...


# =============================================================================
# Model 提供者
# =============================================================================


class ModelProvider(Protocol):
    """LLM 模型提供者。

    负责与模型 API 通信，不包含 Agent 逻辑。
    """

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """同步生成模型响应。"""
        ...

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamChunk]:
        """流式生成模型响应。"""
        ...

    def capabilities(self) -> ModelCapabilities:
        """返回模型能力描述。"""
        ...

    async def health(self) -> HealthStatus:
        """检查模型服务健康状态。"""
        ...


# =============================================================================
# Session 策略
# =============================================================================


class SessionPolicy(Protocol):
    """会话解析策略。

    根据 Channel、Conversation 和 Message 计算 Session 归属。
    """

    async def resolve(
        self, channel: ChannelRef, conversation: Conversation, message: Message
    ) -> SessionRef:
        """解析 Message 应归属的 Session。"""
        ...


# =============================================================================
# Source 连接器
# =============================================================================


class SourceConnector(Protocol):
    """外部数据源连接器。

    负责拉取外部数据、归档原始内容、标准化和去重。
    """

    async def poll(self, cursor: ConnectorCursor | None) -> ConnectorBatch:
        """拉取一批数据。"""
        ...

    async def acknowledge(self, batch_id: str) -> None:
        """确认批次已成功处理。"""
        ...

    def capabilities(self) -> ConnectorCapabilities:
        """返回连接器能力描述。"""
        ...


# =============================================================================
# Task 处理器
# =============================================================================


class TaskHandler(Protocol):
    """持久化后台任务处理器。

    一次执行占用的入口，需要支持 Checkpoint、Retry、Waiting。
    """

    async def execute(self, context: TaskContext, payload: object) -> TaskOutcome:
        """执行任务并返回结果。"""
        ...


# =============================================================================
# Approval 策略
# =============================================================================


class ApprovalPolicy(Protocol):
    """审批策略引擎。

    评估操作风险并决定是否需要审批。
    """

    async def evaluate(self, subject_type: str, subject_id: str, action_hash: str) -> PolicyDecision:
        """评估是否需要审批。"""
        ...


# =============================================================================
# Identity 服务
# =============================================================================


class IdentityService(Protocol):
    """身份解析服务。"""

    async def resolve_identity(
        self, channel_type: str, channel_instance_id: str, platform_sender_id: str
    ) -> str:
        """解析平台身份到 Principal。"""
        ...

    async def bind_endpoint(self, endpoint_id: str, principal_id: str) -> None:
        """绑定 Endpoint 到 Principal。"""
        ...

    async def unbind_endpoint(self, endpoint_id: str) -> None:
        """解除 Endpoint 绑定。"""
        ...


# =============================================================================
# Context 构建器
# =============================================================================


class ContextBuilder(Protocol):
    """上下文装配器。

    输入：Turn、Session、Recent Messages、MemoryResult、Preferences、Goals。
    输出：ContextSnapshot。
    """

    async def build_snapshot(
        self, turn_id: str, session_id: str, budget: object  # ResourceBudget
    ) -> object:  # ContextSnapshot
        """为一次推理构建上下文快照。"""
        ...
