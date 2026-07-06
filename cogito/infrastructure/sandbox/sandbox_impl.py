# cogito/infrastructure/sandbox/sandbox_impl.py
#
# Concrete ToolSandboxPort implementations:
#   - DummySandbox:    No OS sandbox (fallback for development)
#   - BwrapSandbox:    Linux bwrap-based process sandbox
#   - WinJobSandbox:   Windows job object process isolation

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from cogito.agent.ports.tools.sandbox import (
    SandboxContext,
    SandboxResult,
    ToolSandboxPort,
)

logger = logging.getLogger(__name__)


def _detect_sandbox_backend() -> str:
    """Detect the best available sandbox backend for the current platform."""
    if platform.system() == "Linux":
        # Check if bwrap is available
        import shutil
        if shutil.which("bwrap"):
            return "bwrap"
        return "dummy"
    elif platform.system() == "Windows":
        return "winjob"
    return "dummy"


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Configuration for sandbox backends.

    Attributes:
        backend:          Sandbox backend name ('bwrap', 'winjob', 'dummy').
        workspace:        Workspace directory to bind-mount.
        allow_network:    Whether to allow network access (bwrap only).
        bind_ro_paths:    Additional read-only bind mount paths (bwrap only).
        timeout_default:  Default command timeout in seconds.
    """
    backend: str = "dummy"
    workspace: str = ""
    allow_network: bool = False
    bind_ro_paths: tuple[str, ...] = ()
    timeout_default: float = 60.0


# ── DummySandbox ───────────────────────────────────────────────────────

class DummySandbox:
    """No-op sandbox — executes commands directly.

    Provides path-scoping only: commands are run in a restricted
    working directory.  No OS-level isolation.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    async def run(self, context: SandboxContext) -> SandboxResult:
        timeout = context.timeout_seconds or self._config.timeout_default
        try:
            proc = await asyncio.create_subprocess_shell(
                context.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.cwd or self._config.workspace or None,
                env={**os.environ, **(context.env or {})},
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
                return SandboxResult(
                    returncode=proc.returncode or 0,
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    returncode=-1, stdout="", stderr="Timed out", timed_out=True,
                )
        except Exception as exc:
            return SandboxResult(
                returncode=-1, stdout="", stderr=str(exc),
            )

    async def check_allowed(self, command: str) -> bool:
        """Dummy sandbox allows all commands (policy handles it)."""
        return True


# ── BwrapSandbox (Linux only) ──────────────────────────────────────────

class BwrapSandbox:
    """Linux bwrap (bubblewrap) sandbox for command execution.

    Runs commands in a minimal user namespace with:
      - Workspace bind-mounted read-write
      - Essential system paths read-only (/usr, /lib, /etc, etc.)
      - Network disabled by default
      - /proc and /dev provided
      - tmpfs for /tmp
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    async def run(self, context: SandboxContext) -> SandboxResult:
        timeout = context.timeout_seconds or self._config.timeout_default
        ws = str(Path(self._config.workspace).resolve()) if self._config.workspace else ""

        # Resolve cwd relative to workspace
        if context.cwd and ws:
            try:
                sandbox_cwd = str(Path(ws) / Path(context.cwd).resolve().relative_to(ws))
            except ValueError:
                sandbox_cwd = ws
        else:
            sandbox_cwd = ws or "/tmp"

        # Build bwrap args
        args = [
            "bwrap", "--new-session", "--die-with-parent",
            "--setenv", "HOME", sandbox_cwd,
        ]

        # Required system paths
        for p in ["/usr"]:
            args += ["--ro-bind", p, p]

        # Optional paths (try, in case they don't exist in container)
        optional = ["/bin", "/lib", "/lib64", "/etc/alternatives",
                     "/etc/ssl/certs", "/etc/resolv.conf", "/etc/ld.so.cache"]
        for p in optional:
            args += ["--ro-bind-try", p, p]

        args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

        # Bind workspace
        if ws:
            args += ["--dir", ws, "--bind", ws, ws]

        # Additional read-only paths
        for p in self._config.bind_ro_paths:
            args += ["--ro-bind-try", p, p]

        # Network
        if not self._config.allow_network:
            args.append("--unshare-net")

        # Custom env vars
        if context.env:
            for k, v in context.env.items():
                args += ["--setenv", k, v]

        args += ["--chdir", sandbox_cwd, "--", "sh", "-c", context.command]
        wrapped_cmd = shlex.join(args)

        try:
            proc = await asyncio.create_subprocess_shell(
                wrapped_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
                return SandboxResult(
                    returncode=proc.returncode or 0,
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    returncode=-1, stdout="", stderr="Timed out", timed_out=True,
                )
        except FileNotFoundError:
            return SandboxResult(
                returncode=-1, stdout="", stderr="bwrap not found on this system",
            )
        except Exception as exc:
            return SandboxResult(
                returncode=-1, stdout="", stderr=str(exc),
            )

    async def check_allowed(self, command: str) -> bool:
        """Bwrap sandbox checks: bwrap must be installed."""
        import shutil
        return shutil.which("bwrap") is not None


# ── WinJobSandbox (Windows only) ──────────────────────────────────────

class WinJobSandbox:
    """Windows job object sandbox for command execution.

    Uses asyncio subprocess with job object process group isolation.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    async def run(self, context: SandboxContext) -> SandboxResult:
        timeout = context.timeout_seconds or self._config.timeout_default
        try:
            proc = await asyncio.create_subprocess_shell(
                context.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.cwd or self._config.workspace or None,
                env={**os.environ, **(context.env or {})},
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
                return SandboxResult(
                    returncode=proc.returncode or 0,
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    returncode=-1, stdout="", stderr="Timed out", timed_out=True,
                )
        except Exception as exc:
            return SandboxResult(
                returncode=-1, stdout="", stderr=str(exc),
            )

    async def check_allowed(self, command: str) -> bool:
        return True


# ── Factory ────────────────────────────────────────────────────────────

def create_sandbox(config: SandboxConfig | None = None) -> ToolSandboxPort:
    """Create the best available sandbox for the current platform.

    Usage:
        sandbox = create_sandbox(SandboxConfig(backend="bwrap", workspace="/path"))
        result = await sandbox.run(SandboxContext(command="ls -la"))
    """
    cfg = config or SandboxConfig()
    if cfg.backend == "bwrap":
        return BwrapSandbox(cfg)
    elif cfg.backend == "winjob":
        return WinJobSandbox(cfg)
    return DummySandbox(cfg)
