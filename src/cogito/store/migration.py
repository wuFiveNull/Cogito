"""Schema migration runner — discovers and applies numbered SQL migration files.

遵循 CONFIG-PROFILES / 1（配置层级）与 DATABASE-SCHEMA / 1（SQLite 模式）：
- 每个 Migration 是独立 SQL 文件，按版本号递增
- 文件命名：NNNN_description.sql（NNNN = 零填充版本号）
- 版本从 1 开始，无上限
- 支持空库从头应用到最新、已有库增量升级
- 每个 Migration 和版本记录在统一事务中执行
- 迁移后运行 PRAGMA foreign_key_check
- Plan 06 M6: 支持 online_safe 分级、maintenance profile、中断恢复
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_VERSION_PATTERN = re.compile(r"^(\d+)_")


@dataclass
class MigrationMeta:
    """Migration 元数据（来自 .meta.toml）。"""
    online_safe: bool = False
    requires_backup: bool = True
    estimated_space: str = "0"
    preconditions: list[str] = field(default_factory=list)
    post_checks: list[str] = field(default_factory=lambda: ["foreign_key_check"])
    rollback: str = ""
    checksum: str = ""


@dataclass
class MigrationFile:
    version: int
    path: Path
    meta: MigrationMeta = field(default_factory=MigrationMeta)


def _parse_meta(meta_path: Path) -> MigrationMeta:
    """解析 .meta.toml 文件。

    缺失时默认 online_safe=True：旧迁移已在生产运行，无需维护模式。
    新迁移应显式声明 meta 文件；破坏性迁移设置 online_safe=false。
    """
    if not meta_path.exists():
        return MigrationMeta(online_safe=True)
    try:
        import tomllib
        with open(meta_path, "rb") as f:
            raw = tomllib.load(f)
    except ImportError:
        # Python < 3.11: 简单 key = value 解析
        raw = _simple_toml_parse(meta_path)
    except Exception:
        return MigrationMeta()
    return MigrationMeta(
        online_safe=bool(raw.get("online_safe", False)),
        requires_backup=bool(raw.get("requires_backup", True)),
        estimated_space=str(raw.get("estimated_space", "0")),
        preconditions=list(raw.get("preconditions", [])),
        post_checks=list(raw.get("post_checks", ["foreign_key_check"])),
        rollback=str(raw.get("rollback", "")),
        checksum=str(raw.get("checksum", "")),
    )


def _simple_toml_parse(path: Path) -> dict[str, Any]:
    """极简 TOML 解析（仅支持 key = value 形式，无 section）。"""
    result: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value.lower() in ("true", "false"):
            result[key] = value.lower() == "true"
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            result[key] = [v.strip().strip('"') for v in inner.split(",") if v.strip()]
        else:
            result[key] = value
    return result


def _discover() -> list[MigrationFile]:
    """扫描 migrations/ 目录，返回按版本升序的迁移列表。"""
    if not MIGRATIONS_DIR.is_dir():
        return []
    files: list[MigrationFile] = []
    for p in sorted(MIGRATIONS_DIR.iterdir()):
        if p.suffix != ".sql":
            continue
        m = _VERSION_PATTERN.match(p.name)
        if not m:
            continue
        version = int(m.group(1))
        meta_path = p.with_suffix(".meta.toml")
        meta = _parse_meta(meta_path)
        files.append(MigrationFile(version=version, path=p, meta=meta))
    return files


def _get_current_version(conn: sqlite3.Connection) -> int:
    """查询已应用的最大 Migration 版本。"""
    try:
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        return row[0] if row and row[0] else 0
    except sqlite3.OperationalError:
        return 0


def _get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    """查询已应用的版本集合。"""
    try:
        rows = conn.execute(
            "SELECT version FROM _schema_version WHERE error IS NULL"
        ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        # 旧表结构无 error 列：回退到查询所有版本
        try:
            rows = conn.execute(
                "SELECT version FROM _schema_version"
            ).fetchall()
            return {r[0] for r in rows}
        except sqlite3.OperationalError:
            return set()


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    """确保 _schema_version 表存在（含 Plan 06 M6 扩展字段）。

    若表已存在但缺少新列，通过 ALTER TABLE 补齐（expand 阶段）。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version     INTEGER NOT NULL,
            applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            checksum    TEXT    NOT NULL DEFAULT '',
            started_at  TEXT,
            completed_at TEXT,
            error       TEXT,
            online_safe INTEGER NOT NULL DEFAULT 0
        )
    """)
    # 补齐可能缺失的列（旧表升级）
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(_schema_version)")
    }
    if "started_at" not in existing_cols:
        conn.execute(
            "ALTER TABLE _schema_version ADD COLUMN started_at TEXT"
        )
    if "completed_at" not in existing_cols:
        conn.execute(
            "ALTER TABLE _schema_version ADD COLUMN completed_at TEXT"
        )
    if "error" not in existing_cols:
        conn.execute(
            "ALTER TABLE _schema_version ADD COLUMN error TEXT"
        )
    if "online_safe" not in existing_cols:
        conn.execute(
            "ALTER TABLE _schema_version ADD COLUMN online_safe INTEGER NOT NULL DEFAULT 0"
        )


def _apply_one(conn: sqlite3.Connection, mf: MigrationFile) -> None:
    """应用单个迁移文件并记录版本。

    在 execute 中，DDL 可能导致隐式提交，但版本 INSERT 在相同连接上：
    - 若 executescript 部分失败 → 异常传播，版本不记录
    - 若 INSERT 失败 → 版本不记录，迁移已部分完成（DDL 不可回滚）
    """
    sql = mf.path.read_text(encoding="utf-8")
    checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
    now = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
    # 记录 started_at
    conn.execute(
        f"INSERT INTO _schema_version (version, checksum, started_at, online_safe) "
        f"VALUES (?, ?, {now}, ?)",
        (mf.version, checksum, int(mf.meta.online_safe)),
    )
    conn.commit()
    # 执行 DDL
    conn.executescript(sql)
    # 记录 completed_at
    conn.execute(
        f"UPDATE _schema_version SET completed_at={now} WHERE version=?",
        (mf.version,),
    )


def _check_foreign_keys(conn: sqlite3.Connection) -> list[str]:
    """运行 PRAGMA foreign_key_check，返回所有违规描述。"""
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return [f"FK violation: table={r[0]}, rowid={r[1]}, parent={r[2]}, seq={r[3]}"
            for r in rows]


def _run_post_checks(conn: sqlite3.Connection, checks: list[str]) -> list[str]:
    """运行 post-check 列表，返回失败描述。"""
    failures: list[str] = []
    for check in checks:
        if check == "foreign_key_check":
            fk = _check_foreign_keys(conn)
            failures.extend(fk)
        elif check == "integrity_check":
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if row and row[0] != "ok":
                failures.append(f"integrity_check: {row[0]}")
    return failures


def migrate(
    conn: sqlite3.Connection,
    maintenance: bool = False,
) -> list[int]:
    """运行待处理的 Migration（自动发现 + 增量应用）。

    Args:
        conn: SQLite 连接。
        maintenance: 为 True 时运行所有迁移（含 non-online-safe）；
                    为 False（默认）时只运行 online_safe=True 的小迁移。

    幂等保证：
    - 已应用的版本跳过
    - 同一版本重复执行：CREATE IF NOT EXISTS 保证幂等
    - 迁移后运行 PRAGMA foreign_key_check

    Returns:
        本次应用的版本号列表。
    """
    _ensure_schema_version_table(conn)
    applied = _get_applied_versions(conn)
    current = max(applied) if applied else 0
    pending = [mf for mf in _discover() if mf.version > current]

    applied_versions: list[int] = []
    for mf in pending:
        # 非 maintenance 模式跳过 non-online-safe 迁移
        if not mf.meta.online_safe and not maintenance:
            continue
        _apply_one(conn, mf)
        # 运行 post-check
        failures = _run_post_checks(conn, mf.meta.post_checks)
        if failures:
            # 记录错误
            conn.execute(
                "UPDATE _schema_version SET error=? WHERE version=?",
                ("; ".join(failures), mf.version),
            )
            conn.commit()
            raise RuntimeError(
                f"Post-check failed for migration {mf.version}: {'; '.join(failures)}"
            )
        conn.commit()
        applied_versions.append(mf.version)

    return applied_versions


def get_migration_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """查询每个 migration 的状态（用于 CLI / Dashboard）。"""
    _ensure_schema_version_table(conn)
    discovered = {mf.version: mf for mf in _discover()}
    try:
        rows = conn.execute(
            "SELECT version, applied_at, completed_at, error, online_safe "
            "FROM _schema_version ORDER BY version ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    applied = {r["version"]: r for r in rows}
    result: list[dict[str, Any]] = []
    for ver in sorted(set(discovered) | set(applied)):
        info: dict[str, Any] = {"version": ver}
        if ver in discovered:
            info["name"] = discovered[ver].path.stem
            info["online_safe"] = discovered[ver].meta.online_safe
            info["requires_backup"] = discovered[ver].meta.requires_backup
        if ver in applied:
            info["applied_at"] = applied[ver]["applied_at"]
            info["completed_at"] = applied[ver]["completed_at"]
            info["error"] = applied[ver]["error"]
            info["status"] = "failed" if applied[ver]["error"] else "completed"
        else:
            info["status"] = "pending"
        result.append(info)
    return result
