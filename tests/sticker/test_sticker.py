"""Tests for sticker storage, tools, SSRF-safe fetch, and gateway image read."""

from __future__ import annotations

import base64
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cogito.capability.models import ToolContext
from cogito.config import MultimodalConfig
from cogito.domain.conversation import Conversation, ConversationStatus, ConversationType, ContextPartitionPolicy, Session, SessionStatus
from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
from cogito.domain.multimodal import MultimodalAsset
from cogito.infrastructure.multimodal_metrics import MultimodalMetrics
from cogito.infrastructure.payload_store import PayloadStore
from cogito.infrastructure.safe_http import SafeHttpError, fetch_url_bytes
from cogito.service.asset_service import AssetIngestionService
from cogito.service.sticker_service import SqliteStickerService
from cogito.store.migration import migrate
from cogito.store.multimodal_repo import MultimodalRepository
from cogito.store.repositories import MessageRepository, SessionRepository, ConversationRepository
from cogito.tools.sticker import (
    create_save_sticker_def,
    create_save_sticker_from_url_def,
    create_send_sticker_def,
)


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNg"
    "AAIAAAUAAeImBZsAAAAASUVORK5CYII="
)


def _ctx(principal: str, session: str) -> ToolContext:
    return ToolContext(
        attempt_id="a1", trace_id="t1", tool_call_id="tc1",
        principal_id=principal, session_id=session,
    )


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def config() -> MultimodalConfig:
    return MultimodalConfig(enabled=True, allowed_sticker_hosts=("i.imgur.com",))


@pytest.fixture
def repo(db: sqlite3.Connection) -> MultimodalRepository:
    return MultimodalRepository(db)


@pytest.fixture
def payload_store(db: sqlite3.Connection, tmp_path: Path) -> PayloadStore:
    return PayloadStore(str(tmp_path / "payload"), db)


@pytest.fixture
def asset_service(db: sqlite3.Connection, tmp_path: Path, config: MultimodalConfig) -> AssetIngestionService:
    return AssetIngestionService(db, str(tmp_path / "payload"), config)


def _seed_image(
    repo: MultimodalRepository,
    payload_store: PayloadStore,
    db: sqlite3.Connection,
    *,
    principal: str = "owner",
    idx: int = 0,
) -> MultimodalAsset:
    data = PNG_BYTES + bytes([idx])
    payload = payload_store.put(data, content_type="image/png")
    db.execute(
        "INSERT OR IGNORE INTO payload_objects "
        "(payload_ref,sha256,content_type,size,storage_path,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (payload.payload_id, payload.sha256, "image/png",
         payload.size_bytes, payload.storage_uri, int(time.time() * 1000)),
    )
    db.commit()
    asset = MultimodalAsset(
        asset_id=payload.payload_id,
        payload_ref=payload.payload_id,
        sha256=payload.sha256,
        media_kind="image",
        mime_type="image/png",
        size_bytes=payload.size_bytes,
        created_by_principal_id=principal,
        created_at=int(time.time() * 1000),
    )
    repo.insert_asset(asset)
    # Create a message + part + link so is_accessible(owner, s1) resolves.
    # Use the canonical repositories to satisfy every FK.
    # Each image needs a distinct message to avoid (conversation_id,
    # receive_sequence) and message_id collisions. Use payload_id as suffix.
    msg_id = f"m-link-{payload.payload_id}"
    part_id = f"part-{payload.payload_id}"
    msg_repo = MessageRepository(db)
    msg = Message(
        message_id=msg_id,
        conversation_id="conv1",
        session_id="s1",
        sender_principal_id=principal,
        role=MessageRole.user,
        direction=MessageDirection.inbound,
        content_parts=[ContentPart(
            part_id=part_id, content_type="image",
            payload_ref=asset.payload_ref, size=asset.size_bytes,
            sha256=asset.sha256, metadata={"mime": "image/png", "name": "cat.png"},
            ordinal=0,
        )],
        receive_sequence=int(payload.payload_id, 16) % 100000,
        created_at=datetime.now(UTC),
    )
    msg_repo.insert(msg)
    msg_repo.insert_content_part(msg.content_parts[0], msg.message_id)
    repo.link_message_asset(
        message_id=msg_id, part_id=part_id,
        asset_id=asset.asset_id, ordinal=0, original_filename="cat.png",
    )
    return asset


def _seed_conversation(db: sqlite3.Connection) -> str:
    conv = Conversation(
        conversation_id="conv1",
        status=ConversationStatus.active,
        conversation_type=ConversationType.private,
        context_partition_policy=ContextPartitionPolicy.isolated,
    )
    ConversationRepository(db).insert(conv)
    db.commit()
    return conv.conversation_id


def _seed_session(db: sqlite3.Connection, conversation_id: str) -> str:
    sess = Session(
        session_id="s1",
        conversation_id=conversation_id,
        created_at=datetime.now(UTC),
        status=SessionStatus.active,
    )
    SessionRepository(db).insert(sess)
    db.commit()
    return sess.session_id


def _seed_message_with_image(
    db: sqlite3.Connection,
    repo: MultimodalRepository,
    payload_store: PayloadStore,
    *,
    principal: str = "owner",
    session: str = "s1",
    conversation: str = "conv1",
    message_id: str = "m1",
) -> MultimodalAsset:
    asset = _seed_image(repo, payload_store, db, principal=principal)
    msg_repo = MessageRepository(db)
    msg = Message(
        message_id=message_id,
        conversation_id=conversation,
        session_id=session,
        sender_principal_id=principal,
        role=MessageRole.user,
        direction=MessageDirection.inbound,
        content_parts=[
            ContentPart(content_type="text", inline_data="hello", ordinal=0),
            ContentPart(
                content_type="image", payload_ref=asset.payload_ref,
                size=asset.size_bytes, sha256=asset.sha256,
                metadata={"mime": "image/png", "name": "cat.png"}, ordinal=1,
            ),
        ],
        receive_sequence=1,
        created_at=datetime.now(UTC),
    )
    msg_repo.insert(msg)
    for part in msg.content_parts:
        msg_repo.insert_content_part(part, msg.message_id)
    db.commit()
    return asset


@pytest.fixture
def sticker_service(
    db: sqlite3.Connection,
    repo: MultimodalRepository,
    payload_store: PayloadStore,
    config: MultimodalConfig,
    asset_service: AssetIngestionService,
) -> SqliteStickerService:
    return SqliteStickerService(
        db,
        multimodal_repo=repo,
        payload_store=payload_store,
        config=config,
        asset_service=asset_service,
        metrics=MultimodalMetrics(),
    )


# ── save_sticker ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_sticker_marks_asset(sticker_service, repo, payload_store, db):
    _seed_conversation(db)
    _seed_session(db, "conv1")
    asset = _seed_image(repo, payload_store, db)
    tool = create_save_sticker_def(make_service=lambda: sticker_service)
    result = await tool.handler(
        {"asset_id": asset.asset_id, "name": "happy cat", "tags": ["cat", "funny"]},
        _ctx("owner", "s1"),
    )
    import json
    data = json.loads(result)
    assert data["status"] == "saved"
    assert data["sticker_id"] == asset.asset_id
    sticker = repo.get_sticker(asset.asset_id)
    assert sticker is not None
    assert sticker.is_sticker is True
    assert sticker.sticker_name == "happy cat"
    assert "cat" in sticker.tags


@pytest.mark.asyncio
async def test_save_sticker_denies_cross_principal(sticker_service, repo, payload_store, db):
    _seed_conversation(db)
    _seed_session(db, "conv1")
    asset = _seed_image(repo, payload_store, db, principal="owner")
    tool = create_save_sticker_def(make_service=lambda: sticker_service)
    result = await tool.handler(
        {"asset_id": asset.asset_id, "name": "x"},
        _ctx("intruder", "other-session"),
    )
    import json
    data = json.loads(result)
    assert data["status"] == "denied"


@pytest.mark.asyncio
async def test_save_sticker_records_metrics(sticker_service, repo, payload_store, db):
    _seed_conversation(db)
    _seed_session(db, "conv1")
    metrics = MultimodalMetrics()
    sticker_service._metrics = metrics
    asset = _seed_image(repo, payload_store, db)
    sticker_service.save_sticker(
        asset.asset_id, name="cat", principal_id="owner", session_id="s1",
    )
    assert metrics.snapshot()["stickers_saved"] == 1


# ── list / search ───────────────────────────────────────────────────────


def test_list_stickers_filters_by_tag(sticker_service, repo, payload_store, db):
    _seed_conversation(db)
    _seed_session(db, "conv1")
    a1 = _seed_image(repo, payload_store, db, idx=1)
    a2 = _seed_image(repo, payload_store, db, idx=2)
    repo.mark_as_sticker(a1.asset_id, name="cat sticker", tags=("cat",))
    repo.mark_as_sticker(a2.asset_id, name="dog sticker", tags=("dog",))
    cats = sticker_service.list_stickers(principal_id="owner", tag="cat")
    assert len(cats) == 1
    assert cats[0]["sticker_name"] == "cat sticker"


def test_record_usage_increments(sticker_service, repo, payload_store, db):
    _seed_conversation(db)
    _seed_session(db, "conv1")
    asset = _seed_image(repo, payload_store, db)
    repo.mark_as_sticker(asset.asset_id, name="cat")
    repo.record_sticker_usage(asset.asset_id)
    repo.record_sticker_usage(asset.asset_id)
    g = repo.get_sticker(asset.asset_id)
    assert g is not None and g.usage_count == 2


# ── send_sticker requires delivery service ───────────────────────────────


@pytest.mark.asyncio
async def test_send_sticker_without_delivery(sticker_service, repo, payload_store, db):
    _seed_conversation(db)
    _seed_session(db, "conv1")
    asset = _seed_image(repo, payload_store, db)
    repo.mark_as_sticker(asset.asset_id, name="cat")
    tool = create_send_sticker_def(make_service=lambda: sticker_service)
    result = await tool.handler(
        {"sticker_id": asset.asset_id},
        _ctx("owner", "s1"),
    )
    import json
    data = json.loads(result)
    assert data["status"] == "error"
    assert "delivery" in data["error"]


# ── SSRF safe fetch ─────────────────────────────────────────────────────


def test_fetch_blocks_private_addresses():
    for url in [
        "http://127.0.0.1/x.png",
        "http://192.168.1.1/x",
        "http://10.0.0.1/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest",
        "http://localhost/x",
        "ftp://evil.com/x",
        "file:///etc/passwd",
    ]:
        with pytest.raises(SafeHttpError):
            fetch_url_bytes(url, timeout_s=2)


def test_fetch_blocks_even_when_allowlisted_private():
    with pytest.raises(SafeHttpError):
        fetch_url_bytes(
            "http://127.0.0.1/x.png",
            allowed_hosts=("127.0.0.1",),
            timeout_s=2,
        )


# ── gateway reads image content ──────────────────────────────────────────


def test_gateway_reads_image_attachment(db, tmp_path: Path):
    from cogito.service.channel_gateway import ChannelGateway

    repo = MultimodalRepository(db)
    payload_store = PayloadStore(str(tmp_path / "payload"), db)
    _seed_conversation(db)
    _seed_session(db, "conv1")
    asset = _seed_message_with_image(db, repo, payload_store)

    # _read_message_content only uses the connection, not the manager.
    gw = ChannelGateway(db, None)  # type: ignore[arg-type]
    content = gw._read_message_content("m1")
    assert content.text == "hello"
    assert len(content.attachments) == 1
    assert content.attachments[0].payload_ref == asset.payload_ref
    assert content.attachments[0].mime == "image/png"
    assert content.attachments[0].name == "cat.png"


def test_gateway_text_only_message(db, tmp_path: Path):
    """Plain text message -> no attachments, no crash."""
    from cogito.service.channel_gateway import ChannelGateway

    _seed_conversation(db)
    _seed_session(db, "conv1")
    msg_repo = MessageRepository(db)
    msg = Message(
        message_id="m2", conversation_id="conv1", session_id="s1",
        sender_principal_id="owner", role=MessageRole.user,
        direction=MessageDirection.inbound,
        content_parts=[ContentPart(content_type="text", inline_data="only text", ordinal=0)],
        receive_sequence=2, created_at=datetime.now(UTC),
    )
    msg_repo.insert(msg)
    msg_repo.insert_content_part(msg.content_parts[0], msg.message_id)
    db.commit()

    gw = ChannelGateway(db, None)  # type: ignore[arg-type]
    content = gw._read_message_content("m2")
    assert content.text == "only text"
    assert content.attachments == ()
