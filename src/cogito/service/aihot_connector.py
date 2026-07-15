"""AIHOT MCP Connector —— 将 AIHOT 公开 API 封装为主动推送数据源。

把 aihot_items (AI 动态列表) 注册为一个 polling 型 MCP Connector，
由 Scheduler 定期触发 mcp_connector.poll Task，经决策流水线筛选后
进入主动推送。

服从事实来源单一职责（PROACTIVE-TASKS / 1）：
- 本模块只负责"把外部数据带入系统"（Connector 摄取），
- 推送决策由 Proactive Decision Pipeline 完成。

字段映射（aihot_items 响应 → ConnectorItem）：
- stable_id  : items[].id
- title      : items[].title
- body       : items[].summary
- url        : items[].url
- topic      : items[].category
- updated_at : items[].createdAt / updatedAt
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from cogito.domain.connector import Connector, ConnectorStatus, ConnectorType
from cogito.domain.mcp_connector import MCPConnectorConfig
from cogito.domain.schedule import MisfirePolicy, Schedule, ScheduleType
from cogito.store.connector_repo import ConnectorRepository
from cogito.store.mcp_connector_repo import MCPConnectorConfigRepository
from cogito.store.schedule_repo import ScheduleRepository

_LOGGER = logging.getLogger(__name__)

# 幂等键：保证多次启动不会重复注册
AIHOT_CONNECTOR_ID = "connector-aihot-items"
AIHOT_SCHEDULE_ID = "schedule-aihot-items"

# aihot_items 默认参数：精选视图，每次拉 20 条
AIHOT_ARGUMENTS_TEMPLATE = {"scope": "selected", "limit": 20}


def seed_aihot_connector(conn) -> str | None:
    """注册 AIHOT MCP Connector（幂等）。

    创建三样东西：
    1. Connector (type=mcp) —— 摄取源标识
    2. MCPConnectorConfig —— aihot 服务器 + aihot_items 工具字段映射
    3. Schedule (interval=1h) —— 轮询节奏

    Args:
        conn: SQLite 连接。

    Returns:
        connector_id 如果新建/已存在；None 如果 AIHOT MCP 未在配置中启用。
    """
    # 检查是否已注册（幂等）
    existing = ConnectorRepository(conn).get(AIHOT_CONNECTOR_ID)
    if existing is not None:
        _LOGGER.debug("AIHOT connector already registered: %s", AIHOT_CONNECTOR_ID)
        return AIHOT_CONNECTOR_ID

    now = datetime.now(UTC)

    # 注意插入顺序：connectors.poll_schedule_id 外键引用 schedules(schedule_id)，
    # 必须先插入 Schedule，再插入 Connector，否则 FK 约束失败。

    # 1. Schedule：每 1 小时轮询一次（先建，满足 FK 引用）
    from cogito.domain.schedule import next_fire_at

    first_fire = next_fire_at("1h", timezone="Asia/Shanghai", after=now)
    schedule = Schedule(
        schedule_id=AIHOT_SCHEDULE_ID,
        schedule_type=ScheduleType.interval,
        expression="1h",
        timezone="Asia/Shanghai",
        misfire_policy=MisfirePolicy.catch_up_limited,
        max_catch_up=2,
        enabled=True,
        next_fire_at=first_fire,
        connector_id=AIHOT_CONNECTOR_ID,
        created_at=now,
    )
    ScheduleRepository(conn).insert(schedule)

    # 2. Connector（引用上面创建的 schedule）
    connector = Connector(
        connector_id=AIHOT_CONNECTOR_ID,
        connector_type=ConnectorType.mcp,
        name="AIHOT",
        url="https://aihot.virxact.com/api/public",
        site_link="https://aihot.virxact.com",
        poll_schedule_id=AIHOT_SCHEDULE_ID,
        fetch_timeout_s=30,
        status=ConnectorStatus.active,
        created_at=now,
    )
    ConnectorRepository(conn).insert(connector)

    # 3. MCP 映射：aihot_items 响应字段 → ConnectorItem
    mapping = MCPConnectorConfig(
        connector_id=AIHOT_CONNECTOR_ID,
        server_name="aihot",
        tool_name="aihot_items",
        arguments_template=AIHOT_ARGUMENTS_TEMPLATE,
        items_path="items",
        next_cursor_path="nextCursor",
        has_more_path="hasNext",
        stable_id_path="id",
        updated_at_path="createdAt",
        title_path="title",
        body_path="summary",
        url_path="url",
        topic_path="category",
        max_pages_per_poll=3,
        max_items_per_poll=100,
        max_output_bytes=512 * 1024,
        config_version=1,
    )
    MCPConnectorConfigRepository(conn).save(mapping)

    conn.commit()
    _LOGGER.info(
        "AIHOT connector registered: %s (schedule=%s, first_fire=%s)",
        AIHOT_CONNECTOR_ID,
        AIHOT_SCHEDULE_ID,
        first_fire.isoformat() if first_fire else None,
    )
    return AIHOT_CONNECTOR_ID


def disable_aihot_connector(conn) -> None:
    """停用 AIHOT Connector（保留数据，仅停调度）。"""
    ConnectorRepository(conn).update_status(
        AIHOT_CONNECTOR_ID,
        ConnectorStatus.disabled,
    )
    ScheduleRepository(conn).update_enabled(AIHOT_SCHEDULE_ID, False)
    conn.commit()
    _LOGGER.info("AIHOT connector disabled: %s", AIHOT_CONNECTOR_ID)
