"""Subprocess lifecycle and permission gate for third-party plugins."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PluginPermissionError(PermissionError):
    pass


class PluginPolicyAdapter:
    """Map manifest permissions to explicit host grants."""

    _ALIASES = {
        "fs.read": "filesystem.read",
        "fs.write": "filesystem.write",
        "filesystem:read": "filesystem.read",
        "filesystem:write": "filesystem.write",
        "network:outbound": "network.connect",
    }

    def __init__(self, granted_permissions: set[str] | None = None) -> None:
        self._grants = frozenset(granted_permissions or set())

    def authorize(self, permissions: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(self._ALIASES.get(p, p) for p in permissions)
        denied = [p for p in normalized if not self._is_granted(p)]
        if denied:
            raise PluginPermissionError(
                "Plugin permissions not granted: " + ", ".join(sorted(denied))
            )
        return normalized

    def _is_granted(self, permission: str) -> bool:
        if permission in self._grants or "*" in self._grants:
            return True
        return any(
            grant.endswith(".*") and permission.startswith(grant[:-1])
            for grant in self._grants
        )


@dataclass
class PluginProcess:
    plugin_id: str
    process: subprocess.Popen[str]


class PluginProcessSupervisor:
    """Start, supervise, and terminate isolated plugin host processes."""

    def __init__(
        self, *, startup_timeout_s: float = 5.0, stop_timeout_s: float = 3.0,
    ) -> None:
        self._startup_timeout_s = startup_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._processes: dict[str, PluginProcess] = {}

    def start(self, manifest: Any) -> int:
        existing = self._processes.get(manifest.plugin_id)
        if existing and existing.process.poll() is None:
            return int(existing.process.pid)
        if not manifest.entry_point or not manifest.source_path:
            raise ValueError("subprocess plugin requires entry_point and source_path")

        command = [
            sys.executable,
            "-m",
            "cogito.capability.plugin_host",
            "--source-path",
            str(Path(manifest.source_path).resolve()),
            "--entry-point",
            manifest.entry_point,
            "--plugin-id",
            manifest.plugin_id,
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=str(Path(manifest.source_path).resolve()),
            env=self._minimal_env(manifest.source_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            shell=False,
            creationflags=creationflags,
        )
        ready_queue: queue.Queue[str] = queue.Queue(maxsize=1)

        def _read_ready() -> None:
            line = process.stdout.readline() if process.stdout else ""
            ready_queue.put(line)

        threading.Thread(target=_read_ready, daemon=True).start()
        try:
            line = ready_queue.get(timeout=self._startup_timeout_s)
        except queue.Empty:
            self._terminate(process)
            raise TimeoutError(f"plugin {manifest.plugin_id} startup timed out") from None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"status": "error", "error_code": "invalid_handshake"}
        if payload.get("status") != "ready":
            self._terminate(process)
            raise RuntimeError(
                f"plugin {manifest.plugin_id} failed startup: "
                f"{payload.get('error_code', 'unknown')}"
            )
        self._processes[manifest.plugin_id] = PluginProcess(manifest.plugin_id, process)
        return int(process.pid)

    def stop(self, plugin_id: str) -> None:
        managed = self._processes.pop(plugin_id, None)
        if managed is None:
            return
        process = managed.process
        if process.poll() is not None:
            return
        try:
            if process.stdin:
                process.stdin.write("stop\n")
                process.stdin.flush()
            process.wait(timeout=self._stop_timeout_s)
        except Exception:
            self._terminate(process)

    def health(self, plugin_id: str) -> dict[str, Any]:
        managed = self._processes.get(plugin_id)
        if managed is None:
            return {"status": "stopped", "pid": None}
        code = managed.process.poll()
        return {
            "status": "running" if code is None else "crashed",
            "pid": managed.process.pid,
            "exit_code": code,
        }

    def close(self) -> None:
        for plugin_id in list(self._processes):
            self.stop(plugin_id)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)

    @staticmethod
    def _minimal_env(source_path: str) -> dict[str, str]:
        allowed = (
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
        )
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        src_root = str(Path(__file__).resolve().parents[2])
        plugin_parent = str(Path(source_path).resolve().parent)
        env["PYTHONPATH"] = os.pathsep.join((src_root, plugin_parent))
        env["PYTHONUNBUFFERED"] = "1"
        return env


__all__ = [
    "PluginPermissionError",
    "PluginPolicyAdapter",
    "PluginProcessSupervisor",
]
