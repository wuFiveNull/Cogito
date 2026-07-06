"""Domain entities — 核心领域模型。"""

from .principal import Principal, PrincipalType, PrincipalStatus, Endpoint, EndpointStatus
from .conversation import Conversation, ConversationType, Session, SessionStatus
from .message import Message, MessageRole, MessageDirection, ContentPart
from .turn import Turn, TurnStatus, RunAttempt, RunAttemptStatus
from .task import Task, TaskStatus, TaskAttempt, TaskAttemptStatus
from .delivery import Delivery, DeliveryStatus, DeliveryAttempt
from .memory import MemoryItem, MemoryKind, MemoryStatus
from .events import DomainEvent

__all__ = [
    "Principal", "PrincipalType", "PrincipalStatus", "Endpoint", "EndpointStatus",
    "Conversation", "ConversationType", "Session", "SessionStatus",
    "Message", "MessageRole", "MessageDirection", "ContentPart",
    "Turn", "TurnStatus", "RunAttempt", "RunAttemptStatus",
    "Task", "TaskStatus", "TaskAttempt", "TaskAttemptStatus",
    "Delivery", "DeliveryStatus", "DeliveryAttempt",
    "MemoryItem", "MemoryKind", "MemoryStatus",
    "DomainEvent",
]
