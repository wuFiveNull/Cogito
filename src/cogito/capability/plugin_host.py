"""Minimal subprocess host used by PluginProcessSupervisor.

The host imports and resolves the declared entry point, emits a single ready
record, and then waits for a stop command.  Runtime RPC can be layered on this
framed control channel without granting plugins access to Core internals.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path


def _resolve_entry_point(value: str) -> object:
    if ":" in value:
        module_name, attr_name = value.split(":", 1)
    else:
        module_name, _, attr_name = value.rpartition(".")
    if not module_name:
        module_name, attr_name = value, ""
    module = importlib.import_module(module_name)
    return getattr(module, attr_name) if attr_name else module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--entry-point", required=True)
    parser.add_argument("--plugin-id", required=True)
    args = parser.parse_args(argv)

    source = str(Path(args.source_path).resolve())
    parent = str(Path(source).parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    if source not in sys.path:
        sys.path.insert(0, source)

    try:
        _resolve_entry_point(args.entry_point)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "plugin_id": args.plugin_id,
                    "error_code": type(exc).__name__,
                }
            ),
            flush=True,
        )
        return 2

    print(json.dumps({"status": "ready", "plugin_id": args.plugin_id}), flush=True)
    for line in sys.stdin:
        if line.strip() == "stop":
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
