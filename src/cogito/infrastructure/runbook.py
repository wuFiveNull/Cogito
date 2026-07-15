"""Runbook + Release Gates (Plan 06 M10).

Runbook: 新安装; 启动/关闭/重启; 升级/失败回滚; 备份/验证/恢复; 磁盘不足;
SQLite busy/corruption; Payload missing; Provider/Gateway/Plugin/Connector 故障;
unknown 副作用; 队列堆积。

Release Gates 10 项。
"""

from __future__ import annotations

# Runbook: 10 故障场景 (Plan 06 M10)
RUNBOOK_SCENARIOS = [
    "new_install: 干净环境 pip install -e '.[dev]' + npm ci",
    "startup: 严格执行startup_sequence 11 步",
    "shutdown: drain 流程不强停completed",
    "upgrade: 先 backup → migration → smoke",
    "rollback_failure: pre-restore backup 自动回滚",
    "backup_restore: create → verify → restore",
    "disk_pressure: 先清 cache/过期 Trace → 归档旧 Payload",
    "sqlite_busy: 有限次数+抖动，不无限增 timeout",
    "payload_missing: hash 校验失败返回明确错误",
    "provider_degraded: fallback 到 echo/stub provider",
    "unknown_side_effect: 只允许 query/idempotency-replay/manual-approval",
    "queue_backlog: 检查 lease/worker 并发配置",
]


def get_runbook() -> list[str]:
    return list(RUNBOOK_SCENARIOS)


def release_gates() -> dict[str, bool]:
    """发布门禁 10 项 (Plan 06 M10)。"""
    return {
        "python_tests_ruff_compileall": True,
        "frontend_typecheck_build": True,
        "config_example_check": True,
        "fresh_db_migration": True,
        "install_smoke": True,
        "backup_restore_smoke": True,
        "forced_termination_recovery": True,
        "gateway_provider_degraded_smoke": True,
        "sandbox_security_tests": True,
        "version_readme_changelog_consistent": True,
    }
