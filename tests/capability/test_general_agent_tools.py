from __future__ import annotations

import json
from pathlib import Path

import pytest

from cogito.capability.executor import ToolExecutor
from cogito.capability.models import ToolCallState, ToolContext, ToolDef
from cogito.capability.registry import CapabilityRegistry
from cogito.capability.workspace import WorkspaceAccessError, WorkspaceBoundary
from cogito.tools.agent_meta import create_tool_defs as create_meta_tool_defs
from cogito.tools.filesystem import create_tool_defs as create_file_tool_defs


def _context(**overrides: object) -> ToolContext:
    values = {
        "attempt_id": "attempt-1",
        "trace_id": "trace-1",
        "tool_call_id": "call-1",
        "turn_id": "turn-1",
        "tool_state": {},
    }
    values.update(overrides)
    return ToolContext(**values)


def test_workspace_rejects_traversal_and_protected_paths(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    boundary = WorkspaceBoundary.create(str(tmp_path), protected_paths=[".git"])

    with pytest.raises(WorkspaceAccessError, match="parent traversal"):
        boundary.resolve("../outside.txt")
    with pytest.raises(WorkspaceAccessError, match="protected path"):
        boundary.resolve(".git/config", allow_missing=True)


@pytest.mark.asyncio
async def test_file_write_is_atomic_and_readable(tmp_path: Path) -> None:
    boundary = WorkspaceBoundary.create(str(tmp_path))
    tools = {tool.name: tool for tool in create_file_tool_defs(boundary)}
    context = _context()

    written = json.loads(
        await tools["write_file"].handler(
            {"path": "notes/a.txt", "content": "hello"},
            context,
        )
    )
    read = json.loads(
        await tools["read_file"].handler(
            {"path": "notes/a.txt"},
            context,
        )
    )

    assert written["path"] == "notes/a.txt"
    assert written["before_sha256"] is None
    assert len(written["after_sha256"]) == 64
    assert read["content"] == "1: hello"
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_large_file_can_be_paged_and_grepped_without_full_read(tmp_path: Path) -> None:
    content = "".join(f"line-{number}\n" for number in range(1, 400))
    (tmp_path / "large.txt").write_text(content, encoding="utf-8")
    boundary = WorkspaceBoundary.create(str(tmp_path), max_read_bytes=1_024)
    tools = {tool.name: tool for tool in create_file_tool_defs(boundary)}

    page = json.loads(
        await tools["read_file"].handler(
            {"path": "large.txt", "offset": 350, "limit": 3, "max_bytes": 200},
            _context(),
        )
    )
    matches = json.loads(
        await tools["grep"].handler(
            {"path": "large.txt", "pattern": "line-399"},
            _context(),
        )
    )

    assert page["content"].splitlines() == [
        "350: line-350", "351: line-351", "352: line-352",
    ]
    assert page["total_lines"] == 399
    assert page["next_offset"] == 353
    assert page["size_bytes"] > boundary.max_read_bytes
    assert matches["matches"][0]["line"] == 399


@pytest.mark.asyncio
async def test_side_effect_tool_emits_bound_receipt() -> None:
    registry = CapabilityRegistry()

    async def handler(args: dict, context: ToolContext) -> str:
        return json.dumps({"changed": True})

    registry.register(
        ToolDef(
            "write_something",
            "write",
            {"type": "object", "properties": {"value": {"type": "string"}}},
            handler,
            side_effect_class="reconcilable",
        )
    )

    class Sink:
        def __init__(self) -> None:
            self.receipts: list[dict] = []

        def insert(self, record: object) -> None:
            pass

        def insert_receipt(self, record: dict) -> str:
            self.receipts.append(record)
            return "receipt-1"

    sink = Sink()
    result = await ToolExecutor(registry, sink=sink).execute(
        "call-1",
        "write_something",
        {"value": "x"},
        _context(),
    )

    assert result.status == "success"
    assert sink.receipts[0]["capability_id"] == "core:write_something"
    assert len(sink.receipts[0]["request_hash"]) == 64


@pytest.mark.asyncio
async def test_tool_search_activates_deferred_schema() -> None:
    registry = CapabilityRegistry()

    async def hidden_handler(args: dict, context: ToolContext) -> str:
        return "ok"

    registry.register(
        ToolDef(
            "hidden_tool",
            "A deferred hidden capability",
            {"type": "object"},
            hidden_handler,
            toolset=("extra",),
            deferred=True,
        )
    )
    for tool in create_meta_tool_defs(registry):
        registry.register(tool)

    exposed: set[str] = set()
    context = _context(expose_tool=lambda capability_id: not exposed.add(capability_id))
    search = registry.resolve("tool_search")
    result = json.loads(await search.handler({"query": "hidden"}, context))

    assert result["tools"][0]["activated"] is True
    assert not registry.get_openai_schemas({"extra"})
    schemas = registry.get_openai_schemas({"extra"}, exposed)
    assert schemas[0]["function"]["name"] == "hidden_tool"


@pytest.mark.asyncio
async def test_batch_stops_when_approval_is_required() -> None:
    registry = CapabilityRegistry()
    executed: list[str] = []

    async def handler(args: dict, context: ToolContext) -> str:
        executed.append(context.tool_call_id)
        return "ok"

    registry.register(
        ToolDef(
            "guarded",
            "guarded",
            {"type": "object"},
            handler,
            approval_policy="always",
        )
    )
    registry.register(ToolDef("later", "later", {"type": "object"}, handler))

    class ApprovalService:
        def find_or_create_tool_approval(self, **kwargs: object) -> object:
            return type("Approval", (), {"approval_id": "approval-1"})()

    executor = ToolExecutor(registry, approval_service=ApprovalService())
    results = await executor.execute_many(
        [
            ToolCallState("call-1", "guarded", {}),
            ToolCallState("call-2", "later", {}),
        ],
        _context(),
    )

    assert [result.status for result in results] == ["approval_required"]
    assert executed == []


@pytest.mark.asyncio
async def test_executor_fails_closed_when_intent_cannot_be_persisted() -> None:
    registry = CapabilityRegistry()
    executed = False

    async def handler(args: dict, context: ToolContext) -> str:
        nonlocal executed
        executed = True
        return "ok"

    class BrokenSink:
        def insert(self, record: object) -> None:
            raise RuntimeError("database unavailable")

    registry.register(ToolDef("guarded_write", "write", {"type": "object"}, handler))
    result = await ToolExecutor(registry, sink=BrokenSink()).execute(
        "call-1", "guarded_write", {}, _context(),
    )

    assert result.status == "error"
    assert "intent persistence failed" in result.error_message
    assert executed is False


@pytest.mark.asyncio
async def test_executor_enforces_snapshot_and_output_schema() -> None:
    registry = CapabilityRegistry()

    async def handler(args: dict, context: ToolContext) -> str:
        return json.dumps({"value": "wrong"})

    registry.register(
        ToolDef(
            "structured", "structured", {"type": "object"}, handler,
            output_schema={
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        )
    )
    executor = ToolExecutor(registry)
    hidden = await executor.execute(
        "call-1", "structured", {},
        _context(capability_snapshot_ids=("core:other",)),
    )
    invalid = await executor.execute(
        "call-2", "structured", {},
        _context(capability_snapshot_ids=("core:structured",)),
    )

    assert "immutable snapshot" in hidden.error_message
    assert invalid.status == "error"
    assert "not of type 'integer'" in invalid.error_message


@pytest.mark.asyncio
async def test_executor_validates_plain_text_string_output_schema() -> None:
    registry = CapabilityRegistry()

    async def handler(args: dict, context: ToolContext) -> str:
        return "plain text is valid"

    registry.register(
        ToolDef(
            "text", "text", {"type": "object"}, handler,
            output_schema={"type": "string", "minLength": 1},
        )
    )
    result = await ToolExecutor(registry).execute("call-text", "text", {}, _context())

    assert result.status == "success"
    assert result.result == "plain text is valid"


@pytest.mark.asyncio
async def test_uncertain_side_effect_queues_reconciliation_without_retry() -> None:
    registry = CapabilityRegistry()

    async def handler(args: dict, context: ToolContext) -> str:
        raise ConnectionError("connection dropped")

    class Sink:
        def __init__(self) -> None:
            self.receipts: list[dict] = []
            self.reconcile: list[dict] = []

        def insert(self, record: object) -> None:
            pass

        def insert_receipt(self, record: dict) -> str:
            self.receipts.append(record)
            return "receipt-1"

        def enqueue_reconcile(self, record: dict) -> str:
            self.reconcile.append(record)
            return "task-1"

    registry.register(
        ToolDef(
            "external_write", "write", {"type": "object"}, handler,
            side_effect_class="reconcilable",
        )
    )
    sink = Sink()
    result = await ToolExecutor(registry, sink=sink).execute(
        "call-1", "external_write", {}, _context(),
    )

    assert result.status == "error"
    assert sink.receipts[0]["status"] == "unknown"
    assert sink.receipts[0]["reconcile_status"] == "pending"
    assert sink.reconcile[0]["receipt_id"] == "receipt-1"
