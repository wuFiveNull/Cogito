"""R8: Drift 运维手册对应的 smoke 测试。

验证文档描述的关键路径可用：
- config 默认值（drift.enabled=false, dry_run=true）
- 配置加载 [drift] / [drift.preemption] / [proactive.cadence]
- enable/disable via config 开关不影响 parsing
- Command API 基础设施（approve/retry 等）存在可导入
"""
from __future__ import annotations

from cogito.config import Config, DriftConfig, ProactiveCadenceConfig


class TestDriftConfigDefaults:
    def test_default_disabled_and_dry_run(self):
        cfg = Config()
        assert cfg.drift.enabled is False
        assert cfg.drift.dry_run is True
        assert cfg.drift.idle_after_minutes == 30
        assert cfg.drift.max_runs_per_day == 3

    def test_preemption_defaults(self):
        cfg = Config()
        assert cfg.drift.preemption.check_interval_seconds == 1
        assert cfg.drift.preemption.high_priority_backlog_threshold == 1

    def test_cadence_defaults(self):
        cfg = Config()
        c = cfg.capability.proactive.cadence
        assert c.min_interval_seconds == 60
        assert c.max_interval_seconds == 1800
        assert c.high_energy_interval_seconds == 60
        assert c.low_energy_interval_seconds == 480
        assert c.jitter_ratio == 0.10
        assert c.misfire_policy == "coalesce"


class TestDriftConfigFromRaw:
    def test_parse_drift_section(self):
        cfg = DriftConfig._from_raw({
            "enabled": True,
            "dry_run": False,
            "idle_after_minutes": 60,
            "max_runs_per_day": 5,
            "allow_workspace_skills": True,
            "allow_candidate_emission": True,
            "preemption": {"check_interval_seconds": 2},
        })
        assert cfg.enabled is True
        assert cfg.dry_run is False
        assert cfg.idle_after_minutes == 60
        assert cfg.max_runs_per_day == 5
        assert cfg.allow_workspace_skills is True
        assert cfg.allow_candidate_emission is True
        assert cfg.preemption.check_interval_seconds == 2

    def test_parse_cadence_section(self):
        c = ProactiveCadenceConfig._from_raw({
            "min_interval_seconds": 120,
            "max_interval_seconds": 3600,
            "jitter_ratio": 0.05,
        })
        assert c.min_interval_seconds == 120
        assert c.max_interval_seconds == 3600
        assert c.jitter_ratio == 0.05

    def test_unknown_drift_field_rejected(self):
        import pytest
        with pytest.raises(Exception):
            DriftConfig._from_raw({"nonexistent_field": 1})


class TestCommandInfrastructure:
    def test_command_handlers_importable(self):
        """Command API 基础设施（approve/retry/pause）可导入。"""
        from cogito.service.api.command_handlers import (
            approve, cancel_turn, retry_task, pause_connector,
        )
        assert callable(approve)
        assert callable(retry_task)
        assert callable(pause_connector)
        assert callable(cancel_turn)
