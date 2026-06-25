# cogito/agent/ports/clock.py

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    """Abstract time source for the runtime."""

    def now(self) -> datetime:
        ...
