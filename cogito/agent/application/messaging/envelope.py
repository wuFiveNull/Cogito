# cogito/agent/application/messaging/envelope.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    message_id: str
    message_type: str
    correlation_id: str
    source: str
    reply_to: str | None
    timestamp: datetime
    payload: Mapping[str, object]
    metadata: Mapping[str, object] = field(default_factory=dict)
