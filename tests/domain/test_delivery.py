"""Tests for Delivery and DeliveryAttempt domain entities."""

from cogito.domain.delivery import Delivery, DeliveryStatus, DeliveryAttempt, DeliveryAttemptStatus


class TestDelivery:
    def test_create_default(self):
        d = Delivery()
        assert d.delivery_id is not None
        assert d.status == DeliveryStatus.pending

    def test_create_with_values(self):
        d = Delivery(
            delivery_id="d1",
            content_ref="msg://m1",
            status=DeliveryStatus.sending,
            idempotency_key="ik1",
        )
        assert d.content_ref == "msg://m1"
        assert d.idempotency_key == "ik1"

    def test_to_dict_roundtrip(self):
        d1 = Delivery(delivery_id="d1", idempotency_key="ik1", status=DeliveryStatus.sent)
        d = d1.to_dict()
        d2 = Delivery.from_dict(d)
        assert d1 == d2
        assert d2.status == DeliveryStatus.sent


class TestDeliveryAttempt:
    def test_create_default(self):
        da = DeliveryAttempt()
        assert da.status == DeliveryAttemptStatus.created

    def test_create_with_values(self):
        da = DeliveryAttempt(
            attempt_id="da1", delivery_id="d1", attempt_no=2,
            status=DeliveryAttemptStatus.succeeded,
        )
        assert da.status == DeliveryAttemptStatus.succeeded

    def test_to_dict_roundtrip(self):
        da1 = DeliveryAttempt(attempt_id="da1", delivery_id="d1")
        d = da1.to_dict()
        da2 = DeliveryAttempt.from_dict(d)
        assert da2.delivery_id == "d1"
