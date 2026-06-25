# cogito/infrastructure/mcp/transport.py
#
# MCP Transport implementations: stdio, SSE, Streamable HTTP.
#
# Each transport implements the json-rpc 2.0 message exchange pattern:
#   Client sends: {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
#   Server sends: {"jsonrpc":"2.0","id":1,"result":{"tools":[...]}}

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

from cogito.infrastructure.sandbox.network_policy import DefaultNetworkPolicy

logger = logging.getLogger(__name__)


class MCPTransportError(Exception):
    pass


class MCPTransport(ABC):
    """Abstract base for MCP transport implementations."""

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


class StdioMCPTransport(MCPTransport):
    """MCP transport over stdio subprocess.

    On Windows, wraps the command in ``cmd.exe /d /c`` when the binary
    is a shell launcher (npx, npm, .cmd, .bat) to ensure reliable startup.
    """

    def __init__(
        self,
        command: str,
        args: tuple[str, ...] = (),
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        self._command = command
        self._args = args
        self._cwd = cwd
        self._env = env
        self._connect_timeout = connect_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    @staticmethod
    def _normalize_windows_command(command: str) -> tuple[str, tuple[str, ...]]:
        """On Windows, wrap shell launchers in cmd.exe /d /c.

        Reference: nanobot _normalize_windows_stdio_command.
        """
        if os.name != "nt":
            return command, ()

        basename = os.path.basename(command).lower()
        if basename in ("npx", "npm", "pnpm", "yarn", "bunx"):
            return "cmd.exe", ("/d", "/c", command)
        if basename.endswith((".cmd", ".bat")):
            return "cmd.exe", ("/d", "/c", command)
        return command, ()

    async def connect(self) -> None:
        cmd, extra_args = self._normalize_windows_command(self._command)
        all_args = extra_args + self._args
        self._proc = await asyncio.create_subprocess_exec(
            cmd, *all_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        asyncio.create_task(self._read_loop())

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise MCPTransportError("Not connected")
        self._request_id += 1
        req_id = self._request_id
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        line = json.dumps(request, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=self._connect_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise MCPTransportError(f"Request {method} timed out")

    async def close(self) -> None:
        if self._proc:
            self._proc.kill()
            await self._proc.wait()
            self._proc = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPTransportError("Connection closed"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        buffer = ""
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            buffer += line.decode("utf-8")
            while "\n" in buffer:
                msg_line, buffer = buffer.split("\n", 1)
                msg_line = msg_line.strip()
                if not msg_line:
                    continue
                try:
                    msg = json.loads(msg_line)
                    if "id" in msg:
                        future = self._pending.pop(msg["id"], None)
                        if future and not future.done():
                            if "result" in msg:
                                future.set_result(msg["result"])
                            elif "error" in msg:
                                future.set_exception(MCPTransportError(msg["error"].get("message", "Unknown error")))
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning("MCP parse error: %s", exc)


class SSEMCPTransport(MCPTransport):
    """MCP transport over Server-Sent Events.

    Uses an HTTP POST for outgoing requests and an SSE stream for
    incoming responses and notifications.  Validates URLs against
    the network policy (SSRF protection) before connecting.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        network_policy: DefaultNetworkPolicy | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._network_policy = network_policy
        self._connect_timeout = connect_timeout
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None

    async def connect(self) -> None:
        import httpx

        # SSRF validation
        if self._network_policy is not None:
            result = self._network_policy.check_url(self._url)
            if not result.allowed:
                raise MCPTransportError(f"SSRF check failed: {result.reason}")

        # TCP pre-connect probe
        await self._tcp_probe(self._url)

        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=self._connect_timeout,
            follow_redirects=True,
        )
        self._read_task = asyncio.create_task(self._read_sse())

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._client is None:
            raise MCPTransportError("Not connected")
        self._request_id += 1
        req_id = self._request_id
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        # Redirect re-validation: POST and check final URL
        response = await self._client.post(self._url, json=request)
        response.raise_for_status()
        if response.url and str(response.url) != self._url:
            if self._network_policy is not None:
                redirect_result = self._network_policy.check_url(str(response.url))
                if not redirect_result.allowed:
                    self._pending.pop(req_id, None)
                    raise MCPTransportError(f"Redirect SSRF check failed: {redirect_result.reason}")

        try:
            return await asyncio.wait_for(future, timeout=self._connect_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise MCPTransportError(f"Request {method} timed out")

    async def close(self) -> None:
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPTransportError("Connection closed"))
        self._pending.clear()

    async def _read_sse(self) -> None:
        """Read SSE stream for server push (notifications, results)."""
        if self._client is None:
            return
        try:
            async with self._client.stream("GET", self._url) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if not data:
                            continue
                        try:
                            msg = json.loads(data)
                            if "id" in msg:
                                future = self._pending.pop(msg["id"], None)
                                if future and not future.done():
                                    if "result" in msg:
                                        future.set_result(msg["result"])
                                    elif "error" in msg:
                                        future.set_exception(MCPTransportError(str(msg["error"])))
                            else:
                                # This is a notification (no id field)
                                self._handle_notification(msg)
                        except json.JSONDecodeError:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Handle MCP notifications (messages without 'id' field).

        Currently logs; subclasses can override to dispatch to manager.
        """
        method = msg.get("method", "")
        if method == "notifications/tools/list_changed":
            logger.info("MCP notification: tools/list_changed")

    @staticmethod
    async def _tcp_probe(url: str) -> None:
        """Quick TCP pre-connect probe to fail fast on unreachable hosts."""
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            _, _ = await asyncio.wait_for(
                asyncio.get_event_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=3.0,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise MCPTransportError(f"TCP probe failed for {host}:{port}: {exc}")


class StreamableHTTPTransport(MCPTransport):
    """MCP transport over Streamable HTTP (HTTP POST with streaming response).

    Validates URLs against the network policy before connecting.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        network_policy: DefaultNetworkPolicy | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._network_policy = network_policy
        self._connect_timeout = connect_timeout
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def connect(self) -> None:
        import httpx

        # SSRF validation
        if self._network_policy is not None:
            result = self._network_policy.check_url(self._url)
            if not result.allowed:
                raise MCPTransportError(f"SSRF check failed: {result.reason}")

        # TCP pre-connect probe
        await self._tcp_probe(self._url)

        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=self._connect_timeout,
            follow_redirects=True,
        )

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._client is None:
            raise MCPTransportError("Not connected")
        self._request_id += 1
        req_id = self._request_id
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        # Redirect re-validation
        url = self._url
        async with self._client.stream("POST", url, json=request) as response:
            response.raise_for_status()
            if response.url and str(response.url) != url:
                if self._network_policy is not None:
                    redirect_result = self._network_policy.check_url(str(response.url))
                    if not redirect_result.allowed:
                        self._pending.pop(req_id, None)
                        raise MCPTransportError(f"Redirect SSRF check failed: {redirect_result.reason}")
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if "id" in msg:
                        f = self._pending.pop(msg["id"], None)
                        if f and not f.done():
                            if "result" in msg:
                                f.set_result(msg["result"])
                            elif "error" in msg:
                                f.set_exception(MCPTransportError(str(msg["error"])))
                except json.JSONDecodeError:
                    pass
        # Fallback: resolve all remaining pending
        for fid, f in list(self._pending.items()):
            if not f.done():
                f.set_exception(MCPTransportError("Stream ended"))
        self._pending.clear()
        return self._pending.get(req_id, future)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPTransportError("Connection closed"))
        self._pending.clear()

    @staticmethod
    async def _tcp_probe(url: str) -> None:
        """Quick TCP pre-connect probe to fail fast on unreachable hosts."""
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            _, _ = await asyncio.wait_for(
                asyncio.get_event_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=3.0,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise MCPTransportError(f"TCP probe failed for {host}:{port}: {exc}")