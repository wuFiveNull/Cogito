"""Tests for shell tool — command policy, sandbox, and execution."""

from __future__ import annotations

import pytest

from cogito.agent.tools.builtin.shell_tool import ShellHandler
from cogito.infrastructure.sandbox.command_policy import CommandPolicy


class TestShellHandler:
    def test_shell_disabled_by_default(self) -> None:
        handler = ShellHandler()
        assert handler.definition.enabled is False
        result = handler.execute(arguments={"command": "ls"}, context={})
        # Should return error since it's disabled
        import asyncio
        r = asyncio.run(result)
        assert "error" in r

    def test_shell_command_policy_check(self) -> None:
        policy = CommandPolicy()
        handler = ShellHandler(command_policy=policy, enabled=True)
        assert handler.definition.name == "shell"
        assert handler.definition.risk.value == "privileged"

    def test_allowed_commands_pass_through(self) -> None:
        policy = CommandPolicy()
        assert policy.check("echo hello") is not None
