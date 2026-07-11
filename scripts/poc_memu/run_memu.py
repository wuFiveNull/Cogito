"""memU 侧评测入口（PLAN-13 P13-15）。

Owner 在 Python 3.13 + memU 环境执行：

    conda activate memu-poc          # Python 3.13 + memu-py
    python scripts/poc_memu/run_memu.py \\
        --input scripts/poc_memu/results/dataset.json \\
        --output scripts/poc_memu/results/memu_results.json

隔离约束：使用临时内存数据库（:memory:），不污染生产。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))


def main():
    parser = argparse.ArgumentParser(description="memU PoC evaluation")
    parser.add_argument("--input", required=True, help="dataset.json 路径")
    parser.add_argument("--output", default="scripts/poc_memu/results/memu_results.json")
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()

    dataset = json.loads(Path(args.input).read_text(encoding="utf-8"))

    try:
        import memu  # noqa: F401
    except ImportError:
        print("memU 未安装。请在 Python 3.13 + memU 环境执行：")
        print("  conda create -n memu-poc python=3.13 -y && conda activate memu-poc")
        print("  pip install memu-py")
        sys.exit(1)

    # 构建 memU 后端（实现 protocol.KnowledgeBackend）
    _build_memu_backend()

    from scripts.poc_memu.metrics import evaluate_backend

    docs = [
        type("Doc", (), {"doc_id": d["doc_id"], "content": d["content"],
                          "expected_queries": d["expected_queries"]})
        for d in dataset
    ]

    start = time.perf_counter()
    metrics = evaluate_backend(docs, top_k=args.top_k)
    elapsed = time.perf_counter() - start

    metrics["total_eval_time_s"] = round(elapsed, 3)
    metrics["backend"] = "memu"

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"\nResults written to {out_path}")


def _build_memu_backend():
    """构建 memU 后端。

    实现 protocol.KnowledgeBackend 协议。
    使用临时内存数据库。
    """
    # 具体 memU API 调用需参考 reference/memU 源码
    # （app/service.py、app/memorize.py、app/retrieve.py）
    # 以下为协议骨架，Owner 在 memU 环境补充实现。
    raise NotImplementedError(
        "memU 后端需要 Owner 在 Python 3.13 + memU 环境，"
        "参考 reference/memU/src/memu/app 的 memorize/retrieve 流程实现。"
        "参见 scripts/poc_memu/README.md。"
    )


if __name__ == "__main__":
    main()
