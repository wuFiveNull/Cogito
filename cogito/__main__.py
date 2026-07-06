# cogito/__main__.py

import argparse
import asyncio


def parse_args():
    parser = argparse.ArgumentParser(prog="cogito")

    parser.add_argument(
        "--config",
        help="path to config.toml",
    )

    return parser.parse_args()


async def async_main(config_path: str | None) -> None:
    from .bootstrap import create_application

    app = await create_application(config_path)

    try:
        await app.run()
    finally:
        await app.close()


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args.config))


if __name__ == "__main__":
    main()
