"""Cogito CLI entry point."""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="cogito", description="Cogito — 主动式个人 Agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize project (config + database)")
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
    print("Initializing Cogito project...")
    # TODO: Phase 1b — create config, init database
    print("Done.")


def _cmd_info() -> None:
    from cogito import __version__
    print(f"Cogito v{__version__}")
    print("Python 3.12+ personal agent framework (architecture preview)")


if __name__ == "__main__":
    main()
