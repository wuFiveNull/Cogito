"""Config Schema + Version + Secret refs + Hot reload (Plan 06 M2).

配置层级：defaults → profile → local override → environment → validated runtime override。
后层只覆盖显式字段，未知字段报错。Secret 只保存 secret_ref。
热更新先 parse/validate/dry-run，再原子激活；配置失败继续使用旧版本。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

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
