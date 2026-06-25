"""验证 Phase 3 应用集成"""
import asyncio
import os
import tempfile

from cogito.database import DatabaseManager


async def test():
    tmp = tempfile.mktemp(suffix=".db")
    db = DatabaseManager(tmp)
    try:
        await db.open()
        print(f"DB path: {db.db_path}")
        print(f"Health check: {await db.health_check()}")

        # 验证所有服务已挂载
        print(f"Trace service:     {db.trace.__class__.__name__}")
        print(f"Trace repo:        {db.trace_events.__class__.__name__}")
        print(f"Event service:     {db.event.__class__.__name__}")
        print(f"Event repo:        {db.events.__class__.__name__}")
        print(f"Memory writer:     {db.memory_writer.__class__.__name__}")
        print(f"Memory retriever:  {db.memory_retriever.__class__.__name__}")
        print(f"Memory repo:       {db.memories.__class__.__name__}")

        print("** Phase 3 integration passed! **")
    finally:
        await db.close()
        os.unlink(tmp)


asyncio.run(test())
