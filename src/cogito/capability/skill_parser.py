"""SKILL.md parser + Skill Runtime (Plan 03 M6).

完整解析 SKILL.md frontmatter/Markdown，支持内置/用户/Plugin Skill 来源。
状态 active/stale/archived/pinned；只移动到 .archive，支持恢复。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class SkillManifest:
    """解析后的 Skill 声明。"""
    name: str
    description: str = ""
    version: str = "1.0"
    toolsets: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    content: str = ""
    source: str = "user"        # builtin | user | plugin
    author: str = ""
    requires: tuple[str, ...] = ()


@dataclass
class SkillState:
    """Skill 运行时状态。"""
    manifest: SkillManifest
    status: str = "active"      # active | stale | archived | pinned
    loaded_at: str = ""
    last_used_at: str = ""
    use_count: int = 0


# frontmatter 字段: name, description, version, toolsets, permissions, source, author, requires
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_FIELD_RE = re.compile(r"^(\w+):\s*(.+)$")
_LIST_RE = re.compile(r"\s*-\s*(.+)")


def parse_skill_md(raw: str, *, source: str = "user") -> SkillManifest:
    """解析 SKILL.md (frontmatter + Markdown body)。"""
    m = _FRONTMATTER_RE.match(raw)
    body = raw
    data: dict[str, str] = {}

    if m:
        fm, body = m.group(1), m.group(2)
        list_key: str | None = None
        for line in fm.splitlines():
            fm_match = _FIELD_RE.match(line.strip())
            if fm_match:
                list_key = fm_match.group(1)
                val = fm_match.group(2).strip()
                # 行内列表 [a, b]
                if val.startswith("[") and val.endswith("]"):
                    data[list_key] = val[1:-1]
                else:
                    data[list_key] = val
            elif list_key and line.strip().startswith("-"):
                item = _LIST_RE.match(line.strip())
                if item:
                    data[list_key] = data.get(list_key, "") + "," + item.group(1).strip()

    def _tuple(k: str) -> tuple[str, ...]:
        v = data.get(k, "")
        if not v:
            return ()
        return tuple(x.strip() for x in v.split(",") if x.strip())

    return SkillManifest(
        name=data.get("name", "unnamed"),
        description=data.get("description", ""),
        version=data.get("version", "1.0"),
        toolsets=_tuple("toolsets"),
        permissions=_tuple("permissions"),
        content=body.strip(),
        source=source,
        author=data.get("author", ""),
        requires=_tuple("requires"),
    )


def validate_skill(manifest: SkillManifest, *,
                   max_content_chars: int = 50_000) -> list[str]:
    """校验 Skill 声明。"""
    errors: list[str] = []
    if (not manifest.name or manifest.name == "unnamed"
            or not re.match(r"^[a-zA-Z0-9_-]+$", manifest.name)):
        errors.append(f"invalid skill name: {manifest.name!r}")
    if len(manifest.content) > max_content_chars:
        errors.append(f"skill {manifest.name}: content too large ({len(manifest.content)})")
    return errors


class SkillRuntime:
    """Skill 生命周期管理（Plan 03 M6）。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._skills: dict[str, SkillState] = {}

    def parse_and_register(self, raw: str, *,
                           source: str = "user") -> tuple[SkillManifest, list[str]]:
        """解析 + 校验 + 注册 Skill。"""
        manifest = parse_skill_md(raw, source=source)
        errors = validate_skill(manifest)
        if not errors:
            self._skills[manifest.name] = SkillState(
                manifest=manifest,
                status="active",
                loaded_at=datetime.now(UTC).isoformat(),
                use_count=0,
            )
        return manifest, errors

    def activate(self, name: str) -> str | None:
        """激活方式：被动匹配 / 显式 /skill-name / skill_loader。"""
        s = self._skills.get(name)
        if not s:
            return None
        s.status = "active"
        s.last_used_at = datetime.now(UTC).isoformat()
        s.use_count += 1
        return s.manifest.content

    def archive(self, name: str) -> bool:
        """归档只移动到 .archive，支持恢复。"""
        s = self._skills.get(name)
        if not s:
            return False
        s.status = "archived"
        return True

    def restore(self, name: str) -> bool:
        """从归档恢复。"""
        s = self._skills.get(name)
        if not s or s.status != "archived":
            return False
        s.status = "active"
        return True

    def pin(self, name: str) -> bool:
        s = self._skills.get(name)
        if not s:
            return False
        s.status = "pinned"
        return True

    def list_active(self) -> list[SkillState]:
        """列出 active/pinned Skill。"""
        return [s for s in self._skills.values()
                if s.status in ("active", "pinned")]
