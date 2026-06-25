# cogito/agent/tools/builtin/background_task.py
#
# Built-in tools: task_output, task_stop — manage long-running shell tasks.
#
# Works with ShellHandler's run_in_background mode.

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Mapping
from uuid import uuid4

from cogito.agent.domain.tools import (
    ToolConcurrencyMode,
    ToolDefinition,
    ToolKind,
    ToolLimits,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)

logger = logging.getLogger(__name__)


# ── Background task registry (singleton) ───────────────────────────────

@dataclass
class BackgroundTask:
    """State of a single background task."""
    task_id: str
    command: str
    created_at: float
    process: asyncio.subprocess.Process | None = None
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    completed: bool = False
    timed_out: bool = False
    cancelled: bool = False


class BackgroundTaskRegistry:
    """Registry of all running background tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = asyncio.Lock()

    async def register(self, command: str) -> BackgroundTask:
        """Create and register a new background task."""
        task = BackgroundTask(
            task_id=uuid4().hex[:12],
            command=command,
            created_at=time.time(),
        )
        async with self._lock:
            self._tasks[task.task_id] = task
        return task

    async def get(self, task_id: str) -> BackgroundTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def remove(self, task_id: str) -> None:
        async with self._lock:
            self._tasks.pop(task_id, None)

    async def list_active(self) -> list[BackgroundTask]:
        async with self._lock:
            return [t for t in self._tasks.values() if not t.completed]

    async def update(self, task_id: str, **kwargs) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task:
                for k, v in kwargs.items():
                    setattr(task, k, v)

    async def cleanup_expired(self, max_age: float = 3600.0) -> int:
        """Remove tasks older than max_age seconds. Returns count removed."""
        now = time.time()
        async with self._lock:
            expired = [tid for tid, t in self._tasks.items() if now - t.created_at > max_age]
            for tid in expired:
                self._tasks.pop(tid, None)
        return len(expired)


# Global singleton
_task_registry = BackgroundTaskRegistry()


def get_task_registry() -> BackgroundTaskRegistry:
    return _task_registry


# ── Task output tool ───────────────────────────────────────────────────

class TaskOutputHandler:
    """Handler for task_output — polls output of a background task."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="task_output",
            description="Get the current output and status of a background task. Returns what has been collected so far.",
            input_schema={
                "type": "object", "properties": {
                    "task_id": {"type": "string", "minLength": 1, "description": "Task ID from shell's run_in_background"},
                },
                "required": ["task_id"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE, risk_level=ToolRiskLevel.LOW,
            timeout_seconds=10.0, idempotent=True, parallel_safe=True,
            kind=ToolKind.READ, risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        task_id = str(arguments.get("task_id", ""))
        registry = get_task_registry()
        task = await registry.get(task_id)
        if task is None:
            return {"error": {"code": "TASK_NOT_FOUND", "message": f"Background task not found: {task_id}"}}
        return {
            "task_id": task.task_id,
            "command": task.command,
            "stdout": task.stdout[-50_000:] if task.stdout else "",
            "stderr": task.stderr[-10_000:] if task.stderr else "",
            "returncode": task.returncode,
            "completed": task.completed,
            "timed_out": task.timed_out,
            "cancelled": task.cancelled,
            "created_at": task.created_at,
        }


# ── Task stop tool ─────────────────────────────────────────────────────

class TaskStopHandler:
    """Handler for task_stop — terminates a running background task."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="task_stop",
            description="Stop a running background task by its task_id. Kills the process if still running.",
            input_schema={
                "type": "object", "properties": {
                    "task_id": {"type": "string", "minLength": 1, "description": "Task ID to stop"},
                },
                "required": ["task_id"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.LOCAL_MUTATION, risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=10.0, idempotent=False, parallel_safe=False,
            kind=ToolKind.EDIT, risk=ToolRisk.LOCAL_WRITE,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_SESSION,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        task_id = str(arguments.get("task_id", ""))
        registry = get_task_registry()
        task = await registry.get(task_id)
        if task is None:
            return {"error": {"code": "TASK_NOT_FOUND", "message": f"Background task not found: {task_id}"}}

        if task.completed:
            return {"task_id": task_id, "already_completed": True, "returncode": task.returncode}

        # Kill the process
        if task.process and task.process.returncode is None:
            try:
                task.process.kill()
                await task.process.wait()
            except ProcessLookupError:
                pass
            except Exception as exc:
                logger.warning("Error stopping task %s: %s", task_id, exc)

        task.cancelled = True
        task.completed = True
        task.returncode = task.process.returncode if task.process else -1
        await registry.update(task_id, completed=True, cancelled=True, returncode=task.returncode)

        return {
            "task_id": task_id,
            "stopped": True,
            "returncode": task.returncode,
        }
