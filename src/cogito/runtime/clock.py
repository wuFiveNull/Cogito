"""Clock — re-exported from cogito.contracts.clock (PLAN-09 M2).

Kept as a compatibility shim. New code should import from
`cogito.contracts.clock` directly.
"""
from __future__ import annotations

from cogito.contracts.clock import (
    Clock,
    FakeClock,
    ProductionClock,
)

__all__ = ["Clock", "ProductionClock", "FakeClock"]
