# cogito/agent/subagent/spec.py
#
# SubAgent data models — specification and result types.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Sequence


SubAgentStatus = Literal["running", "completed", "incomplete", "error"]
SubAgentProfile = Literal["research", "scripting", "general"]


@dataclass(frozen=True, slots=True)
class SubAgentSpec:
    """Specification for creating a SubAgent instance.

    Attributes:
        task:              Natural language task description for the sub-agent.
        tool_names:        Names of tools this sub-agent is allowed to use.
        max_iterations:    Maximum model-invoke → tool-call rounds.
        profile:           Capability profile (controls default limits).
        system_prompt_extra: Extra instructions appended to system prompt.
        timeout_seconds:   Total wall-clock timeout for the sub-agent.
    """
    task: str
    tool_names: tuple[str, ...] = ()
    max_iterations: int = 10
    profile: SubAgentProfile = "general"
    system_prompt_extra: str = ""
    timeout_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class SubAgentResult:
    """Result from a completed SubAgent run."""
    agent_id: str
    status: SubAgentStatus
    exit_reason: str
    summary: str
    iteration_count: int
    started_at: datetime
    finished_at: datetime
    error: str | None = None
