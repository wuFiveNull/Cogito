"""Public errors for read-only canonical Event queries.

The error is deliberately a contract rather than a storage implementation
detail: HTTP, MCP, and any future UI adapter need to classify an invalid cursor
without importing the EventStore package.
"""

from __future__ import annotations


class EventCursorError(ValueError):
    """The supplied canonical Event Explorer cursor is malformed."""


class EventPayloadUnavailableError(ValueError):
    """An Event references a payload that is absent, expired, or corrupted."""
