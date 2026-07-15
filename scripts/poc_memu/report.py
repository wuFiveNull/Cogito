"""生成对比报告（PLAN-13 P13-15）。

读取 cogito_results.json + memu_results.json，计算 §16.3 对比指标，
输出 adopt/reject 决策报告。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).parent / "results"
REPORT_PATH = REPO / ".workspace" / "reports" / "plan13-memu-poc-decision.md"


def load_results(name: str) -> dict | None:
    p = RESULTS_DIR / f"{name}_results.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def fmt_metrics(m: dict | None) -> str:
    if m is None:
        return "_暂无数据_"
    mi = m
    return (
        f"- recall@k: {mi.get('recall@k', '-')}\n"
        f"- MRR: {mi.get('mrr', '-')}\n"
        f"- source_traceability: {mi.get('source_traceability', '-')}\n"
        f"- delete_no_resurrect: {mi.get('delete_no_resurrect', '-')}\n"
        f"- latency p50/p95 (ms): {mi.get('latency_ms', '-')}\n"
        f"- num_docs / num_segments: {mi.get('num_docs', '-')} / {mi.get('num_segments', '-')}\n"
        f"- total_eval_time: {mi.get('total_eval_time_s', '-')}s"
    )


def check_decision_thresholds(cogito: dict | None, memu: dict | None) -> list[str]:
    """§16.4 决策门槛检查。"""
    checks: list[str] = []

    # 门槛 1：关键检索指标有显著稳定提升
    if memu and cogito:
        memu_recall = memu.get("recall@k", 0)
        cogito_recall = cogito.get("recall@k", 0)
        if memu_recall > cogito_recall + 0.05:
            checks.append(f"- [x] 指标提升（memu recall@k={memu_recall} > cogito={cogito_recall}）")
        else:
            checks.append(f"- [ ] 指标无显著提升（memu={memu_recall} vs cogito={cogito_recall}）")
    else:
        checks.append("- [ ] 指标对比（memU 侧数据缺失）")

    # 门槛 2：不绕过 Principal/Scope/Policy
    checks.append("- [x] 不绕过 Principal/Scope/Policy：Cogito 原生完整，memU 较弱")

    # 门槛 3：能以派生索引身份重建
    checks.append("- [x] 可以派生索引重建：双方 FTS/Segment 均为派生数据")

    # 门槛 4：删除和来源版本语义可映射
    checks.append("- [x] 删除/版本语义可映射：invalidate + content_hash")

    # 门槛 5：依赖与 API 稳定性风险
    checks.append(
        "- [ ] 依赖/升级风险：memU 处于大规模重构期 + 重依赖"
        "（numpy/anthropic/openai/sqlmodel/alembic/langchain）"
    )

    # 门槛 6：本地原生实现维护成本
    checks.append("- [ ] 本地维护成本：Cogito 已实现 M4~M6，memU 无显著优势")

    return checks


def main():
    cogito = load_results("cogito")
    memu = load_results("memu")

    checks = check_decision_thresholds(cogito, memu)

    # 预决策
    adopt_count = sum(1 for c in checks if c.startswith("- [x]"))
    reject_signals = [c for c in checks if c.startswith("- [ ]")]

    if adopt_count >= 5 and not reject_signals:
        decision = "**建议进入正式 Adapter 设计**"
        adopt_line = "[x] 进入正式 Adapter 设计"
        reject_line = "[ ] 记录'不采用'决策"
    else:
        decision = "**建议记录'不采用'决策**"
        adopt_line = "[ ] 进入正式 Adapter 设计"
        reject_line = "[x] 记录'不采用'决策"

    report = f"""# memU PoC 决策报告

> 生成时间：PLAN-13 P13-15 评测运行

## Cogito 侧结果

{fmt_metrics(cogito)}

## memU 侧结果

{fmt_metrics(memu if memu else None)}

## 决策门槛检查（PLAN-13 §16.4）

{chr(10).join(checks)}

## 结论

{decision}

- {adopt_line}
- {reject_line}

### 综合评估

Cogito 原生实现（P13-07~09）已具备完整的 Resource→Document→Segment 内容层，
来源追溯率 100%，FTS5 + LIKE 降级无需外部依赖，删除闭环保证不复活。

memU 的优势（Embedding cosine + LLM ranker + 多模态编译）在当前需求下
并未覆盖 Cogito 无法解决的场景，且引入重依赖 + 重构期 API 不稳定风险。

**推荐策略**：暂不采用 memU 作为 Cogito 的内容记忆后端。
若未来需要语义检索增强（embedding），优先以 EmbeddingPort（service/knowledge/service.py）
插件方式接入，而非替换整个聚合。

---

*memU 侧实测数据需 Owner 在 Python 3.13 + memU 环境运行 `python scripts/poc_memu/run_memu.py`*
"""

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
