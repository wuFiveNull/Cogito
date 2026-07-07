"""CLI smoke tests — exec the cogito process via subprocess (RB-A01, RB-A02).

These tests exercise the whole CLI surface (argparse, config load/validation,
exit codes, stdout/stderr) without depending on internal function call patterns.

QQ-ONEBOT-E2E-01 / PR 1:
- 源码黑盒测试显式 PYTHONPATH=src，不依赖 editable install。
- 删除对本地 config.toml 的测试依赖（本机私有文件）。
- 使用 fixtures 目录下的去敏配置文件。
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
FIXTURES_DIR = ROOT / "tests" / "fixtures" / "config"

PY = sys.executable


def subprocess_env() -> dict[str, str]:
    """源码黑盒测试的确定性环境 —— 显式 PYTHONPATH=src。

    不依赖 pytest 对本进程 sys.path 的修改传播到子进程。
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `python -m cogito ...` with captured output and strict text mode."""
    return subprocess.run(
        [PY, "-m", "cogito", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=30,
        env=subprocess_env(),
    )


class TestConfigCheck:
    def test_example_config_is_valid(self) -> None:
        result = _run_cli("config", "check", "--config", str(EXAMPLE_CONFIG))
        assert result.returncode == 0, result.stderr
        assert "[ok] schema:    valid" in result.stdout
        assert "[ok] model:     stub" in result.stdout

    def test_canonical_fixture_is_valid(self) -> None:
        """Canonical fixture 必须通过 config check（QQ-A03）。"""
        p = FIXTURES_DIR / "canonical.toml"
        result = _run_cli("config", "check", "--config", str(p))
        assert result.returncode == 0, result.stderr
        assert "[ok] schema:    valid" in result.stdout

    def test_qq_onebot_fixture_is_valid_when_enabled(self) -> None:
        """QQ OneBot enabled fixture 通过校验，包含 [channel.qq]（QQ-A07）。"""
        p = FIXTURES_DIR / "qq_onebot.toml"
        result = _run_cli("config", "check", "--config", str(p))
        assert result.returncode == 0, result.stderr
        assert "[ok] schema:    valid" in result.stdout
        # token 不出现在 stdout/stderr
        combined = result.stdout + result.stderr
        assert "test-qq-access-token" not in combined

    def test_qq_onebot_host_non_loopback_rejected(self) -> None:
        """非 loopback host 必须拒绝（QQ-A06）。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad_qq.toml"
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
                lease_duration_seconds = 300
                heartbeat_interval_seconds = 60
                outbox_lease_ttl_seconds = 120
                delivery_lease_ttl_seconds = 120
                recovery_grace_period_seconds = 30
                [channel.qq]
                enabled = true
                driver = "aiocqhttp"
                instance_id = "qq-main"
                host = "0.0.0.0"
                port = 8080
                access_token = "tok"
                owner_qq_ids = ["123456"]
                """),
                encoding="utf-8",
            )
            result = _run_cli("config", "check", "--config", str(p))
            assert result.returncode == 2, result.stderr
            assert "host" in result.stderr.lower()

    def test_config_check_does_not_leak_secret(self) -> None:
        """Secret values must never appear in stdout or stderr (CONFIG-PROFILES / 5).

        使用合成敏感值，不读取本机 config.toml（QQ-A27）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "secret.toml"
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
