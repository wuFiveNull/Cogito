# cogito/agent/ports/ids.py

from __future__ import annotations

from typing import Protocol


class IdGeneratorPort(Protocol):
    """Generates unique identifiers for the runtime."""

    def new_id(self) -> str:
        ...
