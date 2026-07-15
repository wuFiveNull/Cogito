"""PR-C4: Sandbox profiles — Plan 03 M4."""
from __future__ import annotations

import pytest

from cogito.capability.sandbox import (
    PLUGIN_PROCESS,
    READ_ONLY,
    WORKSPACE_WRITE,
    SandboxProfile,
    get_profile,
    validate_profile,
)


def test_read_only_blocks_network_and_shell() -> None:
    assert READ_ONLY.allow_network is False
    assert READ_ONLY.allow_shell is False


def test_workspace_write_allows_workspace() -> None:
    assert "/workspace" in WORKSPACE_WRITE.allowed_roots
    assert WORKSPACE_WRITE.allow_network is False


def test_network_restricted_requires_allowlist() -> None:
    """受限网络必须有 Host allowlist。"""
    assert NETWORK_RESTRICTED.allow_network is True
    assert len(NETWORK_RESTRICTED.allowed_hosts) >= 1


def test_plugin_process_is_subprocess() -> None:
    """第三方默认进程外。"""
    assert PLUGIN_PROCESS.subprocess is True
    assert PLUGIN_PROCESS.max_processes == 1


def test_get_profile_all_supported() -> None:
    """All supported profiles can be resolved."""
    for name in ["read_only", "workspace_write", "network_restricted", "plugin_process"]:
        p = get_profile(name)
        assert p.name == name


def test_get_profile_unknown_raises() -> None:
    with pytest.raises(ValueError):
        get_profile("unknown")


def test_validate_profile_detects_issues() -> None:
    """校验器能检测 Profile 声明缺失。"""
    bad = SandboxProfile(name="bad", allow_network=True)
    errors = validate_profile(bad)
    assert any("host allowlist" in e for e in errors)


from cogito.capability.sandbox import NETWORK_RESTRICTED  # noqa: E402
