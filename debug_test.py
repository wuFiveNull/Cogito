"""Debug: AgentLoop max_tool_calls in test context."""
import sys, asyncio
sys.path.insert(0, 'src')

from cogito.capability import CapabilityRegistry
from cogito.capability.executor import ToolExecutor
from cogito.capability.models import ToolDef, ToolContext
from cogito.model.router import ModelRouter
from cogito.model.stub_provider import StubModelProvider, StubScenario
from cogito.model.contracts import FinishReason
from cogito.runtime.context import ContextItem, ContextSnapshot
from cogito.runtime.loop import AgentLoop


async def _echo_handler(args, ctx):
    return args.get('text', '')


async def _noop_handler(args, ctx):
    return 'done'


def _make_registry():
    r = CapabilityRegistry()
    r.register(ToolDef(name='echo', description='Echo', input_schema={'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}, handler=_echo_handler, risk_level='low'))
    r.register(ToolDef(name='noop', description='Noop', input_schema={'type': 'object', 'properties': {}}, handler=_noop_handler, risk_level='low'))
    return r


def _make_loop(scenarios, toolsets=None, registry=None, **kwargs):
    provider = StubModelProvider(scenarios)
    router = ModelRouter(providers={'stub': provider}, role_map={'main': 'stub'})
    resolved_registry = registry or _make_registry()
    executor = ToolExecutor(resolved_registry)
    print(f"Creating AgentLoop with kwargs: {kwargs}")
    return AgentLoop(router=router, registry=resolved_registry, executor=executor, toolsets=toolsets or {'core'}, **kwargs)


def _make_snapshot():
    return ContextSnapshot(snapshot_id='snap1', turn_id='t1', session_id='s1',
        items=(ContextItem(item_type='message', item_id='m1', source='s1', tokens=5, content='hello'),),
        total_tokens=5, created_at=1000)


async def main():
    scenarios = [
        StubScenario(
            finish_reason=FinishReason.tool_calls,
            tool_calls=({"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "' + str(i) + '"}'}},),
        )
        for i in range(10)
    ]
    loop = _make_loop(
        scenarios,
        max_tool_calls=3,
        max_repeated_tool_signature=10,
        max_iterations=100,
    )
    print(f"loop._max_tool_calls: {loop._max_tool_calls}")
    print(f"loop._max_iterations: {loop._max_iterations}")
    print(f"loop._max_repeated_tool_signature: {loop._max_repeated_tool_signature}")

    result = await loop.run(_make_snapshot())
    print(f'Result: {result.result_type}')
    print(f'Iterations: {result.iterations}')
    print(f'Tool calls: {result.tool_call_count}')

asyncio.run(main())
