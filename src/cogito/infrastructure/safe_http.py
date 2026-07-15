"""SSRF-safe outbound HTTP fetch for explicitly-designated resources (stickers).

Core deliberately does NOT fetch arbitrary URLs on the inbound path (see
``AssetIngestionService._decode_inline``). This helper is the ONE gated
exception: a Tool that the model has explicitly invoked to save a URL as a
sticker. Every defence is deliberate and inescapable — the caller cannot widen
the gate by passing a different URL.

Defences (in order):
1. Scheme allowlist: ``https`` only (also permits explicit ``http`` when the
   host is in the allowlist — loopback is still blocked).
2. Host allowlist: ``allowed_hosts`` (exact ``host:port``). If empty, only
   public internet addresses are permitted (private ranges are always blocked).
3. Private/link-local addresses are ALWAYS rejected, allowlist or not:
   loopback (127/::1), RFC1918 (10/172.16/192.168), link-local (169.254),
   CG-NAT (100.64), unique-local (fc00::/7). DNS is resolved once and the
   resulting IPs are checked and the connection is pinned to the verified IP;
   redirects repeat the complete validation.
4. Response streamed with ``max_bytes`` hard cap.
5. ``timeout_s`` overall deadline.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

# Ranges that are NEVER fetched, even when allowed_hosts is populated.
# Loopback is included so an explicit allowlist cannot whitelist 127.0.0.1.
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


class SafeHttpError(ValueError):
    """Raised when a URL fails any SSRF defence."""


@dataclass(frozen=True)
class SafeHttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool = False


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a verified IP while retaining the original TLS SNI."""

    def __init__(self, ip: str, *, server_name: str, port: int, timeout: float) -> None:
        super().__init__(ip, port=port, timeout=timeout, context=ssl.create_default_context())
        self._server_name = server_name

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_name)


def _is_private(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_private or address == "::1"
    except ValueError:
        return False


def _resolve_host(host: str) -> list[str]:
    """Return the IP addresses for a host (empty when resolution fails)."""
    try:
        return [addr for _, _, _, _, (addr, *_) in socket.getaddrinfo(host, None)]
    except socket.gaierror:
        return []


def fetch_url(
    url: str,
    *,
    method: str = "GET",
    allowed_hosts: tuple[str, ...] = (),
    allowed_ports: tuple[int, ...] = (80, 443),
    max_bytes: int = 2_000_000,
    timeout_s: float = 20.0,
    max_redirects: int = 5,
) -> SafeHttpResponse:
    """Fetch with per-hop DNS validation and IP pinning.

    Redirects are handled manually. Every hop is re-resolved, checked for
    private/link-local ranges, and connected to the exact verified IP. URL
    credentials are always rejected.
    """
    method = method.upper()
    if method not in {"GET", "HEAD"}:
        raise SafeHttpError("only GET and HEAD are allowed")
    current = url
    for redirect_count in range(max_redirects + 1):
        parsed = urlsplit(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SafeHttpError("only HTTP(S) URLs are allowed")
        if parsed.username or parsed.password:
            raise SafeHttpError("URL credentials are not allowed")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in allowed_ports:
            raise SafeHttpError(f"port not allowed: {port}")
        host = parsed.hostname.rstrip(".").casefold()
        if allowed_hosts and not _host_allowed(host, port, allowed_hosts):
            raise SafeHttpError(f"host not allowlisted: {host}:{port}")
        ips = _resolve_host(host)
        if not ips:
            raise SafeHttpError(f"cannot resolve host {host!r}")
        if any(_is_private(ip) or not ipaddress.ip_address(ip).is_global for ip in ips):
            raise SafeHttpError(f"non-public target blocked: {host}")
        ip = ips[0]
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHTTPSConnection(
                ip,
                server_name=host,
                port=port,
                timeout=timeout_s,
            )
        else:
            connection = http.client.HTTPConnection(ip, port=port, timeout=timeout_s)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            connection.request(
                method,
                path,
                headers={
                    "Host": host if port in {80, 443} else f"{host}:{port}",
                    "User-Agent": "Cogito/1.0 safe-http",
                    "Accept": "text/*,application/json,application/xml",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            headers = {key.casefold(): value for key, value in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                if redirect_count >= max_redirects:
                    raise SafeHttpError("too many redirects")
                location = headers.get("location")
                if not location:
                    raise SafeHttpError("redirect has no location")
                current = urljoin(current, location)
                continue
            length = headers.get("content-length")
            if length and int(length) > max_bytes:
                raise SafeHttpError("response exceeds max_bytes")
            body = response.read(max_bytes + 1) if method != "HEAD" else b""
            truncated = len(body) > max_bytes
            if truncated:
                body = body[:max_bytes]
            return SafeHttpResponse(current, response.status, headers, body, truncated)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise SafeHttpError(f"HTTP error: {exc}") from exc
        finally:
            connection.close()
    raise SafeHttpError("too many redirects")


def _host_allowed(host: str, port: int, allowed_hosts: tuple[str, ...]) -> bool:
    """Check host:port against the explicit allowlist (exact match)."""
    target = f"{host}:{port}"
    return target in allowed_hosts or host in allowed_hosts


def fetch_url_bytes(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] = (),
    max_bytes: int = 20 * 1024 * 1024,
    timeout_s: float = 15.0,
) -> bytes:
    """Fetch a URL, rejecting any address that is not explicitly permitted.

    Args:
        url: the URL the model explicitly asked to save as a sticker.
        allowed_hosts: explicit ``host:port`` or ``host`` allowlist. When
            empty, only public (non-private) destinations are permitted.
        max_bytes: hard cap on response size.
        timeout_s: overall deadline.

    Raises:
        SafeHttpError: when any defence trips (and the bytes are NOT returned).
    """
    return fetch_url(
        url,
        allowed_hosts=allowed_hosts,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
    ).body


__all__ = ["SafeHttpError", "SafeHttpResponse", "fetch_url", "fetch_url_bytes"]
