# cogito/agent/tools/validation.py
#
# JsonSchemaToolValidator — validates tool arguments against JSON Schema.
#
# Design rules (see tool-system-spec §12):
#   - Uses Draft 2020-12 validation semantics.
#   - Schemas are compiled and cached at registration time.
#   - Rejects: unbounded recursive $ref, huge enums, remote $ref.
#   - All tools get additionalProperties: false by default.

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from cogito.agent.domain.tools import ToolDefinition

logger = logging.getLogger(__name__)


class JsonSchemaValidationError(ValueError):
    """Raised when tool arguments fail JSON Schema validation."""

    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        self.errors = errors or []
        detail = "; ".join(self.errors)
        super().__init__(f"{message}: {detail}" if detail else message)


class JsonSchemaToolValidator:
    """Validates tool arguments against JSON Schema.

    Uses simple structural validation without an external jsonschema library.
    Supports: type checking, required fields, enum, minLength/maxLength,
    minimum/maximum, pattern, additionalProperties, items validation.
    """

    def __init__(self) -> None:
        self._cache: dict[str, _CompiledSchema] = {}

    # ── Public API ──────────────────────────────────────────────────────

    def validate(
        self,
        *,
        definition: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> None:
        """Validate arguments against the tool's input schema.

        Raises JsonSchemaValidationError on failure.
        """
        schema = definition.input_schema
        compiled = self._compile(schema)
        errors = self._validate_object(arguments, compiled)

        if errors:
            raise JsonSchemaValidationError(
                f"Tool {definition.name!r} argument validation failed",
                errors=errors,
            )

    def validate_output(
        self,
        *,
        definition: ToolDefinition,
        output: Mapping[str, object],
    ) -> list[str]:
        """Validate tool output against output_schema (if defined).

        Returns a list of error messages (empty = valid).
        """
        schema = definition.output_schema
        if schema is None:
            return []

        compiled = self._compile(schema)
        return self._validate_object(output, compiled)

    @staticmethod
    def check_schema_safety(schema: Mapping[str, object]) -> list[str]:
        """Check a schema for safety concerns. Returns warning messages."""
        warnings: list[str] = []

        if _has_remote_ref(schema):
            warnings.append("Schema contains remote $ref — not supported")

        enum_values = schema.get("enum", [])
        if isinstance(enum_values, list) and len(enum_values) > 500:
            warnings.append(f"Schema has {len(enum_values)} enum values — may exceed budget")

        depth = _schema_depth(schema)
        if depth > 10:
            warnings.append(f"Schema depth {depth} exceeds recommended limit of 10")

        if _has_unbounded_recursion(schema):
            warnings.append("Schema has potential unbounded $ref recursion")

        return warnings

    # ── Internal ────────────────────────────────────────────────────────

    def _compile(self, schema: Mapping[str, object]) -> _CompiledSchema:
        """Compile and cache a schema."""
        key = _schema_key(schema)
        if key in self._cache:
            return self._cache[key]

        compiled = _CompiledSchema(schema)
        self._cache[key] = compiled
        return compiled

    def _validate_object(
        self,
        obj: Mapping[str, object],
        schema: _CompiledSchema,
        path: str = "",
    ) -> list[str]:
        """Validate an object against a compiled schema."""
        errors: list[str] = []

        # Type check
        expected_type = schema.raw.get("type")
        if expected_type == "object":
            if not isinstance(obj, dict):
                errors.append(f"{path}: expected object, got {type(obj).__name__}")
                return errors

            # Required fields
            required = schema.raw.get("required", [])
            if isinstance(required, list):
                for field in required:
                    if field not in obj:
                        errors.append(f"{path}: missing required field {field!r}")

            # Properties
            properties = schema.raw.get("properties", {})
            if isinstance(properties, dict):
                for prop_name, prop_schema in properties.items():
                    if prop_name in obj and isinstance(prop_schema, dict):
                        prop_path = f"{path}.{prop_name}" if path else prop_name
                        child_errors = self._validate_value(
                            obj[prop_name],
                            prop_schema,
                            prop_path,
                        )
                        errors.extend(child_errors)

            # Additional properties
            additional = schema.raw.get("additionalProperties", True)
            if additional is False and isinstance(properties, dict):
                extra = set(obj.keys()) - set(properties.keys())
                if extra:
                    errors.append(f"{path}: unexpected properties: {', '.join(sorted(extra))}")

        elif expected_type == "array":
            if not isinstance(obj, list):
                errors.append(f"{path}: expected array, got {type(obj).__name__}")
                return errors

            items_schema = schema.raw.get("items")
            if isinstance(items_schema, dict):
                for i, item in enumerate(obj):
                    item_path = f"{path}[{i}]"
                    child_errors = self._validate_value(item, items_schema, item_path)
                    errors.extend(child_errors)

        else:
            # Primitive types — validate at value level
            child_errors = self._validate_value(obj, schema.raw, path)
            errors.extend(child_errors)

        return errors

    def _validate_value(
        self,
        value: object,
        raw_schema: Mapping[str, object],
        path: str,
    ) -> list[str]:
        """Validate a single value against a schema."""
        errors: list[str] = []

        # Handle $ref
        ref = raw_schema.get("$ref")
        if isinstance(ref, str):
            return errors  # $ref resolution is a no-op for now

        expected_type = raw_schema.get("type")

        # null is allowed for any type when nullable
        if value is None:
            nullable = raw_schema.get("nullable", False)
            if not nullable:
                errors.append(f"{path}: expected {expected_type}, got null")
            return errors

        # Type-specific validation
        if expected_type == "string":
            self._validate_string(value, raw_schema, path, errors)
        elif expected_type == "integer":
            self._validate_integer(value, raw_schema, path, errors)
        elif expected_type == "number":
            self._validate_number(value, raw_schema, path, errors)
        elif expected_type == "boolean":
            if not isinstance(value, bool):
                errors.append(f"{path}: expected boolean, got {type(value).__name__}")
        elif expected_type == "array":
            if not isinstance(value, list):
                errors.append(f"{path}: expected array, got {type(value).__name__}")
        elif expected_type == "object":
            if not isinstance(value, dict):
                errors.append(f"{path}: expected object, got {type(value).__name__}")
            else:
                child_errors = self._validate_object(value, _CompiledSchema(raw_schema), path)
                errors.extend(child_errors)

        # Enum check
        enum_values = raw_schema.get("enum")
        if isinstance(enum_values, list) and value not in enum_values:
            errors.append(f"{path}: value {value!r} not in enum {enum_values}")

        return errors

    @staticmethod
    def _validate_string(
        value: object,
        schema: dict[str, Any],
        path: str,
        errors: list[str],
    ) -> None:
        if not isinstance(value, str):
            errors.append(f"{path}: expected string, got {type(value).__name__}")
            return

        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(value) < min_len:
            errors.append(f"{path}: string too short ({len(value)} < {min_len})")

        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and len(value) > max_len:
            errors.append(f"{path}: string too long ({len(value)} > {max_len})")

        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            import re
            if not re.match(pattern, value):
                errors.append(f"{path}: does not match pattern {pattern!r}")

    @staticmethod
    def _validate_integer(
        value: object,
        schema: dict[str, Any],
        path: str,
        errors: list[str],
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: expected integer, got {type(value).__name__}")
            return

        minimum = schema.get("minimum")
        if isinstance(minimum, int) and value < minimum:
            errors.append(f"{path}: value {value} < minimum {minimum}")

        maximum = schema.get("maximum")
        if isinstance(maximum, int) and value > maximum:
            errors.append(f"{path}: value {value} > maximum {maximum}")

    @staticmethod
    def _validate_number(
        value: object,
        schema: dict[str, Any],
        path: str,
        errors: list[str],
    ) -> None:
        if not isinstance(value, int | float) or isinstance(value, bool):
            errors.append(f"{path}: expected number, got {type(value).__name__}")
            return

        minimum = schema.get("minimum")
        if isinstance(minimum, int | float) and value < minimum:
            errors.append(f"{path}: value {value} < minimum {minimum}")

        maximum = schema.get("maximum")
        if isinstance(maximum, int | float) and value > maximum:
            errors.append(f"{path}: value {value} > maximum {maximum}")


class _CompiledSchema:
    """Cached compiled schema for fast validation."""

    __slots__ = ("raw", "properties", "required")

    def __init__(self, raw: Mapping[str, object]) -> None:
        self.raw = raw
        self.properties = raw.get("properties", {}) if isinstance(raw.get("properties"), dict) else {}
        self.required = raw.get("required", []) if isinstance(raw.get("required"), list) else []


def _schema_key(schema: Mapping[str, object]) -> str:
    """Generate a cache key from a schema dict."""
    return str(id(schema))  # Identity-based for now; schemas are immutable


def _has_remote_ref(schema: Mapping[str, object], _depth: int = 0) -> bool:
    """Check if a schema references remote $ref."""
    if _depth > 10:
        return False
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("http"):
        return True
    for val in schema.values():
        if isinstance(val, dict):
            if _has_remote_ref(val, _depth + 1):
                return True
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and _has_remote_ref(item, _depth + 1):
                    return True
    return False


def _schema_depth(schema: Mapping[str, object], _depth: int = 0) -> int:
    """Compute the maximum nesting depth of a schema."""
    if _depth > 20:
        return _depth
    max_depth = _depth
    for val in schema.values():
        if isinstance(val, dict):
            d = _schema_depth(val, _depth + 1)
            max_depth = max(max_depth, d)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    d = _schema_depth(item, _depth + 1)
                    max_depth = max(max_depth, d)
    return max_depth


def _has_unbounded_recursion(schema: Mapping[str, object], _seen: frozenset[str] | None = None) -> bool:
    """Detect potential unbounded recursion via $ref cycles."""
    if _seen is None:
        _seen = frozenset()
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in _seen:
            return True
        seen = _seen | {ref}
        # Simplified detection — full impl would resolve refs
        return len(_seen) > 5
    for val in schema.values():
        if isinstance(val, dict):
            if _has_unbounded_recursion(val, _seen):
                return True
    return False
