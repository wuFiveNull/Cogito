"""Install smoke tests: public Python API is importable and usable without a CLI.

Drives the public Python API (`Config.load`, `RuntimeApplication.build`) directly,
instead of going through `python -m cogito`.  Exercises:
- cogito package has no import-time side-effect triggers for channel adapters (RB-08)
- RuntimeApplication builds without traceback
- config load + schema validation round-trips
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = ROOT / "config.example.toml"

SAMPLE_BODY = textwrap.dedent("""\
    workspace_path = ".workspace"
    [storage]
    db_path = "data/cogito.db"
    enable_wal = true
    busy_timeout = 5000
    payload_dir = "data/payload"
    [runtime]
    profile = "personal"
    timezone = "Asia/Shanghai"
    [interaction]
    bind_host = "127.0.0.1"
    allow_remote = false
    validate_origin = true
    [worker]
    concurrency = 1
    lease_duration_seconds = 300
    heartbeat_interval_seconds = 60
    delivery_lease_ttl_seconds = 120
    recovery_grace_period_seconds = 30
""")


class TestPublicApiImport:
    def test_top_level_import(self) -> None:
        """Top-level cogito import must not error on channel adapters (RB-08)."""
        import cogito

        assert hasattr(cogito, "__version__")

    def test_main_cli_module_exists(self) -> None:
        """轻薄 CLI 启动器应可被 `python -m cogito` 调用。"""
        import importlib

        spec = importlib.util.find_spec("cogito.__main__")
        assert spec is not None, "cogito.__main__ 轻薄启动器应存在"

    def test_runtime_public_api_exposed(self) -> None:
        from cogito.application import RuntimeApplication
        from cogito.config import Config

        # Public API surface used by deployment launchers:
        assert callable(getattr(RuntimeApplication, "build", None))
        assert callable(getattr(RuntimeApplication, "close", None))
        assert callable(getattr(RuntimeApplication, "recovery_counts", None))
        assert hasattr(Config, "load")


class TestPublicConfigApi:
    def test_load_example_config(self) -> None:
        from cogito.config import Config

        cfg = Config.load(EXAMPLE_CONFIG)
        assert cfg.schema_version
        assert Path(cfg.workspace_path).name == ".workspace"

    def test_unknown_section_rejected(self) -> None:
        from cogito.config import Config, ConfigError

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.toml"
            p.write_text("[storage]\ndb_path='x'\n[magic_section]\nfoo=1\n")
            try:
                Config.load(p)
            except ConfigError as e:
                assert "magic_section" in str(e)
            else:
                raise AssertionError("Expected ConfigError for unknown section")

    def test_secret_not_in_formatted_error(self) -> None:
        from cogito.config import Config, ConfigError

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "config.toml"
            bad.write_text(
                textwrap.dedent("""\
                workspace_path = ".workspace"
                [storage]
                db_path = "data/cogito.db"
                enable_wal = true
                busy_timeout = 5000
                payload_dir = "data/payload"
                [runtime]
                profile = "personal"
                timezone = "Asia/Shanghai"
                [interaction]
                bind_host = "127.0.0.1"
                allow_remote = false
                validate_origin = true
                [worker]
                concurrency = 1
                lease_duration_seconds = 300
                heartbeat_interval_seconds = 60
                delivery_lease_ttl_seconds = 120
                recovery_grace_period_seconds = 30
                [model]
                provider = "openai_compat"
                api_key = "sk-this-is-a-test-secret-123"
                enable_thinking = true
            """)
            )
            try:
                Config.load(bad)
            except ConfigError as e:
                formatted = e.format_cli() if hasattr(e, "format_cli") else str(e)
                assert "sk-this-is-a-test-secret-123" not in formatted, formatted
            else:
                raise AssertionError("Expected ConfigError for invalid model config")

    def test_application_builds_on_fresh_workspace(self) -> None:
        """RB-A03 — public API builds without traceback on fresh workspace."""
        from cogito.config import Config
        from cogito.application import RuntimeApplication

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg_path = cwd / "config.toml"
            cfg_path.write_text(SAMPLE_BODY, encoding="utf-8")
            cfg = Config.load(cfg_path)
            app = RuntimeApplication.build(cfg)
            try:
                counts = app.recovery_counts()
                assert "streaming_deliveries" in counts
                assert "stale_turns" in counts
            finally:
                app.close()
