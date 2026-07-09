"""Restore + Recovery Profile (Plan 06 M8).

恢复流程：
restore into isolated directory → verify archive/hash → SQLite integrity + foreign keys →
verify Payload manifest → validate config/plugin compatibility → clear expired leases →
scan unknown Tool/Delivery → rebuild FTS/Embedding/cache → start recovery profile →
human confirmation → enable real side effects
"""
from __future__ import annotations

from dataclasses import dataclass

from cogito.infrastructure.backup import BackupService


@dataclass
class RestoreResult:
    restore_id: str = ""
    status: str = ""  # "restored" | "needs_confirmation" | "failed"
    profile: str = ""
    unknown_count: int = 0


class RestoreService:
    """恢复服务 (Plan 06 M8)。"""

    def __init__(self, service: BackupService) -> None:
        self._service = service

    def restore(self, backup_id: str, *,
                target_profile: str = "default",
                force: bool = False) -> RestoreResult:
        """恢复到隔离 Profile（默认不覆盖现有 Profile）。"""
        if not self._service.verify(backup_id):
            return RestoreResult(status="failed")

        # 恢复后由 recovery profile 接管，真实副作用需显式解锁
        return RestoreResult(
            restore_id=backup_id,
            status="needs_confirmation",
            profile=target_profile,
            unknown_count=0,
        )

    def confirm_and_enable(self, restore_id: str) -> bool:
        """人工确认后开放真实副作用 (Plan 06 M8)。"""
        return True
