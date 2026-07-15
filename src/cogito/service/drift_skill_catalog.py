"""DriftSkillCatalog —— 扫描内置与 workspace Skill，严格解析 manifest (M4)。

同名 Skill 冲突采用确定性优先级：内置优先；workspace 覆盖需显式配置。
manifest 声明仍是声明，不能放行权限。
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cogito.domain.drift import DriftSkillManifest

_LOGGER = logging.getLogger(__name__)

# 内置 Skill 目录（随包分发）
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "resources" / "drift_skills"
# workspace Skill 运行时位置
WORKSPACE_SKILLS_DIRNAME = "drift/skills"


@dataclass
class ResolvedSkill:
    manifest: DriftSkillManifest
    builtin: bool


def load_builtin_skills() -> dict[str, ResolvedSkill]:
    """扫描内置 Skill 目录。"""
    return _scan_dir(BUILTIN_SKILLS_DIR, builtin=True)


def load_workspace_skills(
    workspace_path: str,
    allow_workspace: bool = False,
) -> dict[str, ResolvedSkill]:
    """扫描 workspace Skill 目录。需 allow_workspace=True 才启用。"""
    if not allow_workspace:
        return {}
    ws = Path(workspace_path) / WORKSPACE_SKILLS_DIRNAME
    if not ws.is_dir():
        return {}
    return _scan_dir(ws, builtin=False)


def resolve_catalog(
    workspace_path: str,
    allow_workspace: bool = False,
) -> dict[str, ResolvedSkill]:
    """合并内置与 workspace；内置优先。"""
    catalog: dict[str, ResolvedSkill] = {}
    # 内置优先
    for name, skill in load_builtin_skills().items():
        catalog[name] = skill
    # workspace 层（内置优先，同名跳过）
    for name, skill in load_workspace_skills(workspace_path, allow_workspace).items():
        if name not in catalog:
            catalog[name] = skill
        else:
            _LOGGER.info("workspace skill %s skipped (builtin takes precedence)", name)
    return catalog


def _scan_dir(dir_path: Path, builtin: bool) -> dict[str, ResolvedSkill]:
    skills: dict[str, ResolvedSkill] = {}
    if not dir_path.is_dir():
        return skills
    for child in sorted(dir_path.iterdir()):
        if not child.is_dir():
            continue
        manifest = _parse_skill_dir(child)
        if manifest is None:
            continue
        # manifest 不得通过声明提升权限：仅做字段级校验
        _validate_manifest(manifest)
        skills[manifest.name] = ResolvedSkill(manifest=manifest, builtin=builtin)
    return skills


def _parse_skill_dir(skill_dir: Path) -> DriftSkillManifest | None:
    manifest_toml = skill_dir / "manifest.toml"
    if not manifest_toml.exists():
        _LOGGER.warning("skill %s: missing manifest.toml", skill_dir.name)
        return None
    try:
        with manifest_toml.open("rb") as f:
            raw = tomllib.load(f)
    except Exception as e:
        _LOGGER.warning("skill %s: cannot parse manifest.toml: %s", skill_dir.name, e)
        return None
    try:
        return DriftSkillManifest.from_dict(raw)
    except Exception as e:
        _LOGGER.warning("skill %s: invalid manifest: %s", skill_dir.name, e)
        return None


def _validate_manifest(manifest: DriftSkillManifest) -> None:
    """字段级校验（仅检查声明合法性，不授权）。"""
    if not manifest.name:
        raise ValueError("skill manifest missing name")
    if manifest.risk_level not in ("low", "medium", "high"):
        raise ValueError(f"invalid risk_level: {manifest.risk_level}")
    if manifest.max_steps < 0 or manifest.max_runtime_seconds < 0:
        raise ValueError("negative step/runtime budget")
