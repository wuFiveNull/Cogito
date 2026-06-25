#!/usr/bin/env python3
"""
启动 Cogito Web 服务（完整管线 + SQLite 持久化）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname).1s %(name)s | %(message)s",
)

logger = logging.getLogger("start")


async def main():
    from cogito.bootstrap.application import create_application

    app = await create_application()
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("bye")
