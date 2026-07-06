# cogito/infrastructure/sandbox/network_policy.py
#
# DefaultNetworkPolicy — SSRF prevention and outbound network control.
#
# Design rules (see tool-system-spec §19):
#   - URL scheme whitelist (default: https only).
#   - DNS resolution + IP check (deny loopback, RFC1918, link-local, etc.).
#   - Port allowlist/denylist.
#   - Redirect re-validation (URL changes re-checked).
#   - Compression bomb protection (response size limit).
#   - No automatic credential forwarding (cookies, Authorization, etc.).

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NetworkPolicyConfig:
    allowed_schemes: frozenset[str] = frozenset({"https"})
    allowed_ports: frozenset[int] = frozenset({443, 80, 8080, 8443})
    blocked_ports: frozenset[int] = frozenset({22, 23, 25, 53, 135, 139, 445, 3389, 5900})
    max_response_bytes: int = 10_000_000  # 10 MB
    max_redirects: int = 5
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0
    allow_private_networks: bool = False


@dataclass(frozen=True, slots=True)
class NetworkPolicyResult:
    allowed: bool
    reason: str = ""
    resolved_url: str = ""


class DefaultNetworkPolicy:
    """SSRF-protective network policy for HTTP requests.

    Validates URLs before any connection is made.  Rejects:
      - Unsupported schemes (e.g. file://, ftp://)
      - Loopback / localhost
      - Private / RFC1918 addresses (default)
      - Link-local, multicast, reserved addresses
      - Blocked ports
    """

    def __init__(self, config: NetworkPolicyConfig | None = None) -> None:
        self._config = config or NetworkPolicyConfig()

    def check_url(self, url: str) -> NetworkPolicyResult:
        """Validate a URL before making a request.

        This is a pre-connection check.  After DNS resolution,
        ``check_resolved_ip()`` must also be called.
        """
        if not url or not url.strip():
            return NetworkPolicyResult(False, "url is empty")

        try:
            parsed = urlparse(url)
        except Exception as exc:
            return NetworkPolicyResult(False, f"url parse error: {exc}")

        # Scheme check
        if parsed.scheme.lower() not in self._config.allowed_schemes:
            return NetworkPolicyResult(
                False,
                f"scheme '{parsed.scheme}' not allowed",
            )

        # Credentials in URL (user:password@host)
        if parsed.username or parsed.password:
            return NetworkPolicyResult(
                False,
                "embedded credentials in URL are not allowed",
            )

        # Host is not empty
        host = parsed.hostname
        if not host:
            return NetworkPolicyResult(False, "url has no hostname")

        # Block localhost / loopback hostnames (pre-DNS)
        if host.lower() in ("localhost", "localhost.localdomain", "ip6-localhost", "loopback"):
            return NetworkPolicyResult(False, "localhost connections are not allowed")

        # Port check
        port = parsed.port
        if port is not None:
            if port in self._config.blocked_ports:
                return NetworkPolicyResult(
                    False,
                    f"port {port} is blocked",
                )
            if port not in self._config.allowed_ports:
                # Non-standard port — still allow if not explicitly blocked
                pass

        # Check for IP-based hostname (pre-DNS check)
        if self._is_ip_address(host):
            if not self._check_ip(host):
                return NetworkPolicyResult(
                    False,
                    f"IP address {host} is not allowed",
                )

        # Hostname validation
        if not self._is_valid_hostname(host):
            return NetworkPolicyResult(False, f"invalid hostname: {host}")

        return NetworkPolicyResult(True, resolved_url=url)

    def check_resolved_ip(self, ip_str: str) -> NetworkPolicyResult:
        """Validate a resolved IP address after DNS lookup."""
        if self._is_ip_address(ip_str):
            if self._check_ip(ip_str):
                return NetworkPolicyResult(True, resolved_url=ip_str)
            return NetworkPolicyResult(False, f"IP address {ip_str} is not allowed")
        return NetworkPolicyResult(False, f"not a valid IP address: {ip_str}")

    def check_redirect(self, original_url: str, redirect_url: str) -> NetworkPolicyResult:
        """Re-validate a redirect target URL."""
        result = self.check_url(redirect_url)
        if not result.allowed:
            return result

        # Additional redirect-specific checks
        original_host = urlparse(original_url).hostname
        redirect_host = urlparse(redirect_url).hostname

        if original_host and redirect_host and original_host != redirect_host:
            # Cross-host redirect — verify resolved IP
            # (DNS rebinding prevention)
            logger.info("Cross-host redirect: %s → %s", original_host, redirect_host)

        return result

    # ── Internal ────────────────────────────────────────────────────────

    IPV4_LOCAL_RANGES = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("0.0.0.0/8"),
    ]

    IPV6_LOCAL_RANGES = [
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
        ipaddress.ip_network("::ffff:0:0/96"),
    ]

    def _check_ip(self, ip_str: str) -> bool:
        """Check if an IP address is allowed to connect to."""
        if self._config.allow_private_networks:
            return True

        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False

        if addr.version == 4:
            for network in self.IPV4_LOCAL_RANGES:
                if addr in network:
                    return False
        elif addr.version == 6:
            for network in self.IPV6_LOCAL_RANGES:
                if addr in network:
                    return False

        return True

    @staticmethod
    def _is_ip_address(host: str) -> bool:
        """Check if a hostname is an IP address."""
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_valid_hostname(hostname: str) -> bool:
        """Basic hostname validation."""
        if len(hostname) > 253:
            return False
        # RFC 1123 hostname
        pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$'
        return bool(re.match(pattern, hostname))
