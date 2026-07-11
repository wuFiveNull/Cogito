"""DriftSkillSelector —— 确定性评分选择下次执行的 Skill (M4)。

MVP 先用 deterministic 评分 (due、上次状态、失败退避、预期成本、价值)；
评分和权重版本进入 selection trace。

score = due_score
      + expected_value
      + continuation_bonus(paused)
      + staleness_bonus
      - estimated_cost
      - recent_run_penalty
      - recent_failure_penalty
"""
from __future__ import annotations

import time
from typing import Any

from cogito.domain.drift import DriftSkillManifest


# 评分权重版本 (bumping 可使 selection trace 跨版本可比较)
WEIGHTS_VERSION = "1"
W_DUE = 30.0
W_VALUE = 20.0
W_CONTINUATION = 15.0
W_STALENESS = 10.0
W_COST = 1.0
W_RECENT_RUN = 8.0
W_RECENT_FAILURE = 25.0
RECENCY_PENALTY_WINDOW_S = 1800   # 30 分钟内最近运行 → 惩罚
FAILURE_BACKOFF_S = 600           # 失败后至少 10 分钟不选
MAX_STALENESS_S = 86400           # 24h 未运行 → 最大 stale bonus


def select_skill(
    skills: dict[str, Any],
    skill_states: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, float]] | None:
    """返回 (skill_name, per_skill_score_dict)；无可选返回 None。

    skills: dict[name, DriftSkillManifest | ResolvedSkill]（取 .manifest）
    """
    if not skills:
        return None

    now = int(time.time() * 1000)
    scores: dict[str, float] = {}

    for name, entry in skills.items():
        manifest = entry.manifest if hasattr(entry, "manifest") else entry
        state = skill_states.get(name, {})
        last_run_at = state.get("last_run_at") or 0
        last_status = state.get("last_status")
        run_count = state.get("run_count", 0)
        del run_count  # 当前评分未直接使用

        # due_score: 距上次运行越久越高
        age_s = (now - last_run_at) / 1000.0 if last_run_at else MAX_STALENESS_S
        due_score = W_DUE * min(1.0, age_s / MAX_STALENESS_S)

        # expected_value: 由 risk_level 推断 (low risk 可用性高 → 略高)
        value_map = {"low": 1.0, "medium": 0.7, "high": 0.4}
        expected_value = W_VALUE * value_map.get(manifest.risk_level, 0.5)

        # continuation_bonus: paused 状态给予 bonus 以继续
        continuation = W_CONTINUATION if last_status == "paused" else 0.0

        # staleness_bonus
        staleness = W_STALENESS * min(1.0, age_s / MAX_STALENESS_S)

        # estimated_cost (tool calls + steps)
        cost = W_COST * (manifest.max_tool_calls + manifest.max_steps)

        # recent_run_penalty (30m 内运行过)
        recent_run = (W_RECENT_RUN
                      if last_run_at and (now - last_run_at) < RECENCY_PENALTY_WINDOW_S * 1000
                      else 0.0)

        # recent_failure_penalty (失败退避)
        recent_failure = (W_RECENT_FAILURE
                          if (last_status == "failed"
                              and last_run_at
                              and (now - last_run_at) < FAILURE_BACKOFF_S * 1000)
                          else 0.0)

        total = (due_score + expected_value + continuation + staleness
                 - cost - recent_run - recent_failure)
        scores[name] = total

    if not scores:
        return None
    best = max(scores, key=lambda k: scores[k])
    return best, scores
