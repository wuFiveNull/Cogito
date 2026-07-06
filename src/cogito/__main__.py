"""Cogito CLI entry point."""

import argparse
import sys
from pathlib import Path

from cogito import __version__
from cogito.config import DEFAULT_CONFIG_PATH, Config
from cogito.store.connection import get_connection
from cogito.store.migration import migrate


def main() -> None:
    parser = argparse.ArgumentParser(prog="cogito", description="Cogito — 主动式个人 Agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize workspace and database")
    sub.add_parser("info", help="Show system info")

    args = parser.parse_args()
    if args.command == "init":
        _cmd_init()
    elif args.command == "info":
        _cmd_info()
    else:
        parser.print_help()
        sys.exit(1)


def _cmd_init() -> None:
    """Create .workspace/ directory and initialize the database."""
    config = Config.load()
    workspace = Path(config.workspace.path)
    workspace.mkdir(parents=True, exist_ok=True)

    db_path = config.resolve_db_path()
    payload_dir = Path(config.workspace.payload_dir)
    payload_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(config.workspace.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    conn = get_connection(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    print(f"[ok] Workspace: {workspace.resolve()}")
    print(f"[ok] Database:  {Path(db_path).resolve()}")
    print(f"[ok] Payloads:  {payload_dir.resolve()}")
    print(f"[ok] Logs:      {log_dir.resolve()}")
    print("Done.")


def _cmd_info() -> None:
    print(f"Cogito v{__version__}")
    print("Python 3.12+ personal agent framework (architecture preview)")
    print()
    config = Config.load()
    print(f"Config file:   {DEFAULT_CONFIG_PATH.resolve()}")
    print(f"Workspace:     {config.workspace.path}")
    print(f"Database path: {config.resolve_db_path()}")
    print(f"Payload dir:   {config.workspace.payload_dir}")
    print(f"Log dir:       {config.workspace.log_dir}")


if __name__ == "__main__":
    main()
