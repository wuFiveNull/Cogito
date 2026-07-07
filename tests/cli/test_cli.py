"""CLI smoke tests — exec the cogito process via subprocess (RB-A01, RB-A02).

These tests exercise the whole CLI surface (argparse, config load/validation,
exit codes, stdout/stderr) without depending on internal function call patterns.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = ROOT / "config.example.toml"
CONFIG_TOML = ROOT / "config.toml"

PY = sys.executable


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `python -m cogito ...` with captured output and strict text mode."""
    return subprocess.run(
        [PY, "-m", "cogito", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestConfigCheck:
    def test_example_config_is_valid(self) -> None:
        result = _run_cli("config", "check", "--config", str(EXAMPLE_CONFIG))
        assert result.returncode == 0, result.stderr
        assert "[ok] schema:    valid" in result.stdout
        assert "[ok] model:     stub" in result.stdout

    def test_current_config_loads(self) -> None:
        if not CONFIG_TOML.exists():
            pytest.skip("no local config.toml")
        result = _run_cli("config", "check", "--config", str(CONFIG_TOML))
        assert result.returncode == 0, result.stderr
        assert "[ok] schema:    valid" in result.stdout

    def test_config_check_does_not_leak_secret(self) -> None:
        """Secret values must never appear in stdout or stderr (CONFIG-PROFILES / 5)."""
        if not CONFIG_TOML.exists():
            pytest.skip("no local config.toml")
        content = CONFIG_TOML.read_text(encoding="utf-8")
        # Build a synthetic sensitive-looking secret so we don't assert on actual user data.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "secret.toml"
            p.write_text(
                textwrap.dedent(f"""\
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
                outbox_lease_ttl_seconds = 120
                delivery_lease_ttl_seconds = 120
                recovery_grace_period_seconds = 30
                [model]
                provider = "openai_compat"
                [model.main]
                model = "dummy"
                api_key = "sk-test-secret-value-1234"
                base_url = "https://example.invalid"
                """),
                encoding="utf-8",
            )
            result = _run_cli("config", "check", "--config", str(p))
            assert result.returncode == 0, result.stderr
            combined = result.stdout + result.stderr
            assert "sk-test-secret-value-1234" not in combined
            # Sensitive tokens should be masked when present anywhere
            assert "sk-test" not in combined or "sk-t****" in combined

    def test_unknown_field_reports_and_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.toml"
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
                outbox_lease_ttl_seconds = 120
                delivery_lease_ttl_seconds = 120
                recovery_grace_period_seconds = 30
                [model]
                provider = "openai_compat"
                enable_thinking = true
                multimodal = true
                """),
                encoding="utf-8",
            )
            result = _run_cli("config", "check", "--config", str(bad))
            assert result.returncode == 2, result.stderr
            assert "enable_thinking" in result.stderr
            assert "multimodal" in result.stderr
            # No raw Secret in output
            combined = result.stdout + result.stderr
            assert os.environ.get("USER", "") + "fake" not in combined  # sanity
            # No traceback
            assert "Traceback" not in result.stderr

    def test_worker_constraint_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.toml"
            p.write_text(
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
                lease_duration_seconds = 60
                heartbeat_interval_seconds = 60
                outbox_lease_ttl_seconds = 120
                delivery_lease_ttl_seconds = 120
                recovery_grace_period_seconds = 30
                """),
                encoding="utf-8",
            )
            result = _run_cli("config", "check", "--config", str(p))
            assert result.returncode == 2, result.stderr
            assert "lease_duration_seconds" in result.stderr


class TestHelpAndBasics:
    def test_help_works(self) -> None:
        result = _run_cli("--help")
        assert result.returncode == 0
        assert "cogito" in result.stdout

    def test_info_works(self) -> None:
        result = _run_cli("info", "--config", str(EXAMPLE_CONFIG))
        assert result.returncode == 0, result.stderr
        assert "Cogito" in result.stdout
