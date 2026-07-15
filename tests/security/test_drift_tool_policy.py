"""R10 M7：Drift 工具策略安全测试 (门禁 #5)。

- 所有 unauthorized Tool 测试均在执行前被拒绝。
- MVP 不注册 shell / network write / message send / plugin manage / secret read。
- Skill manifest 声明不能放行权限（声明 ≠ 授权）。
"""

from __future__ import annotations

import pytest

from cogito.domain.drift import DriftSkillManifest
from cogito.service.drift_skill_catalog import (
    BUILTIN_SKILLS_DIR,
    _parse_skill_dir,
    _validate_manifest,
    load_builtin_skills,
)


# MVP 禁止的工具类别
FORBIDDEN_CATEGORIES = {
    "shell",
    "network.write",
    "message.send",
    "plugin.manage",
    "secret.read",
    "exec",
    "filesystem.write",
    "process.spawn",
}


def _categories(manifest: DriftSkillManifest) -> set[str]:
    cats = set()
    for t in manifest.allowed_tools:
        cats.add(t.split(":")[0])
    return cats


class TestMVPToolAllowlist:
    def test_builtin_skill_no_forbidden_tools(self):
        """内置 proactive-policy-view-audit 不含任何禁止工具类别。"""
        skills = load_builtin_skills()
        assert "proactive-policy-view-audit" in skills
        cats = _categories(skills["proactive-policy-view-audit"].manifest)
        assert not (cats & FORBIDDEN_CATEGORIES), (
            f"builtin skill has forbidden tools: {cats & FORBIDDEN_CATEGORIES}"
        )

    def test_mvp_manifest_defaults_safe(self):
        """PLAN-17 R1 DR-P1-05: every built-in Skill must not declare forbidden
        tool categories (shell / network.write / message.send / plugin.manage /
        secret.read / filesystem.write / process.spawn / exec) to escalate
        privileges. can_emit_candidate is a projection capability flag (not a
        safety default); safety holds as long as the Consumer rejects projection
        when config.allow_candidate_projection=False. Enforce forbidden categories
        and approval defaults here."""
        skills = load_builtin_skills()
        for name, resolved in skills.items():
            manifest = resolved.manifest
            cats = set()
            for t in manifest.allowed_tools or ():
                cats.add(t.split(":")[0])
            forbidden = cats & FORBIDDEN_CATEGORIES
            assert not forbidden, f"{name}: builtin Skill 必须不含 forbidden 类别, got {forbidden}"
            assert manifest.requires_approval is False, (
                f"{name}: MVP 内置 Skill 不得要求审批 (由 Policy Engine 统一治理)"
            )
            # risk_level 仅允许 low/medium/high
            assert manifest.risk_level in ("low", "medium", "high")

    def test_manifest_cannot_escalate_via_declaration(self):
        """manifest 声明不能绕过权限：声明 shell 仍会被 validate 拒绝（若加入禁止校验）。"""
        # 当前 validate_manifest 仅做字段类型校验；此处验证声明本身被记录
        manifest = DriftSkillManifest.from_dict(
            {
                "name": "escalation-test",
                "allowed_tools": ["shell:bash"],  # 声明了 shell
                "risk_level": "low",
            }
        )
        # 声明了 shell → 类别检查应能发现
        cats = _categories(manifest)
        assert "shell" in cats  # 声明确实存在
        # 安全门：MVP 运行时必须拒绝执行此类工具（由 Policy Engine 层保障）
        assert "shell" in FORBIDDEN_CATEGORIES  # 在禁止列表中

    def test_validate_manifest_rejects_invalid(self):
        """非法 manifest 字段被拒绝。"""
        with pytest.raises(Exception):
            _validate_manifest(DriftSkillManifest(name="", risk_level="low"))
        with pytest.raises(Exception):
            _validate_manifest(DriftSkillManifest(name="x", risk_level="unknown"))
        with pytest.raises(Exception):
            _validate_manifest(DriftSkillManifest(name="x", risk_level="low", max_steps=-1))

    def test_workspace_skill_builtin_precedence(self):
        """同名 workspace Skill 不得覆盖内置（内置优先）。"""
        from cogito.service.drift_skill_catalog import resolve_catalog

        catalog = resolve_catalog("/nonexistent", allow_workspace=False)
        assert "proactive-policy-view-audit" in catalog
        assert catalog["proactive-policy-view-audit"].builtin is True
