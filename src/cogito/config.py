"""Configuration — load/save config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config.toml")


@dataclass
class DatabaseConfig:
    path: str = "data/cogito.db"
    enable_wal: bool = True
    busy_timeout: int = 5000


@dataclass
class WorkspaceConfig:
    path: str = ".workspace"
    payload_dir: str = "data/payload"
    log_dir: str = "logs"


@dataclass
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
        cfg_path = Path(path)
        if not cfg_path.exists():
            return cls._default()

        with cfg_path.open("rb") as f:
            data = tomllib.load(f)

        db_cfg = DatabaseConfig()
        ws_cfg = WorkspaceConfig()

        db_section = data.get("database", {})
        if isinstance(db_section, dict):
            db_cfg.path = str(db_section.get("path", db_cfg.path))
            db_cfg.enable_wal = bool(db_section.get("enable_wal", db_cfg.enable_wal))
            db_cfg.busy_timeout = int(db_section.get("busy_timeout", db_cfg.busy_timeout))

        ws_section = data.get("workspace", {})
        if isinstance(ws_section, dict):
            ws_cfg.path = str(ws_section.get("path", ws_cfg.path))
            ws_cfg.payload_dir = str(ws_section.get("payload_dir", ws_cfg.payload_dir))
            ws_cfg.log_dir = str(ws_section.get("log_dir", ws_cfg.log_dir))

        return cls(database=db_cfg, workspace=ws_cfg, raw=data)

    @classmethod
    def _default(cls) -> Config:
        return cls()

    def save_default(self, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        cfg_path = Path(path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        content = f"""# Cogito Configuration
[database]
path = "{self.database.path}"
enable_wal = {"true" if self.database.enable_wal else "false"}
busy_timeout = {self.database.busy_timeout}

[workspace]
path = "{self.workspace.path}"
payload_dir = "{self.workspace.payload_dir}"
log_dir = "{self.workspace.log_dir}"
"""
        cfg_path.write_text(content)

    def resolve_db_path(self) -> str:
        return str(Path(self.workspace.path) / self.database.path)
