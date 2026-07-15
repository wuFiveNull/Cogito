"""MCP Client — 连接 MCP Server 并调用工具。

使用官方 mcp SDK。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cogito.capability.mcp import MCPServerConfig

# ── 结构化结果 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MCPCallResult:
    """MCP Tool 调用的结构化结果。

    - structured_content 始终为 JSON 对象（dict），否则为 None
    - text_content 始终为字符串（展示用）
    - MCP 数据天然不可信，trust_label 固定为 external_untrusted
    """

    server_name: str
    tool_name: str
    structured_content: dict[str, Any] | list[Any] | None
    text_content: str
    is_error: bool
    raw_size_bytes: int = 0
    schema_hash: str = ""
    trust_label: str = "external_untrusted"


class MCPResultError(RuntimeError):
    """MCP 调用失败或输出超限。"""


class MCPClient:
    """MCP Server 连接客户端。

    支持 stdio 和 SSE（streamable HTTP）传输。
    """

    def __init__(
        self,
        server_name: str,
        config: MCPServerConfig,
        *,
        sampling_callback: Any | None = None,
        change_callback: Any | None = None,
    ) -> None:
        self._server_name = server_name
        self._config = config
        self._session = None
        self._stdio_ctx = None
        self._sse_ctx = None
        self._http_ctx = None
        self._process = None
        self._tools: list[dict[str, Any]] = []
        self._connected = False
        self._auth = None
        self._sampling_callback = sampling_callback
        self._change_callback = change_callback
        self._sampling_scope: ContextVar[str] = ContextVar(
            f"mcp_sampling_scope_{server_name}_{id(self)}",
            default="connector",
        )

    async def start(self) -> None:
        """启动并初始化 MCP 连接。"""
        if self._connected:
            return

        if self._config.transport == "stdio":
            await self._start_stdio()
        elif self._config.transport == "sse":
            await self._start_sse()
        elif self._config.transport == "streamable_http":
            await self._start_streamable_http()
        else:
            raise ValueError(f"Unknown transport: {self._config.transport}")

    async def _start_stdio(self) -> None:
        """通过 stdio 启动 MCP Server。

        兼容新版 mcp SDK：stdio_client 已经是 @asynccontextmanager，
        必须用 async with 获取 (read, write)，不能用旧式 await。
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command, args, cwd, env = self._stdio_parameters()
        params = StdioServerParameters(command=command, args=args, cwd=cwd, env=env)

        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()

        # mcp >= 1.x：ClientSession(read, write, read_timeout_seconds?, ...)
        # 不再接受 ClientCapabilities 作为第三位参数
        self._session = self._new_session(ClientSession, read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        self._connected = True

    async def _start_sse(self) -> None:
        """通过 SSE 连接 MCP Server。"""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        self._validate_remote_url(self._config.url)
        auth = self._build_oauth_provider()
        self._sse_ctx = sse_client(
            url=self._config.url,
            headers=self._resolved_headers(),
            timeout=self._config.timeout_seconds,
            auth=auth,
            httpx_client_factory=_secure_httpx_client_factory,
        )
        read, write = await self._sse_ctx.__aenter__()

        self._session = self._new_session(ClientSession, read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        self._connected = True

    async def _start_streamable_http(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._validate_remote_url(self._config.url)
        auth = self._build_oauth_provider()
        self._http_ctx = streamablehttp_client(
            self._config.url,
            headers=self._resolved_headers(),
            timeout=self._config.timeout_seconds,
            auth=auth,
            httpx_client_factory=_secure_httpx_client_factory,
        )
        streams = await self._http_ctx.__aenter__()
        read, write = streams[0], streams[1]
        self._session = self._new_session(ClientSession, read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        self._connected = True

    async def list_tools(self) -> list[dict[str, Any]]:
        """获取 Server 的工具列表。"""
        if not self._session:
            return []

        result = await self._session.list_tools()
        tools = []
        for tool in result.tools:
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                    "output_schema": getattr(tool, "outputSchema", None),
                }
            )
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用 MCP 工具并返回结果文本。"""
        result = await self.call_tool_structured(name, arguments)
        return result.text_content

    async def call_tool_structured(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        max_output_bytes: int = 1048576,
        sampling_scope: str = "",
    ) -> MCPCallResult:
        """调用 MCP 工具并返回结构化结果。

        - 解析 text content 为 JSON；解析失败仍返回合法的原始文本
        - 输出大小超限抛出 MCPResultError（不入库）
        - schema_hash 使用完整 content 的 SHA-256（供 Connector 数据漂移审计）
        """
        if not self._session:
            raise RuntimeError("MCP client not connected")

        token = self._sampling_scope.set(sampling_scope or "connector")
        try:
            result = await self._session.call_tool(name, arguments)
        finally:
            self._sampling_scope.reset(token)

        parts = []
        for content in result.content:
            if hasattr(content, "text"):
                parts.append(content.text)
            else:
                parts.append(str(content))

        text = "\n".join(parts)
        raw_bytes = len(text.encode("utf-8"))

        if raw_bytes > max_output_bytes:
            raise MCPResultError(
                f"tool output too large: {raw_bytes} bytes (limit {max_output_bytes})",
            )

        structured: dict[str, Any] | list[Any] | None = None
        try:
            structured = json.loads(text)
        except (ValueError, TypeError):
            structured = None

        schema_hash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

        return MCPCallResult(
            server_name=self._server_name,
            tool_name=name,
            structured_content=structured,
            text_content=text,
            is_error=bool(result.isError),
            raw_size_bytes=raw_bytes,
            schema_hash=schema_hash,
        )

    async def tool_info(self, name: str) -> dict[str, Any] | None:
        """获取单个 Tool 的 Schema 信息（用于审计）。"""
        for t in self._tools:
            if t["name"] == name:
                return t
        return None

    async def list_resources(self) -> list[dict[str, Any]]:
        if not self._session or not self._config.allow_resources:
            return []
        result = await self._session.list_resources()
        return [
            {
                "uri": str(item.uri),
                "name": item.name,
                "description": item.description or "",
                "mime_type": item.mimeType or "",
            }
            for item in result.resources
        ]

    async def read_resource(self, uri: str) -> str:
        if not self._session or not self._config.allow_resources:
            raise PermissionError("MCP resources are disabled for this server")
        result = await self._session.read_resource(uri)
        parts = []
        for item in result.contents:
            parts.append(getattr(item, "text", None) or str(getattr(item, "blob", "")))
        return "\n".join(parts)[: self._config.max_output_chars]

    async def list_prompts(self) -> list[dict[str, Any]]:
        if not self._session or not self._config.allow_prompts:
            return []
        result = await self._session.list_prompts()
        return [
            {
                "name": item.name,
                "description": item.description or "",
                "arguments": [getattr(arg, "name", "") for arg in (item.arguments or [])],
            }
            for item in result.prompts
        ]

    async def get_prompt(self, name: str, arguments: dict[str, str]) -> str:
        if not self._session or not self._config.allow_prompts:
            raise PermissionError("MCP prompts are disabled for this server")
        result = await self._session.get_prompt(name, arguments)
        parts = []
        for message in result.messages:
            content = message.content
            parts.append(getattr(content, "text", None) or str(content))
        return "\n".join(parts)[: self._config.max_output_chars]

    @property
    def tools_info(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def health(self) -> bool:
        """检查连接是否健康。"""
        if not self._session or not self._connected:
            return False
        try:
            await self._session.send_ping()
            return True
        except Exception:
            return False

    async def stop(self) -> None:
        """关闭连接。

        退出顺序：session → stdio/sse 上下文。
        stdio_client 的 anyio task_group 可能在跨任务 __aexit__ 时抛 RuntimeError
        （"Attempted to exit cancel scope in a different task"），此类错误不影响
        资源回收，静默忽略。子进程由 asyncio 子进程管理层随 stdio_ctx 退出而终止。
        """
        self._connected = False
        if self._session:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._stdio_ctx is not None:
            try:
                await self._stdio_ctx.__aexit__(None, None, None)
            except RuntimeError:
                # anyio cancel scope 跨任务 —— 子进程管理层随 GC 回收
                pass
            except Exception:
                pass
            self._stdio_ctx = None
        if self._sse_ctx is not None:
            try:
                await self._sse_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._sse_ctx = None
        if self._http_ctx is not None:
            try:
                await self._http_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._http_ctx = None

    @property
    def connected(self) -> bool:
        return self._connected

    def _resolved_headers(self) -> dict[str, str]:
        return {key: _resolve_secret(value) for key, value in self._config.headers.items()}

    def _build_oauth_provider(self) -> Any | None:
        if not self._config.oauth_enabled:
            return None
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        token_path = (
            Path(
                self._config.oauth_token_file
                or Path(self._config.secret_root) / f"{self._server_name}.json"
            )
            .expanduser()
            .resolve()
        )
        if not self._config.secret_root:
            raise ValueError("OAuth requires an explicit MCP secret_root")
        secret_root = Path(self._config.secret_root).expanduser().resolve()
        try:
            token_path.relative_to(secret_root)
        except ValueError as exc:
            raise ValueError("OAuth token file must be inside MCP secret_root") from exc
        self._auth = OAuthClientProvider(
            self._config.url,
            OAuthClientMetadata(
                redirect_uris=[self._config.oauth_redirect_uri],
                scope=self._config.oauth_scope or None,
                client_name=f"Cogito MCP ({self._server_name})",
            ),
            _FileTokenStorage(token_path),
            timeout=max(self._config.timeout_seconds, 60),
        )
        return self._auth

    def _new_session(self, session_type: Any, read: Any, write: Any) -> Any:
        list_roots_callback = None
        if self._config.allow_roots:
            from mcp import types

            async def list_roots_callback(context: Any) -> Any:
                roots = []
                for configured_root in self._config.roots:
                    roots.append(
                        types.Root(
                            uri=Path(configured_root).expanduser().resolve().as_uri(),
                            name="Cogito workspace",
                        )
                    )
                return types.ListRootsResult(roots=roots)

        sampling_callback = None
        if self._sampling_callback is not None and self._config.allow_sampling:
            async def sampling_callback(context: Any, params: Any) -> Any:
                return await self._sampling_callback(
                    context,
                    params,
                    self._sampling_scope.get(),
                )
        return session_type(
            read,
            write,
            list_roots_callback=list_roots_callback,
            sampling_callback=sampling_callback,
            message_handler=self._handle_message,
        )

    async def _handle_message(self, message: Any) -> None:
        if self._change_callback is None or isinstance(message, Exception):
            return
        method = str(getattr(message, "method", ""))
        if method in {
            "notifications/tools/list_changed",
            "notifications/resources/list_changed",
            "notifications/prompts/list_changed",
        }:
            result = self._change_callback(method)
            if hasattr(result, "__await__"):
                await result

    def _stdio_parameters(self) -> tuple[str, list[str], str | None, dict[str, str]]:
        env = {key: _resolve_secret(value) for key, value in self._config.env.items()}
        if self._config.isolation != "host_trusted":
            raise ValueError("stdio MCP requires explicit isolation='host_trusted'")
        # Do not inherit the full parent environment. PATH is the only ambient
        # value retained so explicitly configured executables can be found.
        filtered = {"PATH": os.environ.get("PATH", ""), **env}
        return self._config.command, list(self._config.args), self._config.cwd or None, filtered

    @staticmethod
    def _validate_remote_url(url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP URL must use HTTP(S)")
        if parsed.username or parsed.password:
            raise ValueError("MCP URL credentials are not allowed")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        for info in socket.getaddrinfo(parsed.hostname, port):
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise ValueError("remote MCP URL resolves to a non-public address")


def _resolve_secret(value: str) -> str:
    if value.startswith("env://"):
        return os.environ.get(value[6:], "")
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def _secure_httpx_client_factory(
    headers: dict[str, str] | None = None,
    timeout: Any = None,
    auth: Any = None,
) -> Any:
    import httpx

    return httpx.AsyncClient(
        headers=headers, timeout=timeout, auth=auth,
        follow_redirects=False, trust_env=False,
    )


class _FileTokenStorage:
    """Small atomic TokenStorage implementation for the official MCP SDK."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.path)

    async def get_tokens(self) -> Any | None:
        from mcp.shared.auth import OAuthToken

        value = self._read().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens: Any) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> Any | None:
        from mcp.shared.auth import OAuthClientInformationFull

        value = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info: Any) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)
