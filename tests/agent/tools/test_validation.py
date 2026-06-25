"""Tests for JsonSchemaToolValidator — argument validation against JSON Schema."""

from __future__ import annotations

import pytest

from cogito.agent.domain.tools import (
    ToolDefinition,
    ToolKind,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)
from cogito.agent.tools.validation import JsonSchemaToolValidator, JsonSchemaValidationError


def _make_def(name: str, schema: dict) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Test {name}",
        input_schema=schema,
        side_effect=ToolSideEffect.NONE,
        risk_level=ToolRiskLevel.LOW,
        timeout_seconds=30.0,
        idempotent=True,
        parallel_safe=True,
        kind=ToolKind.READ,
        risk=ToolRisk.READ_ONLY,
        source=ToolSource(type=ToolSourceType.BUILTIN, provider="test"),
    )


class TestJsonSchemaToolValidator:
    def test_validates_required_fields(self) -> None:
        """Missing required fields produce validation errors."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
            "additionalProperties": False,
        })

        with pytest.raises(JsonSchemaValidationError) as exc:
            validator.validate(definition=defn, arguments={"name": "Alice"})

        assert "missing" in str(exc.value).lower()

    def test_validates_additional_properties(self) -> None:
        """Unexpected properties produce errors when additionalProperties is false."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        })

        with pytest.raises(JsonSchemaValidationError) as exc:
            validator.validate(definition=defn, arguments={"name": "Alice", "extra": "value"})

        assert "unexpected" in str(exc.value).lower()

    def test_validates_string_length(self) -> None:
        """minLength and maxLength constraints on strings."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {
                "short_str": {"type": "string", "minLength": 2, "maxLength": 10},
            },
            "additionalProperties": False,
        })

        # Too short
        with pytest.raises(JsonSchemaValidationError):
            validator.validate(definition=defn, arguments={"short_str": "A"})

        # Too long
        with pytest.raises(JsonSchemaValidationError):
            validator.validate(definition=defn, arguments={"short_str": "A" * 11})

        # Just right
        validator.validate(definition=defn, arguments={"short_str": "Hello"})

    def test_validates_integer_range(self) -> None:
        """minimum/maximum constraints on integers."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "additionalProperties": False,
        })

        validator.validate(definition=defn, arguments={"count": 50})

        with pytest.raises(JsonSchemaValidationError):
            validator.validate(definition=defn, arguments={"count": -1})

        with pytest.raises(JsonSchemaValidationError):
            validator.validate(definition=defn, arguments={"count": 101})

    def test_validates_enum_values(self) -> None:
        """Enum constraint validation."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["auto", "manual", "semi"]},
            },
            "additionalProperties": False,
        })

        validator.validate(definition=defn, arguments={"mode": "auto"})

        with pytest.raises(JsonSchemaValidationError):
            validator.validate(definition=defn, arguments={"mode": "unknown"})

    def test_validates_nested_objects(self) -> None:
        """Nested object properties are validated."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["field"],
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        })

        validator.validate(definition=defn, arguments={"filter": {"field": "name", "value": "test"}})

        with pytest.raises(JsonSchemaValidationError):
            validator.validate(definition=defn, arguments={"filter": {"value": "test"}})

    def test_validates_arrays(self) -> None:
        """Array item validation."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "additionalProperties": False,
        })

        validator.validate(definition=defn, arguments={"tags": ["a", "b", "c"]})

    def test_passes_valid_arguments(self) -> None:
        """No errors for valid arguments."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "recursive": {"type": "boolean"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["path"],
            "additionalProperties": False,
        })

        # No exception should be raised
        validator.validate(
            definition=defn,
            arguments={"path": "/test", "recursive": True, "depth": 3},
        )

    def test_check_schema_safety(self) -> None:
        """Schema safety checking produces appropriate warnings."""
        validator = JsonSchemaToolValidator()

        # Deeply nested schema
        deep_schema = {"type": "object", "properties": {}}
        current = deep_schema
        for i in range(15):
            current["properties"]["nested"] = {"type": "object", "properties": {}}
            current = current["properties"]["nested"]

        warnings = validator.check_schema_safety(deep_schema)
        assert any("depth" in w for w in warnings)

    def test_nullable_handling(self) -> None:
        """Nullable fields accept None values."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {
                "name": {"type": "string", "nullable": True},
                "count": {"type": "integer", "nullable": True},
            },
            "additionalProperties": False,
        })

        validator.validate(definition=defn, arguments={"name": None})
        validator.validate(definition=defn, arguments={"name": "test", "count": None})

    def test_empty_schema_accepts_any(self) -> None:
        """Empty properties accept arguments."""
        validator = JsonSchemaToolValidator()
        defn = _make_def("test_tool", {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        })

        validator.validate(definition=defn, arguments={"anything": "goes", "numbers": 42})
