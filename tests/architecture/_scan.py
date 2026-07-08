"""Architecture dependency scanner — stdlib only (ast).

Reusable by CI to reproduce docs/architecture/current-dependency-map.py.
Avoids third-party tools so the test suite stays hermetic.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set, Tuple

PKG_ROOT = Path(__file__).resolve().parents[2] / "src" / "cogito"


def _top_module(rel: Path) -> str:
    """Map src/cogito/x/y.py -> cogito.x, src/cogito/x.py -> cogito.x."""
    fwd = str(rel).replace("\\", "/")
    parts = fwd.split("/")
    if len(parts) >= 2 and parts[0] == "cogito":
        return parts[0] + "." + parts[1].rsplit(".", 1)[0]
    return parts[0].rsplit(".", 1)[0]


def scan_imports(
    root: Path = PKG_ROOT,
) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """Return ({top_module -> {imported_top_module}}, all_modules).

    Only counts imports whose target starts with `cogito.`, so external
    libraries don't appear in the dependency graph.
    """
    graph: Dict[str, Set[str]] = {}
    all_modules: Set[str] = set()
    for py in root.rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        try:
            rel = py.relative_to(root.parent)
        except ValueError:
            continue
        top = _top_module(rel)
        all_modules.add(top)
        graph.setdefault(top, set())
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("cogito."):
                    ip = node.module.split(".")
                    graph[top].add(ip[0] + "." + ip[1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("cogito."):
                        ip = alias.name.split(".")
                        graph[top].add(ip[0] + "." + ip[1])
    return graph, all_modules


def forbidden_edges(
    graph: Dict[str, Set[str]],
) -> Dict[str, Set[str]]:
    """Apply SYSTEM-BOUNDARIES / 2 rules and return the remaining violations.

    Declaration site (called by tests): enumerate every (src, dst) that
    violates a hard rule, grouping by the responsible src module.
    """
    # Hard禁令：domain / contracts / config 不得依赖任何基础设施子包
    pure_layers = {"cogito.domain", "cogito.contracts", "cogito.config"}
    infra_layers = {
        "cogito.store",
        "cogito.model",
        "cogito.capability",
        "cogito.runtime",
        "cogito.service",
        "cogito.interaction_web",
        "cogito.channel",
        "cogito.inbound",
        "cogito.tools",
        "cogito.bench",
    }
    # Agent Runtime 不得直接写 Repository
    runtime_forbidden = {"cogito.store"}
    # Dashboard 不得直接执行写 SQL
    web_forbidden = {"cogito.store"}

    violations: Dict[str, Set[str]] = {}
    for src, dests in graph.items():
        blocked: Set[str] = set()
        if src in pure_layers:
            blocked |= dests & infra_layers
        if src == "cogito.runtime":
            blocked |= dests & runtime_forbidden
        if src == "cogito.interaction_web":
            blocked |= dests & web_forbidden
        if blocked:
            violations[src] = blocked
    return violations


def cycles(graph: Dict[str, Set[str]]) -> list[list[str]]:
    """Detect import cycles via iterative DFS; returns list of cycles found."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {m: WHITE for m in graph}
    path: list[str] = []
    found: list[list[str]] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        path.append(u)
        for v in sorted(graph.get(u, set())):
            if v == u:
                continue  # intra-package import, not a cycle
            if v not in color:
                continue
            if color[v] == GRAY:
                idx = path.index(v)
                found.append(path[idx:] + [v])
            elif color[v] == WHITE:
                dfs(v)
        path.pop()
        color[u] = BLACK

    for m in sorted(graph):
        if color[m] == WHITE:
            dfs(m)
    return found


if __name__ == "__main__":
    g, mods = scan_imports()
    print("Modules:", len(mods))
    viol = forbidden_edges(g)
    if viol:
        print("Forbidden edges:")
        for s, ds in viol.items():
            print(f"  {s} -> {sorted(ds)}")
    else:
        print("No forbidden edges.")
    cyc = cycles(g)
    if cyc:
        print("Cycles:")
        for c in cyc:
            print("  " + " -> ".join(c))
    else:
        print("No cycles.")
