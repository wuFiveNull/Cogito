from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from cogito.capability.models import ToolContext
from cogito.config import MultimodalConfig
from cogito.contracts.context import ContextBuilder
from cogito.contracts.envelope import ChannelEnvelope
from cogito.model.contracts import ModelCapabilities, ModelResponse
from cogito.domain.task import Task, TaskStatus
from cogito.service.asset_service import AssetIngestionService
from cogito.service.inbound_service import InboundService
from cogito.service.vision_service import (
    MultimodalContextProjection,
    VisionAnalysisError,
    VisionAnalysisService,
)
from cogito.service.task_dispatcher import TaskDispatcher
from cogito.service.task_handlers import TaskHandlerContext, _build_registry
from cogito.service.task_worker import TaskRunOutcome, TaskWorker
from cogito.store.task_repo import TaskRepository
from cogito.tools.analyze_multimodal_asset import create_tool_def


# Valid 1x1 PNG. Keeping it inline makes the test independent of Pillow.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4"
    "//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


class _FakeVisionProvider:
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            context_window=32_000,
            max_output_tokens=2_000,
            modalities=("text", "image"),
            supports_json_schema=True,
        )


class _FakeVisionRouter:
    def __init__(self) -> None:
        self.calls = 0
        self.provider = _FakeVisionProvider()

    def get_provider(self, role: str = "main") -> _FakeVisionProvider:
        assert role == "vlm"
        return self.provider

    async def generate(self, request, model_role: str = "main") -> ModelResponse:
        self.calls += 1
        assert model_role == "vlm"
        blocks = request.messages[0]["content"]
        assert blocks[1]["type"] == "image_url"
        assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return ModelResponse(
            request_id=request.request_id,
            model_id="fake-vision-1",
            structured_output={
                "short_description": "A single white pixel.",
                "detailed_description": "The image is a one-pixel PNG test fixture.",
                "extracted_text": "",
                "objects": ["pixel"],
                "document_type": "image",
                "metadata": {"fixture": True},
            },
        )


def _config() -> MultimodalConfig:
    return MultimodalConfig(
        enabled=True,
        auto_analyze=True,
        inline_wait_seconds=1.0,
        tool_timeout_seconds=2.0,
    )


def _accept_image(conn, tmp_path):
    config = _config()
    asset_service = AssetIngestionService(conn, str(tmp_path), config)
    inbound = InboundService(conn, asset_service=asset_service)
    result = inbound.accept(ChannelEnvelope(
        channel_type="web",
        channel_instance_id="web-test",
        platform_sender_id="owner",
        platform_conversation_id="conv-mm",
        platform_message_id="platform-mm-1",
        content_parts=[
            {"content_type": "text", "inline_data": "What is in this image?"},
            {
                "content_type": "image",
                "inline_data": PNG_DATA_URI,
                "mime": "image/png",
                "name": "pixel.png",
                "size": len(PNG_BYTES),
            },
        ],
        trust_label="unverified",
    ))
    row = conn.execute(
        "SELECT m.session_id,m.sender_principal_id,l.asset_id,cp.inline_data,cp.payload_ref "
        "FROM messages m JOIN content_parts cp ON cp.message_id=m.message_id "
        "JOIN message_asset_links l ON l.part_id=cp.part_id "
        "WHERE m.message_id=?",
        (result.message_id,),
    ).fetchone()
    return result, row


def test_schema_and_asset_ingestion_deduplicate(in_memory_db, tmp_path):
    result, row = _accept_image(in_memory_db, tmp_path)
    assert result.message_id
    assert row["inline_data"] == ""
    assert row["payload_ref"]

    counts = {
        table: in_memory_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("payload_objects", "multimodal_assets", "message_asset_links")
    }
    assert counts == {
        "payload_objects": 1,
        "multimodal_assets": 1,
        "message_asset_links": 1,
    }

    # Exact duplicate bytes reuse the payload and asset; pHash is not involved.
    inbound = InboundService(
        in_memory_db,
        asset_service=AssetIngestionService(in_memory_db, str(tmp_path), _config()),
    )
    inbound.accept(ChannelEnvelope(
        channel_type="web",
        channel_instance_id="web-test",
        platform_sender_id="owner",
        platform_conversation_id="conv-mm",
        platform_message_id="platform-mm-2",
        content_parts=[{
            "content_type": "image",
            "inline_data": PNG_DATA_URI,
            "mime": "image/png",
        }],
    ))
    assert in_memory_db.execute("SELECT COUNT(*) FROM payload_objects").fetchone()[0] == 1
    assert in_memory_db.execute("SELECT COUNT(*) FROM multimodal_assets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_vision_cache_context_and_scoped_tool(in_memory_db, tmp_path):
    result, row = _accept_image(in_memory_db, tmp_path)
    router = _FakeVisionRouter()
    service = VisionAnalysisService(
        in_memory_db,
        str(tmp_path),
        router,
        _config(),
        model_id="fake-vision-1",
    )

    analyses = service.request_message_assets(result.message_id)
    assert len(analyses) == 1
    assert in_memory_db.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_type='vision.analyze'"
    ).fetchone()[0] == 1

    first = await service.analyze(analyses[0].analysis_id)
    second = await service.analyze(analyses[0].analysis_id)
    assert first.status.value == "succeeded"
    assert second.short_description == "A single white pixel."
    assert router.calls == 1

    projection = MultimodalContextProjection(
        in_memory_db,
        model_id="fake-vision-1",
        config=_config(),
    )
    snapshot = ContextBuilder(
        in_memory_db,
        multimodal_reader=projection,
    ).build("turn-mm", row["session_id"], result.message_id)
    content = "\n".join(item.content for item in snapshot.items)
    assert "A single white pixel." in content
    assert row["asset_id"] in content
    assert "iVBOR" not in content
    assert "<external_data trust=\"unverified\">" in content

    tool = create_tool_def(make_service=lambda: service)
    allowed = await tool.handler({"asset_id": row["asset_id"]}, ToolContext(
        attempt_id="a1",
        trace_id="tr1",
        tool_call_id="tc1",
        principal_id=row["sender_principal_id"],
        session_id=row["session_id"],
    ))
    assert '"vision_status": "succeeded"' in allowed

    denied = await tool.handler({"asset_id": row["asset_id"]}, ToolContext(
        attempt_id="a2",
        trace_id="tr2",
        tool_call_id="tc2",
        principal_id="another-principal",
        session_id="another-session",
    ))
    assert '"vision_status": "denied"' in denied
    assert router.calls == 1


@pytest.mark.asyncio
async def test_retryable_vision_task_creates_new_attempt(in_memory_db):
    class _FlakyVisionService:
        def __init__(self) -> None:
            self.calls = 0

        async def analyze(self, analysis_id: str):
            self.calls += 1
            if self.calls == 1:
                raise VisionAnalysisError("temporary timeout", retryable=True)
            return SimpleNamespace(
                analysis_id=analysis_id,
                status=SimpleNamespace(value="succeeded"),
            )

    service = _FlakyVisionService()
    task = Task(
        task_id="vision-retry-task",
        task_type="vision.analyze",
        payload_ref=json.dumps({"analysis_id": "analysis-retry"}),
        status=TaskStatus.queued,
        retry_policy={"max_attempts": 3, "backoff_seconds": [0, 0]},
        idempotency_key="vision-retry",
    )
    TaskRepository(in_memory_db).insert(task)
    in_memory_db.commit()

    ctx = TaskHandlerContext(vision_service_factory=lambda: service)
    worker = TaskWorker(
        in_memory_db,
        TaskDispatcher(in_memory_db),
        _build_registry(ctx),
        ctx,
    )

    assert await worker.run_once("vision-worker") == TaskRunOutcome.failed
    assert in_memory_db.execute(
        "SELECT status FROM tasks WHERE task_id=?", (task.task_id,),
    ).fetchone()[0] == "scheduled"

    assert await worker.run_once("vision-worker") == TaskRunOutcome.completed
    assert in_memory_db.execute(
        "SELECT status FROM tasks WHERE task_id=?", (task.task_id,),
    ).fetchone()[0] == "completed"
    assert in_memory_db.execute(
        "SELECT COUNT(*) FROM task_attempts WHERE task_id=?", (task.task_id,),
    ).fetchone()[0] == 2
