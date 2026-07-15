from __future__ import annotations

from pathlib import Path

import pytest

from cogito.config import Config
from cogito.capability_diagnostics import (
    CapabilityDiagnosticSession,
    doctor_checks,
    tool_record,
)
from cogito.store.connection import get_connection
from cogito.store.migration import migrate


def _config(tmp_path: Path) -> Config:
    config = Config()
    config.workspace_path = str(tmp_path / ".workspace")
    config.capability.workspace.root = str(tmp_path)
    config.capability.skills.root = str(tmp_path / ".cogito" / "skills")
    Path(config.capability.skills.root).mkdir(parents=True)
    connection = get_connection(config.resolve_db_path())
    migrate(connection)
    connection.close()
    return config


@pytest.mark.asyncio
async def test_inventory_uses_configured_workspace_skills_and_delegation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = await CapabilityDiagnosticSession.open(config, live_mcp=False)
    try:
        names = {tool.name for tool in session.tools()}
        assert {"read_file", "apply_patch", "skills_list", "delegate_task"} <= names
        read_file = tool_record(session.registry.resolve("read_file"))
        assert read_file["deferred"] is True
        assert read_file["available"] is True
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_doctor_reports_local_capability_readiness(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = await CapabilityDiagnosticSession.open(config, live_mcp=False)
    try:
        checks = {item["name"]: item for item in doctor_checks(config, session)}
        assert checks["config"]["ok"]
        assert checks["database"]["ok"]
        assert checks["workspace_tools"]["ok"]
        assert checks["skill_tools"]["ok"]
        assert checks["tools"]["ok"]
    finally:
        await session.close()
