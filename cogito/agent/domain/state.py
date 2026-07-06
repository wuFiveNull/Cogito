# cogito/agent/domain/state.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class SessionLifecycle(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SessionState:
    session_id: str
    actor_id: str
    lifecycle: SessionLifecycle = SessionLifecycle.ACTIVE
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    message_id: str
    session_id: str
    actor_id: str | None
    role: str
    content: str
    sequence: int
    created_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    content: str
    version: int
    updated_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserProfile:
    actor_id: str
    display_name: str | None = None
    locale: str | None = None
    timezone: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserSettings:
    locale: str = "zh-CN"
    timezone: str = "UTC"
    response_style: str | None = None
    tool_approval_mode: str = "default"
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    history_limit: int = 20
    max_tool_rounds: int | None = None
    model_profile: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
