"""CapabilityRepository —— capabilities 表数据访问（Plan 03 M1）。

持久化 Capability Registry 运行期快照，支持按健康状态/Plugin 过滤。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class CapabilityRecord:
    capability_id: str
    kind: str
    version: str
    owner: str | None = None
    provider: str | None = None
    plugin_id: str | None = None
    toolsets: list[str] = field(default_factory=list)
    supported_modes: list[str] = field(default_factory=list)
    input_schema: str | None = None
    output_schema: str | None = None
    permissions: list[str] = field(default_factory=list)
    risk_level: str = "low"
    side_effect_class: str = "none"
    resource_requirements: dict = field(default_factory=dict)
    health: str = "unknown"
    disabled: bool = False
    deprecated: bool = False
    discovered_at: int = 0
    updated_at: int = 0


class CapabilityRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: CapabilityRecord) -> None:
        self._conn.execute(
            "INSERT INTO capabilities (capability_id, kind, version, owner, provider, plugin_id, "
            "toolsets, supported_modes, input_schema, output_schema, permissions, "
            "risk_level, side_effect_class, resource_requirements, health, disabled, deprecated, "
            "discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.capability_id,
                record.kind,
                record.version,
                record.owner,
                record.provider,
                record.plugin_id,
                json.dumps(record.toolsets),
                json.dumps(record.supported_modes),
                record.input_schema,
                record.output_schema,
                json.dumps(record.permissions),
                record.risk_level,
                record.side_effect_class,
                json.dumps(record.resource_requirements),
                record.health,
                int(record.disabled),
                int(record.deprecated),
                record.discovered_at,
                record.updated_at,
            ),
        )

    def upsert(self, record: CapabilityRecord) -> None:
        """注册或更新能力（按 capability_id 唯一）。"""
        self._conn.execute(
            "INSERT INTO capabilities (capability_id, kind, version, owner, "
            "provider, plugin_id, toolsets, supported_modes, input_schema, "
            "output_schema, permissions, risk_level, side_effect_class, "
            "resource_requirements, health, disabled, deprecated, "
            "discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(capability_id) DO UPDATE SET "
            "kind=excluded.kind, version=excluded.version, "
            "health=excluded.health, disabled=excluded.disabled, "
            "deprecated=excluded.deprecated, updated_at=excluded.updated_at",
            (
                record.capability_id,
                record.kind,
                record.version,
                record.owner,
                record.provider,
                record.plugin_id,
                json.dumps(record.toolsets),
                json.dumps(record.supported_modes),
                record.input_schema,
                record.output_schema,
                json.dumps(record.permissions),
                record.risk_level,
                record.side_effect_class,
                json.dumps(record.resource_requirements),
                record.health,
                int(record.disabled),
                int(record.deprecated),
                record.discovered_at,
                record.updated_at,
            ),
        )

    def get(self, capability_id: str) -> CapabilityRecord | None:
        row = self._conn.execute(
            "SELECT * FROM capabilities WHERE capability_id=?",
            (capability_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def list_healthy(self) -> list[CapabilityRecord]:
        rows = self._conn.execute(
            "SELECT * FROM capabilities "
            "WHERE disabled=0 AND deprecated=0 "
            "AND health IN ('unknown','healthy')",
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_by_plugin(self, plugin_id: str) -> list[CapabilityRecord]:
        rows = self._conn.execute(
            "SELECT * FROM capabilities WHERE plugin_id=?",
            (plugin_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def update_health(self, capability_id: str, health: str) -> None:
        self._conn.execute(
            "UPDATE capabilities SET health=? WHERE capability_id=?",
            (health, capability_id),
        )

    def delete(self, capability_id: str) -> None:
        self._conn.execute(
            "DELETE FROM capabilities WHERE capability_id=?",
            (capability_id,),
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> CapabilityRecord:
        return CapabilityRecord(
            capability_id=row["capability_id"],
            kind=row["kind"],
            version=row["version"],
            owner=row["owner"],
            provider=row["provider"],
            plugin_id=row["plugin_id"],
            toolsets=json.loads(row["toolsets"]) if row["toolsets"] else [],
            supported_modes=json.loads(row["supported_modes"]) if row["supported_modes"] else [],
            input_schema=row["input_schema"],
            output_schema=row["output_schema"],
            permissions=json.loads(row["permissions"]) if row["permissions"] else [],
            risk_level=row["risk_level"],
            side_effect_class=row["side_effect_class"],
            resource_requirements=(
                json.loads(row["resource_requirements"]) if row["resource_requirements"] else {}
            ),
            health=row["health"],
            disabled=bool(row["disabled"]),
            deprecated=bool(row["deprecated"]),
            discovered_at=row["discovered_at"],
            updated_at=row["updated_at"],
        )
