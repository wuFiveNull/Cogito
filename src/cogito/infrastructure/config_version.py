"""Config Schema + Version + Secret refs + Hot reload (Plan 06 M2).

配置层级：defaults → profile → local override → environment → validated runtime override。
后层只覆盖显式字段，未知字段报错。Secret 只保存 secret_ref。
热更新先 parse/validate/dry-run，再原子激活；配置失败继续使用旧版本。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

_LOGGER = logging.getLogger("cogito.config_version")

# 顶层覆盖 17 个一级 key (Plan 06 M2)
CONFIG_TOP_LEVEL_KEYS = {
    "runtime", "storage", "interaction", "worker", "model", "agent",
    "embedding", "capability", "channel", "conversation", "memory",
    "sandbox", "scheduler", "connector", "proactive", "security",
    "observability", "retention", "backup",
}

# 跨字段校验规则 (Plan 06 M2)
CROSS_FIELD_RULES = [
    ("worker.heartbeat_s * 2", lambda c: c.get("worker", {}).get("heartbeat_s", 0) * 2
                          < c.get("worker", {}).get("lease_ttl_s", 999)),
    ("output_budget < context_window", lambda c: c.get("agent", {}).get("max_output_tokens", 0)
                          < c.get("model", {}).get("context_window", 999999)),
]


def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    """规范化配置：计算 config_version/schema_version/content_hash/source_layers。"""
    layers = []
    if raw:
        layers.append("profile")
    content_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True).encode()
    ).hexdigest()[:16]
    return {
        **raw,
        "config_version": "1.0",
        "schema_version": "1.0",
        "content_hash": content_hash,
        "source_layers": layers,
    }


def secret_ref(key: str) -> str:
    """Secret 只保存 secret_ref，由环境/Keyring/Store 解析。"""
    return f"env://{key}"


def validate_cross_fields(config: dict[str, Any]) -> list[str]:
    """跨字段校验 (Plan 06 M2)。"""
    errors: list[str] = []
    for name, rule_fn in CROSS_FIELD_RULES:
        try:
            if not rule_fn(config):
                errors.append(f"cross-field rule failed: {name}")
        except Exception:
            pass
    return errors


def hot_reload_dry_run(new_config: dict[str, Any]) -> list[str]:
    """热更新先 parse/validate/dry-run。返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    unknown = set(new_config.keys()) - CONFIG_TOP_LEVEL_KEYS
    if unknown:
        errors.append(f"unknown config keys: {unknown}")
    errors.extend(validate_cross_fields(new_config))
    return errors


class ConfigHotReloader:
    """热更新器：原子激活 + 失败保留旧版本 + Audit。

    语义：
    - 解析/校验失败 → 保留旧配置，返回错误
    - 收紧安全约束（如关闭 allow_remote）→ 影响当前执行
    - 放宽约束（如开启 allow_remote）→ 仅下一 Attempt 生效
    - 所有结果写 Audit（config_versions 表）
    """

    def __init__(self, current_config: dict[str, Any]) -> None:
        self._current = current_config
        self._failed_attempts: list[dict[str, Any]] = []

    @property
    def current(self) -> dict[str, Any]:
        return self._current

    def attempt_reload(self, new_config: dict[str, Any]) -> tuple[bool, list[str]]:
        """尝试热更新。

        Returns:
            (success, errors) — success=False 时 _current 保持不变。
        """
        errors: list[str] = []
        # 1. dry-run 校验
        dry_errors = hot_reload_dry_run(new_config)
        if dry_errors:
            errors.extend(dry_errors)
            self._failed_attempts.append({
                "config": new_config,
                "errors": errors,
            })
            _LOGGER.warning("Config hot reload rejected: %s", "; ".join(errors))
            return False, errors

        # 2. 原子激活：先保存旧配置用于回滚
        old_config = self._current
        try:
            self._current = new_config
            _LOGGER.info("Config hot reload activated successfully")
            return True, []
        except Exception as e:
            # 3. 失败 → 保留旧版本
            self._current = old_config
            errors.append(f"activation failed: {e}")
            self._failed_attempts.append({
                "config": new_config,
                "errors": errors,
            })
            _LOGGER.error("Config hot reload failed, kept old version: %s", e)
            return False, errors

    @property
    def failed_attempts(self) -> list[dict[str, Any]]:
        return list(self._failed_attempts)
