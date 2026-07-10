"""Time utilities — re-exported from cogito.contracts.clock (PLAN-09 M2).

Kept as a compatibility shim. New code should import from
`cogito.contracts.clock` directly.
"""
from __future__ import annotations

from cogito.contracts.clock import (
    EPOCH,
    epoch_ms,
    from_epoch_ms,
    iso_to_epoch_ms,
    now_ms,
)

__all__ = ["EPOCH", "epoch_ms", "from_epoch_ms", "now_ms", "iso_to_epoch_ms"]
