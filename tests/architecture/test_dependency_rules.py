"""Architecture dependency rules — Plan 01 M1 / PLAN-09 M0.

Enforces SYSTEM-BOUNDARIES / 2 + / 4 at CI time:
  1. domain / contracts / config must not depend on infrastructure subpackages;
  2. Agent Runtime (cogito.runtime) must not write Repository directly;
  3. Dashboard (cogito.interaction_web) must not execute write SQL directly;
  4. assembly root (application) is exempt from inbound restrictions;
  5. import cycles are forbidden (tracked via a time-bounded exception registry).

NOTE (PLAN-09 M0): cogito.__main__ has been removed. If it is ever
reintroduced this test must be updated to guard against it.



Known violations are registered with an ADR link and a clear_by date; once a
violation is cleared the entry is removed, and the test will fail on any
*new* violation or any exception past its clear_by date.
"""
from __future__ import annotations

import ast
import datetime as _dt
from pathlib import Path

import pytest

from tests.architecture._scan import cycles, forbidden_edges, scan_imports

# ---------------------------------------------------------------------------
# Known-violation exception registry.
#
# Each entry documents a pre-existing violation that Phase 1.5 / Phase 2 will
# clear.  `clear_by` is a hard deadline: once past it, the entry is treated as
# overdue and the test FAILS, so nothing slips.
# Loaded from the single source of truth below so CI can render it.
# ---------------------------------------------------------------------------

KNOWN_VIOLATIONS: dict[str, dict[str, str]] = {
    "cogito.capability -> cogito.store": {
        "reason": "capability layer imports store repos directly",
        "adr_link": "ADR-000 TBD",
        "clear_by": "2026-09-30",
        "owner": "Plan 01 M2 / capability port extraction",
    },
    "cogito.store -> cogito.model": {
        "reason": "store layer aware of model adapter types",
        "adr_link": "ADR-000 TBD",
        "clear_by": "2026-09-30",
        "owner": "Plan 01 M5 / store cleanup",
    },
    "cogito.interaction_web -> cogito.store": {
        "reason": "interaction_web imports store repositories to serve queries",
        "adr_link": "ADR-000 TBD",
        "clear_by": "2026-09-30",
        "owner": "Plan 01 M2 / interaction web port extraction",
    },
    # Cycle: channel -> inbound -> service -> channel
    "cogito.channel -> cogito.inbound": {
        "reason": "channel reaches into inbound; inbound->service->channel closes the cycle",
        "adr_link": "ADR-000 TBD",
        "clear_by": "2026-09-30",
        "owner": "Plan 01 M2 / inbound port extraction",
    },
}


def _today() -> _dt.date:
    return _dt.date.today()


def test_no_new_forbidden_edges() -> None:
    """No dependency may cross a hard boundary outside the known registry."""
    graph, _ = scan_imports()
    live = forbidden_edges(graph)
    allowed_keys = set(KNOWN_VIOLATIONS.keys())
    overdue: list[str] = []
    unexpected: dict[str, set[str]] = {}

    for src, dests in live.items():
        for dst in sorted(dests):
            key = f"{src} -> {dst}"
            if key in allowed_keys:
                clear_by = _dt.date.fromisoformat(KNOWN_VIOLATIONS[key]["clear_by"])
                if _today() > clear_by:
                    overdue.append(f"{key} (was due {clear_by})")
            else:
                unexpected.setdefault(src, set()).add(dst)

    messages = []
    if overdue:
        messages.append("Overdue architecture exceptions:\n  " + "\n  ".join(overdue))
    if unexpected:
        body = "\n".join(
            f"  {s} -> {sorted(ds)}" for s, ds in sorted(unexpected.items())
        )
        messages.append("New forbidden edges must be registered with ADR + clear_by:\n" + body)
    assert not messages, "\n\n".join(messages)


def test_exception_registry_freshness() -> None:
    """Every registered exception must have a future clear_by date today."""
    stale = [
        key
        for key, meta in KNOWN_VIOLATIONS.items()
        if _dt.date.fromisoformat(meta["clear_by"]) < _today()
    ]
    assert not stale, "Stale exceptions past clear_by: " + ", ".join(stale)


def test_no_import_cycles() -> None:
    """Module graph must be cycle-free (once known cycles are cleared)."""
    graph, _ = scan_imports()
    # Build a graph with known-violation edges removed (they are being cleared)
    cleaned: dict[str, set[str]] = {m: set(ds) for m, ds in graph.items()}
    for key in KNOWN_VIOLATIONS:
        src, dst = key.split(" -> ")
        cleaned.get(src, set()).discard(dst)
    found = cycles(cleaned)
    assert not found, "Cycles: " + "; ".join(" -> ".join(c) for c in found)


def test_known_violations_are_real() -> None:
    """Declared exceptions must match actual edges — no paper exceptions."""
    graph, _ = scan_imports()
    for key in KNOWN_VIOLATIONS:
        src, dst = key.split(" -> ")
        assert dst in graph.get(src, set()), (
            f"Exception {key!r} is not an actual import edge anymore — remove it"
        )


def test_scan_matches_map_doc() -> None:
    """Loose guard: make sure the scanner still sees all modules mentioned in the map."""
    _, modules = scan_imports()
    expected = {
        "cogito.domain",
        "cogito.contracts",
        "cogito.config",
        "cogito.store",
        "cogito.model",
        "cogito.capability",
        "cogito.tools",
        "cogito.runtime",
        "cogito.inbound",
        "cogito.channel",
        "cogito.service",
        "cogito.interaction_web",
        "cogito.application",
    }
    missing = expected - modules
    assert not missing, f"Scanner missing expected modules: {missing}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
