"""Context Builder — re-exported from cogito.contracts.context (PLAN-09 M3).

Kept as a compatibility shim. New code should import from
`cogito.contracts.context` directly.
"""

from __future__ import annotations

from cogito.contracts.context import (
    ContextBuilder,
    ContextItem,
    ContextSnapshot,
    estimate_tokens,
)

__all__ = ["ContextBuilder", "ContextItem", "ContextSnapshot", "estimate_tokens"]
