"""ProactivePolicy 的加载 + Markdown 投影。

PROACTIVE-IDLE: policy 是 .workspace/ 下的派生视图，缺失时重建——
人工修改不能直接改变运行策略。

本文件只负责把 policy 渲染为 PROACTIVE_CONTEXT.md 文件。
实际运行的决策逻辑在 proactive_decision.py 读取数据库 policy。
"""

from __future__ import annotations

import logging
from pathlib import Path

from cogito.store.proactive_repo import ProactivePolicy, ProactivePolicyRepository

_LOGGER = logging.getLogger(__name__)


def render_policy_markdown(
    policy: ProactivePolicy,
    *,
    candidate_count: int = 0,
    dry_run: bool = True,
) -> str:
    """把 ProactivePolicy 投影为 PROACTIVE_CONTEXT.md 文本。"""
    topics_allow = ", ".join(policy.allow_topics) if policy.allow_topics else "（未配置）"
    topics_deny = ", ".join(policy.deny_topics) if policy.deny_topics else "无"
    lines = [
        "# PROACTIVE_CONTEXT.md",
        "",
        "> 本文件由系统根据 proactive_policies 表自动渲染，**不可手动编辑**。",
        f"> policy_version={policy.version}, principal={policy.principal_id}",
        f"> dry_run={dry_run}, updated_at=policy.updated_at",
        "",
        "## 运行状态",
        f"- mode: {'dry_run' if dry_run else 'live'}",
        f"- 活跃候选数: {candidate_count}",
        "",
        "## Quiet Hours",
        f"- enabled: {policy.quiet_hours.get('enabled', True)}",
        f"- 时段: {policy.quiet_hours.get('start', '23:00')} - "
        f"{policy.quiet_hours.get('end', '08:00')} "
        f"({policy.quiet_hours.get('timezone', 'Asia/Shanghai')})",
        "",
        "## 预算",
        f"- 每小时最多: {policy.max_pushes_per_hour}",
        f"- 每天最多: {policy.max_pushes_per_day}",
        "",
        "## 冷却",
        f"- 同 topic 冷却分钟: {policy.cooldown_minutes_same_topic}",
        "",
        "## 阈值",
        f"- minimum_relevance: {policy.minimum_relevance}",
        f"- minimum_novelty: {policy.minimum_novelty}",
        f"- digest_max_delay_minutes: {policy.digest_max_delay_minutes}",
        "",
        "## 白名单 topics",
        f"- 允许: {topics_allow}",
        "",
        "## 黑名单 topics",
        f"- 拒绝: {topics_deny}",
    ]
    return "\n".join(lines)


def write_policy_markdown(
    workspace_path: str | Path,
    policy: ProactivePolicy,
    **kwargs,
) -> Path:
    """把 policy 渲染到 workspace/PROACTIVE_CONTEXT.md。"""
    out = Path(workspace_path) / "PROACTIVE_CONTEXT.md"
    out.write_text(render_policy_markdown(policy, **kwargs), encoding="utf-8")
    return out


def load_or_default_policy(
    conn,
    principal_id: str = "owner",
) -> ProactivePolicy:
    """加载当前生效 policy；若不存在，则 seed 一个默认 policy 版本 1。"""
    repo = ProactivePolicyRepository(conn)
    existing = repo.get_current(principal_id)
    if existing is not None:
        return existing
    # policy_id 使用 stable 形式（single-owner 即 owner 前缀）
    seed = ProactivePolicy(
        policy_id=f"policy-{principal_id}-v1",
        principal_id=principal_id,
        version=1,
        dry_run=True,
    )
    repo.save(seed)
    return seed
