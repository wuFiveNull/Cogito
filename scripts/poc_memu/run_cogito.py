"""运行 Cogito 侧评测（PLAN-13 P13-15）。

用法：
    python scripts/poc_memu/run_cogito.py
    python scripts/poc_memu/run_cogito.py --export dataset.json
    python scripts/poc_memu/run_cogito.py --output results/custom.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 将 src 加入路径（scripts 不在 packages.find 中）
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from poc_memu.cogito_backend import CogitoBackend
from poc_memu.dataset import generate_dataset
from poc_memu.metrics import evaluate_backend, retrieval_latency


def main():
    parser = argparse.ArgumentParser(description="Cogito PoC evaluation")
    parser.add_argument("--export", help="导出数据集到 JSON 后退出")
    parser.add_argument(
        "--output", default="scripts/poc_memu/results/cogito_results.json", help="结果输出路径"
    )
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()

    dataset = generate_dataset(seed=42)

    if args.export:
        out = [
            {"doc_id": d.doc_id, "content": d.content, "expected_queries": d.expected_queries}
            for d in dataset
        ]
        Path(args.export).parent.mkdir(parents=True, exist_ok=True)
        Path(args.export).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Exported {len(dataset)} docs to {args.export}")
        return

    backend = CogitoBackend()
    try:
        start = time.perf_counter()
        metrics = evaluate_backend(backend, dataset, top_k=args.top_k)
        elapsed = time.perf_counter() - start

        # 延迟评测（取第一个 query）
        first_doc = dataset[0]
        latency = retrieval_latency(
            backend.retrieve,
            first_doc.expected_queries[0],
            top_k=args.top_k,
        )

        metrics["latency_ms"] = latency
        metrics["total_eval_time_s"] = round(elapsed, 3)
        metrics["backend"] = "cogito_native"

        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        print(f"\nResults written to {out_path}")
    finally:
        backend.close()


if __name__ == "__main__":
    main()
