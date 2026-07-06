# cogito/config/loader.py

from __future__ import annotations

import json
import os
import re
import tomllib

from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import ConfigError
from .schema import AppConfig


_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def find_config_path(
    explicit_path: str | Path | None = None,
) -> Path:
    if explicit_path is not None:
        path = Path(explicit_path).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path

    env_path = os.getenv("COGITO_CONFIG")
    if env_path:
        path = Path(env_path).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(
                f"COGITO_CONFIG points to a missing file: {path}"
            )
        return path

    cwd = Path.cwd()
    candidates = (
        cwd / "config" / "config.toml",
        cwd / "config.toml",
        cwd / "cogito.toml",
        Path.home() / ".config" / "cogito" / "config.toml",
    )

    for path in candidates:
        if path.is_file():
            return path.resolve()

    raise ConfigError(
        "configuration file not found; "
        "use --config, COGITO_CONFIG, "
        "or create config/config.toml or config.toml"
    )


def expand_env_in_value(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.getenv(name)
            if resolved is None:
                raise ConfigError(
                    f"required environment variable is not set: {name}"
                )
            return resolved

        return _ENV_PATTERN.sub(replace, value)

    if isinstance(value, list):
        return [expand_env_in_value(item) for item in value]

    if isinstance(value, dict):
        return {key: expand_env_in_value(item) for key, item in value.items()}

    return value


def parse_env_value(raw: str) -> Any:
    lowered = raw.lower()

    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def set_nested(
    target: dict[str, Any],
    path: list[str],
    value: Any,
) -> None:
    current = target

    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child

    current[path[-1]] = value


def apply_environment_overrides(
    data: dict[str, Any],
    *,
    prefix: str = "COGITO__",
) -> dict[str, Any]:
    result = deepcopy(data)

    for name, raw_value in os.environ.items():
        if not name.startswith(prefix):
            continue

        path = [
            part.lower()
            for part in name[len(prefix):].split("__")
            if part
        ]

        if not path:
            continue

        set_nested(result, path, parse_env_value(raw_value))

    return result


def load_config(
    path: str | Path | None = None,
) -> AppConfig:
    config_path = find_config_path(path)

    try:
        with config_path.open("rb") as file:
            raw = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    raw = expand_env_in_value(raw)
    raw = apply_environment_overrides(raw)

    raw["config_path"] = config_path
    # 项目根目录：若 config 在项目根下的 config/ 子目录中，则取父目录
    if config_path.parent.name == "config":
        raw["project_dir"] = config_path.parent.parent
    else:
        raw["project_dir"] = config_path.parent

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc

    validate_config_references(config)
    return config


def validate_config_references(config: AppConfig) -> None:
    for model_name, model in config.llm.models.items():
        if model.provider not in config.llm.providers:
            raise ConfigError(
                f"model {model_name!r} references "
                f"unknown provider {model.provider!r}"
            )

    for role, model_name in config.llm.routes.items():
        if model_name not in config.llm.models:
            raise ConfigError(
                f"route {role!r} references "
                f"unknown model {model_name!r}"
            )
