"""Tests for DefaultNetworkPolicy — SSRF prevention."""

from __future__ import annotations

import pytest

from cogito.infrastructure.sandbox.network_policy import DefaultNetworkPolicy, NetworkPolicyConfig


class TestDefaultNetworkPolicy:
    def test_https_url_allowed(self) -> None:
        """HTTPS URLs pass pre-connection check."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://api.example.com/v1/query")
        assert result.allowed is True

    def test_http_scheme_denied_by_default(self) -> None:
        """HTTP is denied by default (only HTTPS allowed)."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("http://example.com")
        assert result.allowed is False

    def test_http_allowed_with_config(self) -> None:
        """HTTP can be enabled via config."""
        config = NetworkPolicyConfig(allowed_schemes=frozenset({"https", "http"}))
        policy = DefaultNetworkPolicy(config)
        result = policy.check_url("http://example.com")
        assert result.allowed is True

    def test_ftp_denied(self) -> None:
        """FTP scheme is always denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("ftp://ftp.example.com")
        assert result.allowed is False

    def test_file_scheme_denied(self) -> None:
        """file:// scheme is denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("file:///etc/passwd")
        assert result.allowed is False

    def test_loopback_ip_denied(self) -> None:
        """127.0.0.1 / localhost is denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://127.0.0.1/admin")
        assert result.allowed is False
        result = policy.check_url("https://localhost/admin")
        assert result.allowed is False

    def test_rfc1918_private_denied(self) -> None:
        """Private RFC1918 addresses are denied."""
        policy = DefaultNetworkPolicy()
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            result = policy.check_url(f"https://{ip}/")
            assert result.allowed is False

    def test_link_local_denied(self) -> None:
        """Link-local addresses are denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://169.254.169.254/latest/meta-data/")
        assert result.allowed is False

    def test_cloud_metadata_rejected(self) -> None:
        """AWS/GCP metadata IP is denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://169.254.169.254/")
        assert result.allowed is False

    def test_blocked_port_denied(self) -> None:
        """Explicitly blocked ports are denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://example.com:22/")
        assert result.allowed is False

    def test_allowed_port_passes(self) -> None:
        """Allowed ports work."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://example.com:443/")
        assert result.allowed is True

    def test_embedded_credentials_denied(self) -> None:
        """URLs with embedded user:password are denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://user:pass@example.com/")
        assert result.allowed is False

    def test_empty_url_denied(self) -> None:
        """Empty URLs are denied."""
        policy = DefaultNetworkPolicy()
        assert policy.check_url("").allowed is False

    def test_resolved_ip_check(self) -> None:
        """check_resolved_ip validates IPs correctly."""
        policy = DefaultNetworkPolicy()
        assert policy.check_resolved_ip("93.184.216.34").allowed is True
        assert policy.check_resolved_ip("127.0.0.1").allowed is False
        assert policy.check_resolved_ip("10.0.0.5").allowed is False

    def test_ipv6_local_denied(self) -> None:
        """IPv6 loopback is denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://[::1]/admin")
        assert result.allowed is False

    def test_redirect_cross_host(self) -> None:
        """Cross-host redirects are re-checked."""
        policy = DefaultNetworkPolicy()
        result = policy.check_redirect(
            "https://example.com/old",
            "https://10.0.0.1/evil",
        )
        assert result.allowed is False

    def test_cgnat_denied(self) -> None:
        """CGNAT range (100.64.0.0/10) is denied."""
        policy = DefaultNetworkPolicy()
        result = policy.check_url("https://100.64.0.1/")
        assert result.allowed is False
