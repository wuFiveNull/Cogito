"""Runtime shutdown regression tests."""

from __future__ import annotations

import asyncio
import sqlite3

from cogito.application import RuntimeApplication


def test_shutdown_drains_worker_before_closing_sqlite() -> None:
    async def scenario() -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE shutdown_probe (value TEXT NOT NULL)")
        app = RuntimeApplication(
            config=None,  # type: ignore[arg-type]
            conn=conn,
            provider=None,  # type: ignore[arg-type]
            runner=None,
            inbound=None,  # type: ignore[arg-type]
        )
        app._shutdown_event = asyncio.Event()

        async def in_flight_worker() -> None:
            await app._shutdown_event.wait()
            await asyncio.sleep(0)
            conn.execute("INSERT INTO shutdown_probe VALUES ('drained')")
            conn.commit()

        worker = asyncio.create_task(in_flight_worker())
        app._worker_loop_task = worker

        await app.shutdown()

        assert worker.done()
        assert app._closed is True
        # A second shutdown is intentionally harmless.
        await app.shutdown()

    asyncio.run(scenario())
