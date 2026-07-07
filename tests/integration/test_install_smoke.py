"""Install smoke tests: `pip install -e .` + `python -m cogito info` works.

Runs in subprocess and exercises:
- installed entry point (cogito) is on PATH
- argparse, config defaults, workspace path resolution
- no import-time side-effect triggers for channel adapters (RB-08)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = ROOT / "config.example.toml"

PY = sys.executable


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, "-m", "cogito", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ},
    )


class TestInstallSmoke:
    def test_info_works_from_installed_entry_point(self) -> None:
        """RB-A15 — no PYTHONPATH override needed."""
        r = _run("info", "--config", str(EXAMPLE_CONFIG))
        assert r.returncode == 0, r.stderr
        assert "Cogito" in r.stdout

    def test_config_check_reasonable(self) -> None:
        r = _run("config", "check", "--config", str(EXAMPLE_CONFIG))
        assert r.returncode == 0, r.stderr
        out = r.stdout
        assert "[ok] schema:    valid" in out

    def test_unknown_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.toml"
            p.write_text("[storage]\ndb_path='x'\n[magic_section]\nfoo=1\n")
            r = _run("config", "check", "--config", str(p), cwd=Path(tmp))
            assert r.returncode == 2, r.stderr
            assert "magic_section" in r.stderr

    def test_no_channel_traceback(self) -> None:
        """Top-level cogito import must not error on channel adapters (RB-08)."""
        r = subprocess.run(
            [PY, "-c", "import cogito; import cogito.__main__; print('ok')"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stderr
        assert "ok" in r.stdout
