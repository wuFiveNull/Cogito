"""Execute a canonical Delivery request from its protected Event payload."""

from __future__ import annotations

import json

from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.delivery_effect_payload import load_delivery_effect_payload
from cogito.service.event_effect_recovery import PendingEffect
from cogito.service.event_effect_worker import EffectOutcome
from cogito.service.gateway_client import PERMANENT_GATEWAY_STATUSES, GatewayClient


class CanonicalDeliveryEffectExecutor:
    """External-delivery adapter with no dependency on Delivery state tables.

    The request Event references one protected payload containing the immutable
    target, resolved body, and provider idempotency key.  Therefore a retry
    after process loss can perform the same operation directly from the Event
    stream.  Legacy v1 payloads intentionally remain ``unknown``: resolving
    their Core ``content_ref`` here would reintroduce a business-table read.
    """

    def __init__(self, payload_store: PayloadStore, gateway: GatewayClient) -> None:
        self._payload_store = payload_store
        self._gateway = gateway

    def execute(self, effect: PendingEffect) -> EffectOutcome:
        if effect.effect_type != "delivery":
            raise ValueError(f"unsupported effect type: {effect.effect_type}")
        if not effect.payload_ref:
            return EffectOutcome("unknown", "missing_effect_payload")
        if effect.payload_hash and effect.payload_hash != effect.payload_ref:
            return EffectOutcome("unknown", "effect_payload_hash_mismatch")
        try:
            payload = load_delivery_effect_payload(self._payload_store, effect.payload_ref)
        except (LookupError, ValueError):
            return EffectOutcome("unknown", "invalid_effect_payload")
        if payload.content is None:
            return EffectOutcome("unknown", "legacy_effect_payload_requires_migration")

        target = json.dumps(
            payload.target_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            result = self._gateway.send(target, payload.content, payload.idempotency_key)
        except Exception as exc:
            return EffectOutcome("unknown", f"gateway_exception:{type(exc).__name__}")

        if result.status in {"success", "sent"}:
            return EffectOutcome(
                "completed",
                attributes=(
                    {"platform_message_id": result.platform_message_id}
                    if result.platform_message_id
                    else None
                ),
            )
        error = result.error_code or result.status or "gateway_unknown"
        if result.status in PERMANENT_GATEWAY_STATUSES:
            return EffectOutcome("failed", error)
        if result.status in {"temporary", "rate_limited"}:
            return EffectOutcome(
                "retry_scheduled",
                error,
                attributes=(
                    {"retry_after_seconds": result.retry_after_seconds}
                    if result.retry_after_seconds is not None
                    else None
                ),
            )
        return EffectOutcome("unknown", error)


__all__ = ["CanonicalDeliveryEffectExecutor"]
