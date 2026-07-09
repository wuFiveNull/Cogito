"""Profile Home + Directory Isolation (Plan 06 M1).

目标布局:
  default home/
    config.toml / .env or secret refs / data/database/cogito.db / data/payload /
    data/cache / plugins / skills / memory / logs / backups
  profiles/<name>/ 同构且完全隔离
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


def get_home(profile: str = "default") -> Path:
    """实现 get_home(profile)。禁止业务代码硬编码 .workspace/~/.cogito/cwd。"""
    if profile == "default":
        return Path(".workspace").resolve()
    return Path(".workspace", "profiles", profile).resolve()


def display_home(profile: str = "default") -> str:
    return str(get_home(profile))


@dataclass(frozen=True)
class ProfileLayout:
    """Profile 目录布局 (Plan 06 M1)。"""
    home: Path

    @property
    def config_toml(self) -> Path:
        return self.home / "config.toml"

    @property
    def database(self) -> Path:
        return self.home / "data" / "database" / "cogito.db"

    @property
    def payload_dir(self) -> Path:
        return self.home / "data" / "payload"

    @property
    def cache_dir(self) -> Path:
        return self.home / "data" / "cache"

    @property
    def plugins_dir(self) -> Path:
        return self.home / "plugins"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    def ensure_directories(self) -> None:
        """创建全部目录（幂等）。"""
        for d in [self.payload_dir, self.cache_dir, self.plugins_dir,
                  self.skills_dir, self.memory_dir, self.logs_dir, self.backups_dir,
                  self.database.parent]:
            d.mkdir(parents=True, exist_ok=True)


def create_profile(profile: str) -> ProfileLayout:
    """从默认模板创建，不复制 Secret 明文。"""
    layout = ProfileLayout(home=get_home(profile))
    layout.ensure_directories()
    return layout


def delete_profile(profile: str) -> Path:
    """删除前展示路径并要求确认（调用方负责确认）。返回路径。"""
    return get_home(profile)
