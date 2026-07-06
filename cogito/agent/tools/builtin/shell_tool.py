# cogito/agent/tools/builtin/shell_tool.py
#
# Built-in tool: shell — executes shell commands with sandboxing.
#
# Risk: PRIVILEGED — default DENY. Must be explicitly enabled.

from __future__ import annotations

import asyncio
import logging
import os
from typing import Mapping

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
from cogito.infrastructure.sandbox.command_policy import CommandPolicy, CommandPolicyResult
from cogito.infrastructure.sandbox.shell_evasion_guardian import ShellEvasionGuardian
from cogito.agent.tools.builtin.background_task import get_task_registry

logger = logging.getLogger(__name__)


class ShellHandler:
    """Handler for shell — executes shell commands with policy + sandbox."""

    def __init__(
        self,
        *,
        command_policy: CommandPolicy | None = None,
        evasion_guardian: ShellEvasionGuardian | None = None,
        sandbox: object | None = None,
        enabled: bool = False,
    ) -> None:
        self._command_policy = command_policy or CommandPolicy()
        self._evasion_guardian = evasion_guardian
        self._sandbox = sandbox
        self._enabled = enabled

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shell",
            description="Execute a shell command. Only allowed commands are available. High-risk patterns are blocked. Use run_in_background=true for long-running commands.",
            input_schema={
                "type": "object", "properties": {
                    "command": {"type": "string", "minLength": 1, "description": "Shell command to execute"},
                    "cwd": {"type": "string", "description": "Working directory"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                    "run_in_background": {"type": "boolean", "description": "Run in background and return immediately"},
                },
                "required": ["command"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.LOCAL_MUTATION, risk_level=ToolRiskLevel.CRITICAL,
            timeout_seconds=60.0, idempotent=False, parallel_safe=False,
            kind=ToolKind.EXECUTE, risk=ToolRisk.PRIVILEGED,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.EXCLUSIVE,
            limits=ToolLimits(timeout_seconds=60.0, max_result_chars=50_000),
            enabled=self._enabled,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        if not self._enabled:
            return {"error": {"code": "SHELL_DISABLED", "message": "Shell execution is not enabled"}}

        command = str(arguments.get("command", ""))
        cwd = str(arguments.get("cwd", ".")) if arguments.get("cwd") else None
        timeout = int(arguments.get("timeout_seconds", 60))
        run_in_background = arguments.get("run_in_background", False)

        # Command policy check
        if self._command_policy.check(command) is not CommandPolicyResult.ALLOW:
            logger.warning("Shell command denied by policy: %s", command[:100])
            return {"error": {"code": "COMMAND_DENIED", "message": "Command rejected by security policy"}}

        # Shell evasion detection (second layer of defense)
        if self._evasion_guardian is not None:
            evasion = self._evasion_guardian.check(command)
            if evasion.is_evasion:
                logger.warning("Shell command rejected by evasion guard: %s", evasion.reason)
                return {"error": {"code": "SHELL_EVASION_DETECTED", "message": evasion.reason}}

        # Background task mode
        if run_in_background:
            return await self._execute_background(command, cwd)

        # Foreground execution
        return await self._execute_foreground(command, cwd, timeout)

    async def _execute_background(self, command: str, cwd: str | None) -> dict:
        """Execute a command in background mode, return task_id immediately."""
        registry = get_task_registry()
        task = await registry.register(command)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
            )
            task.process = proc

            # Read output in background
            stdout, stderr = await proc.communicate()
            task.stdout = stdout.decode("utf-8", errors="replace")
            task.stderr = stderr.decode("utf-8", errors="replace")
            task.returncode = proc.returncode
            task.completed = True
            await registry.update(task.task_id, completed=True, returncode=proc.returncode,
                                  stdout=task.stdout, stderr=task.stderr)

            logger.info("Background task %s completed: returncode=%d", task.task_id, proc.returncode)
        except Exception as exc:
            task.completed = True
            task.stderr = str(exc)
            await registry.update(task.task_id, completed=True, stderr=str(exc))

        return {
            "task_id": task.task_id,
            "command": command,
            "status": "started",
            "message": "Command is running in the background. Use task_output to check results.",
        }

    async def _execute_foreground(self, command: str, cwd: str | None, timeout: int) -> dict:
        """Execute a command in foreground with timeout and smart truncation."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"stdout": "", "stderr": "Command timed out", "returncode": -1, "timed_out": True}

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")

            # Smart truncation: keep head + tail when over limit
            stdout_text = self._smart_truncate(stdout_text, max_chars=50_000)
            stderr_text = self._smart_truncate(stderr_text, max_chars=10_000)

            # Auto-convert to background if output is very large and command is still running
            # (not possible with communicate, but we keep this for future streaming support)

            return {
                "stdout": stdout_text,
                "stderr": stderr_text,
                "returncode": proc.returncode,
            }
        except Exception as exc:
            return {"error": {"code": "SHELL_ERROR", "message": str(exc)}}

    @staticmethod
    def _smart_truncate(text: str, max_chars: int) -> str:
        """Truncate text intelligently: keep head and tail when over limit."""
        if len(text) <= max_chars:
            return text
        head_len = max_chars // 2
        tail_len = max_chars - head_len - 20  # 20 chars for "[... truncated N chars ...]"
        head = text[:head_len]
        tail = text[-tail_len:] if tail_len > 0 else ""
        notice = f"\n[... truncated {len(text) - max_chars} characters ...]\n"
        return head + notice + tail
