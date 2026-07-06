"""Quick test to debug ContextAssemblyPhase validation."""
import asyncio, tempfile, os, sys
sys.path.insert(0, "D:/Code/PythonCode/cogito-v1")

from cogito.database import AsyncDatabase, run_migrations
from cogito.agent.bootstrap.runtime_factory import build_runtime_kernel, build_state_load_adapters
from cogito.agent.ports.defaults import *
from cogito.agent.runtime.models import AgentRequest


async def test():
    tmp = tempfile.mktemp(suffix=".db")
    db = AsyncDatabase(tmp)
    await db.open()
    await run_migrations(db)
    await db.execute(
        "INSERT INTO sessions (session_id, user_id, version, next_seq_no, created_at, updated_at) "
        "VALUES (:sid, :uid, 0, 1, :n, :n)",
        {"sid": "s1", "uid": "u1", "n": "2026-01-01T00:00:00.000Z"},
    )
    await db.execute("INSERT INTO user_settings (actor_id) VALUES (:uid)", {"uid": "u1"})

    a = build_state_load_adapters(db)
    kernel = build_runtime_kernel(
        clock=SystemClock(),
        id_generator=Uuid7Generator(),
        model=None,
        context_window=DefaultModelContextWindow(),
        tool_registry=DefaultToolRegistry(),
        tool_policy=DefaultToolPolicy(),
        tool_executor=DefaultToolExecutor(),
        session_repository=a[0],
        message_repository=a[1],
        summary_repository=a[2],
        user_profile_repository=a[3],
        user_settings_repository=a[4],
        session_config_repository=a[5],
    )

    try:
        result = await kernel.run(
            AgentRequest(request_id="r1", session_id="s1", actor_id="u1", text="hello"),
        )
        print(f"Status: {result.status}, Text: {result.text!r}")
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")

    await db.close()
    os.unlink(tmp)


asyncio.run(test())
