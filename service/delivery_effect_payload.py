"""Protected payload contract for canonical Delivery effect requests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from cogito.infrastructure.payload_store import PayloadStore

_SCHEMA = "cogito.delivery-effect.v1"


@dataclass(frozen=True, slots=True)
class DeliveryEffectPayload:
    delivery_id: str
    target_snapshot: dict[str, Any]
    content_ref: str
    idempotency_key: str
    scheduled_at: str | None = None


def store_delivery_effect_payload(
    store: PayloadStore,
    payload: DeliveryEffectPayload,
) -> tuple[str, str]:
    """Persist target routing data outside Event log and return ref/hash."""
    raw = json.dumps(
        {
            "schema": _SCHEMA,
            "delivery_id": payload.delivery_id,
            "target_snapshot": payload.target_snapshot,
            "content_ref": payload.content_ref,
            "idempotency_key": payload.idempotency_key,
            "scheduled_at": payload.scheduled_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    stored = store.put(
        raw,
        content_type="application/vnd.cogito.delivery-effect+json",
        retention_class="secret",
    )
    return stored.payload_id, stored.sha256


def load_delivery_effect_payload(store: PayloadStore, payload_ref: str) -> DeliveryEffectPayload:
    """Resolve and validate a protected Delivery request for an effect adapter."""
    raw = store.get(payload_ref)
    if raw is None:
        raise LookupError(f"delivery effect payload missing: {payload_ref}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("delivery effect payload is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        raise ValueError("unsupported delivery effect payload schema")
    target = data.get("target_snapshot")
    if not isinstance(target, dict):
        raise ValueError("delivery effect target_snapshot must be an object")
    required = ("delivery_id", "content_ref", "idempotency_key")
    if any(not isinstance(data.get(field), str) for field in required):
        raise ValueError("delivery effect payload has invalid required fields")
    scheduled_at = data.get("scheduled_at")
    if scheduled_at is not None and not isinstance(scheduled_at, str):
        raise ValueError("delivery effect scheduled_at must be a string or null")
    return DeliveryEffectPayload(
        delivery_id=data["delivery_id"],
        target_snapshot=target,
        content_ref=data["content_ref"],
        idempotency_key=data["idempotency_key"],
        scheduled_at=scheduled_at,
    )
