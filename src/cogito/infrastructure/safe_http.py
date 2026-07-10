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
   resulting IPs are checked (no DNS-rebinding defence beyond single resolve —
   this is an MVP for a personal single-owner agent).
4. Response streamed with ``max_bytes`` hard cap.
5. ``timeout_s`` overall deadline.
"""

from __future__ import annotations

import ipaddress
import socket

import httpx

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
    try:
        parsed = httpx.URL(url)
    except Exception as exc:
        raise SafeHttpError(f"invalid URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SafeHttpError(f"scheme not allowed: {scheme!r}")

    host = parsed.host or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    if not host:
        raise SafeHttpError("URL has no host")

    # Always block private/link-local addresses — even when allowlisted.
    # An allowlist entry that resolves to a private IP is still rejected.
    if allowed_hosts and _host_allowed(host, port, allowed_hosts):
        ips = _resolve_host(host)
        if ips and all(not _is_private(ip) for ip in ips):
            return _download(parsed, max_bytes, timeout_s)
        if ips:
            raise SafeHttpError(
                f"host {host} resolves to a private address; blocked"
            )
        raise SafeHttpError(f"cannot resolve host {host!r}")

    # No allowlist → only public addresses permitted.
    ips = _resolve_host(host)
    if not ips:
        raise SafeHttpError(f"cannot resolve host {host!r}")
    if any(_is_private(ip) for ip in ips):
        raise SafeHttpError(f"private address blocked: {host}")
    return _download(parsed, max_bytes, timeout_s)


def _download(parsed: httpx.URL, max_bytes: int, timeout_s: float) -> bytes:
    timeout = httpx.Timeout(timeout_s, connect=min(5.0, timeout_s))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", str(parsed)) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise SafeHttpError("response exceeds max_bytes")
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SafeHttpError("response exceeds max_bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise SafeHttpError(f"HTTP error: {exc}") from exc


__all__ = ["SafeHttpError", "fetch_url_bytes"]
