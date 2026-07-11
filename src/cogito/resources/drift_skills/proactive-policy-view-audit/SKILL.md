# proactive-policy-view-audit

Read-only maintenance skill: verify that the in-memory/derived view of `proactive_policies` matches the SQLite authority.

## Goal
- Load current policy from DB (`ProactivePolicyRepository.get_current`).
- Diff against any derived view (e.g., config snapshot).
- Report mismatches. No writes.

## Constraints
- `max_steps=6`, `max_tool_calls=8`, no model calls.
- `can_emit_candidate=false`: this skill never produces user-visible output.
