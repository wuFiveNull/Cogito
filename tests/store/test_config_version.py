"""Tests for Plan 06 M2 / T3 — Config version + Secret + persistence."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from cogito.config import Config, SENSITIVE_FIELDS
from cogito.infrastructure.config_version import (
    hot_reload_dry_run,
    normalize_config,
    secret_ref,
    validate_cross_fields,
)
from cogito.store.config_version_repo import (
    ConfigVersionRecord,
    ConfigVersionRepository,
)


# ── 8.1: normalize_config ──────────────────────────────────


class TestNormalizeConfig:
    """cfg-01: normalize_config 产生稳定 hash。"""

    def test_stable_hash(self):
        raw = {"runtime": {"profile": "default"}, "model": {"provider": "echo"}}
        a = normalize_config(raw)
        b = normalize_config(raw)
        assert a["content_hash"] == b["content_hash"]
        assert len(a["content_hash"]) == 16

    def test_different_content_different_hash(self):
        a = normalize_config({"a": 1})
        b = normalize_config({"a": 2})
        assert a["content_hash"] != b["content_hash"]


# ── 8.2: Secret 脱敏 ───────────────────────────────────────


class TestSecretMasking:
    """cfg-02: Secret 字段不出现在 dump。"""

    def test_sensitive_fields_defined(self):
        assert "api_key" in SENSITIVE_FIELDS
        assert "token" in SENSITIVE_FIELDS

    def test_config_resolve_secret_env(self):
        os.environ["_TEST_SECRET_"] = "my-secret-value"
        try:
            config = Config()
            val = config.resolve_secret("env://_TEST_SECRET_")
            assert val == "my-secret-value"
        finally:
            del os.environ["_TEST_SECRET_"]

    def test_config_resolve_secret_plain(self):
        config = Config()
        assert config.resolve_secret("plain-value") == "plain-value"

    def test_get_masked(self):
        config = Config()
        masked = config.get_masked("model", "api_key", "sk-1234567890")
        assert "sk-" not in masked
        assert "secret_ref" in masked


# ── 8.3: secret_ref helper ─────────────────────────────────


class TestSecretRef:
    def test_secret_ref_format(self):
        ref = secret_ref("MY_API_KEY")
        assert ref == "env://MY_API_KEY"


# ── 8.4: 跨字段校验 ────────────────────────────────────────


class TestCrossFieldValidation:
    """cfg-05: 热更新跨字段校验（heartbeat vs lease）。"""

    def test_valid_worker_config(self):
        config = {"worker": {"heartbeat_s": 30, "lease_ttl_s": 300}}
        errors = validate_cross_fields(config)
        assert errors == []

    def test_invalid_heartbeat_vs_lease(self):
        # heartbeat_s * 2 >= lease_ttl_s → 失败
        config = {"worker": {"heartbeat_s": 200, "lease_ttl_s": 300}}
        errors = validate_cross_fields(config)
        assert len(errors) == 1
        assert "heartbeat" in errors[0] or "cross-field" in errors[0]


# ── 8.5: 热更新 dry-run ────────────────────────────────────


class TestHotReloadDryRun:
    """cfg-03: 热更新 dry-run 拦截未知 key。"""

    def test_unknown_key_rejected(self):
        errors = hot_reload_dry_run({"unknown_section": {}})
        assert len(errors) == 1
        assert "unknown" in errors[0]

    def test_known_key_accepted(self):
        errors = hot_reload_dry_run({"runtime": {"profile": "default"}})
        assert errors == []


# ── 8.6: Config 加载时计算版本 ─────────────────────────────


class TestConfigLoadVersion:
    def test_load_computes_content_hash(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[runtime]\nprofile = "default"\n\n'
            '[model]\nprovider = "echo"\n',
            encoding="utf-8",
        )
        config = Config.load(cfg_file)
        assert len(config.content_hash) == 16
        assert config.schema_version.startswith("1")

    def test_load_same_content_same_hash(self, tmp_path):
        content = '[runtime]\nprofile = "test"\n'
        f1 = tmp_path / "a.toml"
        f2 = tmp_path / "b.toml"
        f1.write_text(content, encoding="utf-8")
        f2.write_text(content, encoding="utf-8")
        assert Config.load(f1).content_hash == Config.load(f2).content_hash


# ── 8.7: config_versions 持久化 ────────────────────────────


class TestConfigVersionPersistence:
    """cfg-07: config_versions 审计写入。"""

    def test_insert_and_get_by_hash(self, in_memory_db):
        repo = ConfigVersionRepository(in_memory_db)
        rec = ConfigVersionRecord(
            version_id="cfg-1",
            content_hash="abc123",
            schema_version="1",
            source_layers=["profile"],
            applied_at=1000,
            applied_by="startup",
        )
        repo.insert(rec)
        in_memory_db.commit()

        found = repo.get_by_hash("abc123")
        assert found is not None
        assert found.schema_version == "1"

    def test_duplicate_hash_rejected(self, in_memory_db):
        """同一 content_hash 只允许一条记录（UNIQUE 约束）。"""
        repo = ConfigVersionRepository(in_memory_db)
        repo.insert(ConfigVersionRecord(
            version_id="cfg-1", content_hash="unique-hash", schema_version="1",
            source_layers=[], applied_at=1000,
        ))
        in_memory_db.commit()
        with pytest.raises(Exception):
            repo.insert(ConfigVersionRecord(
                version_id="cfg-2", content_hash="unique-hash", schema_version="1",
                source_layers=[], applied_at=2000,
            ))
            in_memory_db.commit()


# ── 8.6: 配置跨字段校验（load 时调用）─────────────────────


class TestConfigLoadValidation:
    """cfg-05: Config.load() 调用 validate_cross_fields。"""

    def test_hostname_lease_validation_passes(self, tmp_path):
        """合法 worker 配置（lease > heartbeat）通过。"""
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[worker]\nheartbeat_interval_seconds = 60\n'
            'lease_duration_seconds = 300\n',
            encoding="utf-8",
        )
        config = Config.load(cfg_file)
        assert config.worker.heartbeat_interval_seconds == 60

    def test_lease_less_than_heartbeat_rejected(self, tmp_path):
        """lease < 2*heartbeat 应在 load() 时 ConfigError。"""
        from cogito.config import ConfigError
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[worker]\nheartbeat_interval_seconds = 200\n'
            'lease_duration_seconds = 300\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            Config.load(cfg_file)


# ── 8.7: 热更新原子激活 + 失败保留旧版本 ──────────────────


class TestHotReload:
    def test_reload_success_activates(self):
        from cogito.infrastructure.config_version import ConfigHotReloader
        reloader = ConfigHotReloader({"runtime": {"profile": "old"}})
        ok, errors = reloader.attempt_reload({"runtime": {"profile": "new"}})
        assert ok is True
        assert errors == []
        assert reloader.current["runtime"]["profile"] == "new"

    def test_reload_failure_keeps_old(self):
        """cfg-06: 热更新失败保留旧版本。"""
        from cogito.infrastructure.config_version import ConfigHotReloader
        reloader = ConfigHotReloader({"runtime": {"profile": "original"}})
        # 传入未知 key 触发 dry-run 拒绝
        ok, errors = reloader.attempt_reload({"unknown_key": "value"})
        assert ok is False
        assert len(errors) == 1
        # 旧版本保留
        assert reloader.current["runtime"]["profile"] == "original"
        assert len(reloader.failed_attempts) == 1
