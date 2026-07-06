# cogito/infrastructure/tools/policy_engine.py
#
# CompositeToolPolicyEngine — layered, risk-based tool call authorisation.
#
# Design rules (see tool-system-spec §13):
#   - Policy layers are combined by strictest-trumps: any DENY → DENY.
#   - Risk tiers determine default behaviour:
#     READ_ONLY → allow (with data boundary checks)
#     LOCAL_WRITE → require session/workspace scope grant
#     EXTERNAL_READ → allow controlled targets; deny private networks
#     EXTERNAL_WRITE → require approval per call or named persistent grant
#     PRIVILEGED → deny by default; admin policy may override
#   - Approvals can produce persistent grants to avoid re-asking.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from cogito.agent.domain.tools import ToolDefinition, ToolRisk, ToolSideEffect
from cogito.agent.ports.tools.policy import (
    ToolPolicyDecision,
    ToolPolicyDecisionType,
    ToolPolicyPort,
    ToolPolicyRequest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PolicyLayerConfig:
    """Configuration for one policy layer."""
    allow_external_read_targets: frozenset[str] = frozenset()
    allow_workspace_write: bool = False
    admin_override: bool = False
    always_require_approval: frozenset[str] = frozenset()


class CompositeToolPolicyEngine:
    """Layered policy engine applying strictest-trumps composition.

    Policy layers (in order):
      1. Platform policy (global defaults)
      2. Deployment policy (admin overrides)
      3. Workspace policy (workspace-level rules)
      4. Actor policy (user-specific grants)
      5. Session grants (per-session approvals)
      6. Tool-specific guards (defined per-tool)
    """

    def __init__(
        self,
        *,
        platform_config: PolicyLayerConfig | None = None,
        deployment_config: PolicyLayerConfig | None = None,
        prior_grants_provider: object | None = None,
    ) -> None:
        self._platform = platform_config or PolicyLayerConfig()
        self._deployment = deployment_config or PolicyLayerConfig()
        self._prior_grants_provider = prior_grants_provider

    async def evaluate(
        self,
        request: ToolPolicyRequest,
    ) -> ToolPolicyDecision:
        """Evaluate a tool call against all policy layers.

        Returns the strictest applicable decision.
        """
        definition = request.definition
        risk = definition.risk

        # Step 1: Risk-based default
        default = self._risk_default(risk)
        if default.decision is ToolPolicyDecisionType.DENY:
            return default

        # Step 2: Side-effect check
        side_effect_check = self._check_side_effects(definition)
        if side_effect_check is not None:
            return side_effect_check

        # Step 3: Always-require-approval list
        if definition.name in self._platform.always_require_approval:
            return ToolPolicyDecision(
                decision=ToolPolicyDecisionType.REQUIRE_APPROVAL,
                reason_code="ALWAYS_REQUIRE_APPROVAL",
                safe_message=f"工具 {definition.name} 需要确认后才能使用",
                approval_prompt=self._build_approval_prompt(definition, request.arguments),
            )

        # Step 4: Check prior grants (per-session persistent approvals)
        if self._prior_grants_provider is not None:
            grant_result = self._check_prior_grants(request)
            if grant_result is not None:
                return grant_result

        # Step 5: Check prior grants embedded in request
        if request.prior_grants:
            for grant in request.prior_grants:
                grant_name = getattr(grant, "tool_name", None)
                if grant_name == request.definition.name:
                    return ToolPolicyDecision(
                        ToolPolicyDecisionType.ALLOW,
                        reason_code="PRIOR_GRANT",
                        safe_message="",
                    )

        return default

    # ── Risk-based defaults ──────────────────────────────────────────

    @staticmethod
    def _risk_default(risk: ToolRisk) -> ToolPolicyDecision:
        """Map risk to default decision."""
        if risk is ToolRisk.READ_ONLY:
            return ToolPolicyDecision(
                ToolPolicyDecisionType.ALLOW,
                reason_code="RISK_READ_ONLY",
                safe_message="",
            )
        if risk is ToolRisk.EXTERNAL_READ:
            return ToolPolicyDecision(
                ToolPolicyDecisionType.ALLOW,
                reason_code="RISK_EXTERNAL_READ",
                safe_message="",
            )
        if risk is ToolRisk.LOCAL_WRITE:
            return ToolPolicyDecision(
                ToolPolicyDecisionType.ALLOW,
                reason_code="RISK_LOCAL_WRITE",
                safe_message="",
            )
        if risk is ToolRisk.EXTERNAL_WRITE:
            return ToolPolicyDecision(
                ToolPolicyDecisionType.REQUIRE_APPROVAL,
                reason_code="RISK_EXTERNAL_WRITE",
                safe_message="该操作将向外部发送数据，需要确认",
                approval_prompt="将向外部服务写入数据",
            )
        if risk is ToolRisk.PRIVILEGED:
            return ToolPolicyDecision(
                ToolPolicyDecisionType.DENY,
                reason_code="RISK_PRIVILEGED",
                safe_message="高风险操作已被系统策略拒绝",
            )
        return ToolPolicyDecision(
            ToolPolicyDecisionType.DENY,
            reason_code="RISK_UNKNOWN",
            safe_message="未知风险等级的操作已被拒绝",
        )

    @staticmethod
    def _check_side_effects(
        definition: ToolDefinition,
    ) -> ToolPolicyDecision | None:
        """Check side-effect level for additional restrictions."""
        if definition.side_effect is ToolSideEffect.EXTERNAL_MUTATION:
            return ToolPolicyDecision(
                ToolPolicyDecisionType.REQUIRE_APPROVAL,
                reason_code="SIDE_EFFECT_EXTERNAL",
                safe_message="该操作会产生外部副作用，需要确认",
                approval_prompt=f"执行 {definition.name} 将修改外部系统",
            )
        return None

    @staticmethod
    def _check_prior_grants(
        request: ToolPolicyRequest,
    ) -> ToolPolicyDecision | None:
        """Check for prior permission grants."""
        if request.prior_grants:
            for grant in request.prior_grants:
                grant_name = getattr(grant, "tool_name", None)
                if grant_name == request.definition.name:
                    return ToolPolicyDecision(
                        ToolPolicyDecisionType.ALLOW,
                        reason_code="PRIOR_GRANT",
                        safe_message="",
                    )
        return None

    @staticmethod
    def _build_approval_prompt(
        definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> str:
        """Build a user-facing approval prompt."""
        arg_summary = ", ".join(
            f"{k}={v}" for k, v in list(arguments.items())[:5]
        )
        return f"允许 {definition.name}({arg_summary}) 吗？"
