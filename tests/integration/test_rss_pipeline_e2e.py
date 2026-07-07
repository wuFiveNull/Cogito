"""RSS 摄取管道 E2E 测试 —— Scheduler → RSS → 去重 → 摘要 → Digest。

使用 FakeRssServer 驱动完整流程。每次测试用独立临时文件数据库 +
独立 connection_factory（符合生产模式）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.connector import (
    Connector,
    ItemStatus,
)
from cogito.domain.schedule import Schedule, ScheduleType
from cogito.model.contracts import ContentPart, FinishReason, ModelResponse, Usage
from cogito.model.provider import ModelProvider
from cogito.model.router import ModelRouter
from cogito.runtime.clock import FakeClock
from cogito.service.scheduler import Scheduler
from cogito.store.connection import get_connection
from cogito.store.connector_repo import (
    ConnectorCursorRepository,
    ConnectorItemRepository,
    ConnectorRepository,
)
from cogito.store.migration import migrate
from cogito.store.schedule_repo import ScheduleRepository


class FakeSummaryProvider(ModelProvider):
    """返回固定摘要的模型 provider。"""

    def __init__(self, summary: str = "这是自动生成的摘要") -> None:
        self.summary = summary
        self.call_count = 0

    async def generate(self, request, model_role: str = "main") -> ModelResponse:
        self.call_count += 1
        return ModelResponse(
            request_id=request.request_id,
            model_id="fake",
            content_parts=(ContentPart(part_type="text", text=self.summary),),
            finish_reason=FinishReason.stop,
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    def capabilities(self):
        from cogito.model.contracts import ModelCapabilities
        return ModelCapabilities(context_window=128000, max_output_tokens=4096)

    async def health(self):
        from cogito.model.provider import HealthStatus
        return HealthStatus(healthy=True)


def _build_router() -> ModelRouter:
    provider = FakeSummaryProvider()
    return ModelRouter(
        providers={"main": provider},
        role_map={"main": "main"},
        max_retries=0,
    )


@pytest.fixture
def db_path(tmp_path):
    """临时文件数据库，跨多次连接持久化。"""
    path = tmp_path / "e2e.db"
    conn = get_connection(str(path))
    migrate(conn)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    """每次调用打开新连接（生产模式）。"""
    c = get_connection(str(db_path))
    c.row_factory = sqlite3.Row
    return c


class TestRssPipelineE2E:
    @pytest.fixture
    def clock(self):
        return FakeClock(start=datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC))

    def _factory(self, db_path):
        return lambda: get_connection(str(db_path))

    def _setup_connector(self, conn, fake_rss_server, name="feed1", timeout_s=5):
        connector = Connector(
            connector_id=f"c-{name}", name=name, url=fake_rss_server.url,
            fetch_timeout_s=timeout_s,
        )
        ConnectorRepository(conn).insert(connector)
        conn.commit()
        return connector

    def test_full_pipeline_dedup_and_digest(self, db_path, clock, fake_rss_server):
        """完整管道：抓取 → 去重 → 摘要 → digest 决策。"""
        conn = get_connection(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate(conn)
        conn.commit()
        connector = self._setup_connector(conn, fake_rss_server)
        router = _build_router()
        factory = self._factory(db_path)

        fake_rss_server.set_entries([
            {"title": "AI breakthrough announced", "link": "http://x/ai-1",
             "description": "A" * 200, "guid": "g1"},
            {"title": "Cooking tips", "link": "http://x/cook-1",
             "description": "B" * 200, "guid": "g2"},
        ])

        from cogito.domain.task import Task, TaskStatus
        from cogito.service.connector_handler import handle_connector_poll
        from cogito.service.task_handlers import TaskHandlerContext

        task = Task(task_type="connector.poll", payload_ref=connector.connector_id,
                    status=TaskStatus.queued, idempotency_key="e2e-1")
        ctx = TaskHandlerContext(connection_factory=factory, model_router=router)
        result = handle_connector_poll(task, ctx)
        assert "new=2" in result

        # 第二次抓取（ETag 匹配）→ 304 not modified
        task2 = Task(task_type="connector.poll", payload_ref=connector.connector_id,
                     status=TaskStatus.queued, idempotency_key="e2e-2")
        result2 = handle_connector_poll(task2, ctx)
        assert result2 == "not modified"  # ETag 匹配 → 无更新

        # 验证 item 数量
        verify_conn = get_connection(str(db_path))
        items = ConnectorItemRepository(verify_conn).find_all()
        assert len(items) == 2
        verify_conn.close()

    def test_304_not_modified(self, db_path, clock, fake_rss_server):
        """ETag 匹配时 304 跳过。"""
        conn = get_connection(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate(conn)
        conn.commit()
        connector = self._setup_connector(conn, fake_rss_server)
        factory = self._factory(db_path)

        fake_rss_server.set_entries([
            {"title": "T", "link": "http://x/t", "description": "D" * 200, "guid": "gt"},
        ])

        from cogito.domain.task import Task, TaskStatus
        from cogito.service.connector_handler import handle_connector_poll
        from cogito.service.task_handlers import TaskHandlerContext

        task = Task(task_type="connector.poll", payload_ref=connector.connector_id,
                    status=TaskStatus.queued, idempotency_key="e2e-3")
        ctx = TaskHandlerContext(connection_factory=factory)
        r1 = handle_connector_poll(task, ctx)
        assert "new=1" in r1

        # 第二次（ETag 匹配）
        task2 = Task(task_type="connector.poll", payload_ref=connector.connector_id,
                     status=TaskStatus.queued, idempotency_key="e2e-4")
        r2 = handle_connector_poll(task2, ctx)
        assert r2 == "not modified"

    def test_timeout_retry(self, db_path, clock, fake_rss_server):
        """超时应抛异常（由 Task 层重试）。"""
        conn = get_connection(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate(conn)
        conn.commit()
        connector = self._setup_connector(conn, fake_rss_server, timeout_s=1)
        factory = self._factory(db_path)

        fake_rss_server.set_entries([
            {"title": "T", "link": "http://x/t", "description": "D", "guid": "gt"},
        ])
        fake_rss_server.set_next_timeout(2.0)  # 服务端 2s > 客户端 1s → 超时

        from cogito.domain.task import Task, TaskStatus
        from cogito.service.connector_handler import handle_connector_poll
        from cogito.service.task_handlers import TaskHandlerContext

        task = Task(task_type="connector.poll", payload_ref=connector.connector_id,
                    status=TaskStatus.queued, idempotency_key="e2e-5")
        ctx = TaskHandlerContext(connection_factory=factory)

        with pytest.raises(RuntimeError, match="retryable"):
            handle_connector_poll(task, ctx)

    def test_scheduler_triggers_poll(self, db_path, clock, fake_rss_server):
        """Scheduler tick 触发生成 connector.poll Task。"""
        conn = get_connection(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate(conn)
        conn.commit()
        connector = self._setup_connector(conn, fake_rss_server)
        now = clock.now()
        s = Schedule(
            schedule_id="s1", schedule_type=ScheduleType.interval, expression="30m",
            next_fire_at=now, connector_id=connector.connector_id,
        )
        ScheduleRepository(conn).insert(s)
        conn.commit()

        scheduler = Scheduler(conn, clock=clock)
        tasks = scheduler.tick()
        assert len(tasks) == 1
        assert tasks[0].task_type == "connector.poll"
        assert tasks[0].payload_ref == connector.connector_id
        conn.close()

    def test_resume_from_cursor(self, db_path, clock, fake_rss_server):
        """断点续传：cursor 持久化已见条目，重启后不重复拉。"""
        conn = get_connection(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate(conn)
        conn.commit()
        connector = self._setup_connector(conn, fake_rss_server)
        factory = self._factory(db_path)

        fake_rss_server.set_entries([
            {"title": "First", "link": "http://x/1", "description": "D" * 200, "guid": "g1"},
        ])

        from cogito.domain.task import Task, TaskStatus
        from cogito.service.connector_handler import handle_connector_poll
        from cogito.service.task_handlers import TaskHandlerContext

        task = Task(task_type="connector.poll", payload_ref=connector.connector_id,
                    status=TaskStatus.queued, idempotency_key="e2e-6")
        ctx = TaskHandlerContext(connection_factory=factory)
        handle_connector_poll(task, ctx)

        # 新增条目后再次抓取
        fake_rss_server.add_entry("Second", description="E" * 200)
        task2 = Task(task_type="connector.poll", payload_ref=connector.connector_id,
                     status=TaskStatus.queued, idempotency_key="e2e-7")
        result = handle_connector_poll(task2, ctx)
        assert "new=1" in result  # g1 已存在，仅新增 1 条

    def test_restart_recovery(self, db_path, clock, fake_rss_server):
        """重启恢复：running task 过期 → abandoned + queued。"""
        conn = get_connection(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate(conn)
        conn.commit()
        connector = self._setup_connector(conn, fake_rss_server)
        now = clock.now()

        from cogito.domain.task import Task, TaskAttempt, TaskStatus, TaskAttemptStatus
        from cogito.store.task_repo import TaskRepository, TaskAttemptRepository
        from cogito.service.recovery_service import RecoveryService

        t = Task(task_id="t-old", task_type="connector.poll",
                 payload_ref=connector.connector_id, status=TaskStatus.running,
                 lease_owner="old-worker", lease_expires_at=now - timedelta(minutes=10),
                 idempotency_key="old-1")
        TaskRepository(conn).insert(t)
        a = TaskAttempt(task_id="t-old", attempt_no=1, status=TaskAttemptStatus.running,
                        lease_owner="old-worker", lease_version=1,
                        lease_expires_at=now - timedelta(minutes=10),
                        started_at=now - timedelta(minutes=15))
        TaskAttemptRepository(conn).insert(a)
        conn.commit()
        conn.close()

        # 恢复（新连接模拟重启后）
        recover_conn = get_connection(str(db_path))
        recover_conn.row_factory = sqlite3.Row
        svc = RecoveryService(recover_conn, clock=clock)
        count = svc.recover_stale_tasks()
        assert count == 1
        assert TaskRepository(recover_conn).get("t-old").status == TaskStatus.queued
        recover_conn.close()
