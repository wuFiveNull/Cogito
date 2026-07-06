"""Tests for Principal and Endpoint domain entities."""

import pytest
from cogito.domain.principal import Principal, PrincipalType, PrincipalStatus, Endpoint, EndpointStatus


class TestPrincipal:
    def test_create_default(self):
        p = Principal()
        assert p.principal_id is not None
        assert p.principal_type == PrincipalType.owner
        assert p.status == PrincipalStatus.active
        assert p.metadata == {}

    def test_create_with_values(self):
        p = Principal(
            principal_id="p1",
            principal_type=PrincipalType.external_user,
            status=PrincipalStatus.blocked,
            metadata={"key": "val"},
        )
        assert p.principal_id == "p1"
        assert p.principal_type == PrincipalType.external_user
        assert p.status == PrincipalStatus.blocked
        assert p.metadata == {"key": "val"}

    def test_to_dict_roundtrip(self):
        p1 = Principal(principal_id="p1", principal_type=PrincipalType.system)
        d = p1.to_dict()
        p2 = Principal.from_dict(d)
        assert p1 == p2
        assert p2.principal_type == PrincipalType.system

    def test_equality(self):
        a = Principal(principal_id="same")
        b = Principal(principal_id="same")
        c = Principal(principal_id="other")
        assert a == b
        assert a != c

    def test_repr(self):
        p = Principal(principal_id="p1")
        assert "Principal(p1" in repr(p)


class TestEndpoint:
    def test_create_default(self):
        e = Endpoint()
        assert e.endpoint_id is not None
        assert e.status == EndpointStatus.active

    def test_create_with_values(self):
        e = Endpoint(
            endpoint_id="ep1",
            channel_type="telegram",
            channel_instance_id="bot1",
            principal_id="p1",
        )
        assert e.channel_type == "telegram"
        assert e.principal_id == "p1"

    def test_to_dict_roundtrip(self):
        e1 = Endpoint(endpoint_id="ep1", channel_type="discord", status=EndpointStatus.disabled)
        d = e1.to_dict()
        e2 = Endpoint.from_dict(d)
        assert e1 == e2
        assert e2.status == EndpointStatus.disabled

    def test_equality(self):
        a = Endpoint(endpoint_id="same")
        b = Endpoint(endpoint_id="same")
        assert a == b
