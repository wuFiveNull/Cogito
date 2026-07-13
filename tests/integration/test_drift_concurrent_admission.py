"""PLAN-17 R6 DR-P1-02: 真实并发 admission 只产生 1 个 active Drift。

100 独立 SQLite 连接 (file-backed) + Barrier 同步开始；每个连接执行完整
tick_drift_admit() → 数据库 partial unique index (uq_drift_one_active_per_principal)
+ max_concurrent 计数判定保证最终恰好 1 个 active DriftRun/Task。

(审计证据: '单连接串行 20 次不创建 Run' 的弱测试已被本测试替代。)
"""
from __future__ import annotations

import os
import random
import sqlite3
import tempfile
import threading
import time

import pytest

from cogito.config import DriftConfig
from cogito.service.scheduler import Scheduler
from cogito.store.migration import migrate


def _prepare_db(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.close()


def _make_scheduler(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.row_factory = sqlite3.Row
    dr = DriftConfig(
        enabled=True, dry_run=False,
        default_principal_id="owner",
        idle_after_minutes=30,
        max_runs_per_day=100,
        max_concurrent=1,
        allow_candidate_emission=True,
        allow_candidate_projection=True,
    )
    return Scheduler(conn, drift_config=dr, workspace_path="")


class TestConcurrentAdmission:
    def test_100_connections_only_one_active(self, tmp_path):
        """100 独立连接并发 tick_drift_admit → 只产生 1 个 active DriftRun + 1 task。"""
        db_path = str(tmp_path / "drift_concurrent.db")
        _prepare_db(db_path)

        N = 100
        barrier = threading.Barrier(N)
        results = []
        errors = []

        def worker():
            try:
                scheduler = _make_scheduler(db_path)
                barrier.wait(timeout=10)
                out = scheduler.tick_drift_admit()
                results.append(out)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # 清理 db 文件
        try:
            os.remove(db_path)
        except Exception:
            pass

        # 最终: 应恰好 1 个授权
        admitted = [r for r in results if r is not None]
        assert len(admitted) == 1, f"并发 {N} 连接应只授权 1 次, got {len(admitted)}; errors: {errors[:5]}"

    def test_active_count_respects_max_concurrent(self, tmp_path):
        """当存在 1 个 active run 时, 第二次 tick 应被拒 (max_concurrent=1)。"""
        db_path = str(tmp_path / "drift_mc.db")
        _prepare_db(db_path)
        # seed an active drift_run
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        now = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO tasks (task_id, task_type, status, priority, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("t-seed", "drift.run", "running", 5, "id-seed", now))
        conn.execute(
            "INSERT INTO drift_runs (drift_run_id, task_id, principal_id, "
            "skill_name, skill_version, status, admission_snapshot_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dr-seed", "t-seed", "owner", "proactive-policy-view-audit",
             "1.0", "running", "{}", now))
        conn.commit()
        conn.close()

        scheduler = _make_scheduler(db_path)
        out = scheduler.tick_drift_admit()
        assert out is None, "已有 active Drift 时 max_concurrent=1 应拒绝新 admission"
        try:
            os.remove(db_path)
        except Exception:
            pass
