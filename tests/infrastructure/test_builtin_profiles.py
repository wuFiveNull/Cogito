from __future__ import annotations

from pathlib import Path

from cogito.__main__ import PROFILE_DIR
from cogito.config import Config


def test_builtin_profiles_are_packaged_and_valid() -> None:
    expected = {"minimal", "developer", "personal"}
    paths = {path.stem: path for path in PROFILE_DIR.glob("*.toml")}
    assert set(paths) == expected

    loaded = {name: Config.load(path) for name, path in paths.items()}
    assert loaded["minimal"].agent.enabled_toolsets == ["core", "memory"]
    assert loaded["minimal"].capability.auto_mode.enabled is False
    assert loaded["developer"].capability.workspace.root == "."
    assert loaded["developer"].capability.auto_mode.enabled is True
    assert loaded["personal"].capability.workspace.root == ""
    assert loaded["personal"].capability.proactive.enabled is True


def test_profile_templates_do_not_configure_web_search_mcp() -> None:
    for path in Path(PROFILE_DIR).glob("*.toml"):
        config = Config.load(path)
        assert config.capability.mcp_servers == []
        assert "web_search" not in config.capability.mcp_aliases
