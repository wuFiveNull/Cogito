"""Manual-only MCP source for exercising the proactive delivery pipeline."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from cogito.domain.connector import Connector, ConnectorStatus, ConnectorType
from cogito.domain.mcp_connector import MCPConnectorConfig
from cogito.store.connector_repo import ConnectorRepository
from cogito.store.mcp_connector_repo import MCPConnectorConfigRepository

_LOGGER = logging.getLogger(__name__)

PROACTIVE_MOCK_CONNECTOR_ID = "connector-proactive-mock"
PROACTIVE_MOCK_SERVER_NAME = "proactive_mock"
PROACTIVE_MOCK_TOOL_NAME = "proactive_mock_items"


def seed_proactive_mock_connector(conn) -> str:
    """Register the manual mock source without a polling schedule.

    A mock item has a unique source ID on every explicit trigger.  There is no
    Schedule row, so it can never create background notification traffic.
    """
    if ConnectorRepository(conn).get(PROACTIVE_MOCK_CONNECTOR_ID) is not None:
        return PROACTIVE_MOCK_CONNECTOR_ID

    ConnectorRepository(conn).insert(
        Connector(
            connector_id=PROACTIVE_MOCK_CONNECTOR_ID,
            connector_type=ConnectorType.mcp,
            name="Proactive delivery mock (manual only)",
            url="mcp://proactive-mock",
            fetch_timeout_s=15,
            status=ConnectorStatus.active,
            created_at=datetime.now(UTC),
        )
    )
    MCPConnectorConfigRepository(conn).save(
        MCPConnectorConfig(
            connector_id=PROACTIVE_MOCK_CONNECTOR_ID,
            server_name=PROACTIVE_MOCK_SERVER_NAME,
            tool_name=PROACTIVE_MOCK_TOOL_NAME,
            items_path="items",
            next_cursor_path="nextCursor",
            has_more_path="hasNext",
            stable_id_path="id",
            updated_at_path="createdAt",
            title_path="title",
            body_path="summary",
            url_path="url",
            topic_path="category",
            max_pages_per_poll=1,
            max_items_per_poll=1,
            max_output_bytes=16 * 1024,
            config_version=1,
        )
    )
    conn.commit()
    _LOGGER.info("Manual proactive mock connector registered: %s", PROACTIVE_MOCK_CONNECTOR_ID)
    return PROACTIVE_MOCK_CONNECTOR_ID
