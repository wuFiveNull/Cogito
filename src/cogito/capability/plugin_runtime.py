"""SqlitePluginRuntime —— PluginRuntime Protocol 的 SQLite 实现 (PLAN-10 M5)。

持久化到 store/plugins 表；多源发现（内置 / 用户 / 项目 / pip）；
连续失败熔断 (3/60s)；权限映射到 Sandbox/Policy。
第三方默认 subprocess；in_process_trusted 仅内置或用户显式批准。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from cogito.capability.plugin_supervisor import (
    PluginPolicyAdapter,
    PluginProcessSupervisor,
)

_LOGGER = logging.getLogger("cogito.plugin_runtime")


# ── 公开面 (与旧 service/plugin_runtime Protocol 对齐) ──────────────────


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    version: str = "1.0"
    api_version: str = "1"
    permissions: tuple[str, ...] = ()
    entry_point: str = ""
    config_schema: dict[str, Any] | None = None
    dependencies: tuple[str, ...] = ()
    subprocess: bool = True  # 第三方默认进程外
    source: str = "builtin"
    source_path: str = ""
    trusted: bool = False
    install_hash: str = ""


@dataclass
class PluginState:
    plugin_id: str = ""
    status: str = "discovered"
    manifest: PluginManifest | None = None
    error: str = ""
    fail_count: int = 0
    last_fail_at: str = ""
    started_at: str = ""
    process_id: int | None = None


class PluginRuntime(Protocol):
    """插件生命周期管理接口（唯一写入口）。"""

    def discover(self, *paths: str) -> list[PluginManifest]:
        ...

    def validate(self, manifest: PluginManifest) -> bool:
        ...

    def install(self, manifest: PluginManifest) -> PluginState:
        ...

    def enable(self, plugin_id: str) -> PluginState | None:
        ...

    def disable(self, plugin_id: str) -> PluginState | None:
        ...

    def record_failure(self, plugin_id: str) -> None:
        ...

    def get(self, plugin_id: str) -> PluginState | None:
        ...

    def list_all(self) -> list[PluginState]:
        ...

    def start(self, plugin_id: str) -> PluginState | None:
        ...

    def stop(self, plugin_id: str) -> PluginState | None:
        ...

    def health(self, plugin_id: str) -> dict[str, Any]:
        ...

    def rollback(self, plugin_id: str) -> PluginState | None:
        ...

    def close(self) -> None:
        ...

# 熔断器参数
CIRCUIT_MAX_FAILURES = 3
CIRCUIT_WINDOW_S = 60.0


class CircuitBreaker:
    """熔断器：连续 N 次/窗口秒内触发熔断。"""

    def __init__(self, max_failures: int = CIRCUIT_MAX_FAILURES,
                 window_s: float = CIRCUIT_WINDOW_S) -> None:
        self._max = max_failures
        self._window = window_s
        self._failures: list[float] = []

    def record_failure(self) -> None:
        now = datetime.now(UTC).timestamp()
        self._failures.append(now)
        cutoff = now - self._window
        self._failures = [t for t in self._failures if t >= cutoff]

    def record_success(self) -> None:
        if self._failures:
            self._failures.pop(0)

    def reset(self) -> None:
        """人工重置熔断器（管理员恢复）。"""
        self._failures.clear()

    @property
    def is_ok(self) -> bool:
        return len(self._failures) < self._max

    @property
    def should_disable(self) -> bool:
        return len(self._failures) >= self._max


class SqlitePluginRuntime:
    """PluginRuntime Protocol 的 SQLite 实现。"""

    def __init__(
        self,
        conn: Any,
        *,
        builtin_paths: list[str] | None = None,
        granted_permissions: set[str] | None = None,
        supervisor: PluginProcessSupervisor | None = None,
    ) -> None:
        self._conn = conn
        self._builtin_paths = builtin_paths or []
        self._plugins: dict[str, PluginState] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._policy = PluginPolicyAdapter(granted_permissions)
        self._supervisor = supervisor or PluginProcessSupervisor()
        self._load_from_db()

    # ── 持久化 ──────────────────────────────────────────────────────

    def _load_from_db(self) -> None:
        if self._conn is None:
            return
        try:
            rows = self._conn.execute("SELECT * FROM plugins").fetchall()
        except Exception:
            return  # 表不存在（旧 DB 未 migration）
        for r in rows:
            manifest = PluginManifest(
                plugin_id=r["plugin_id"],
                version=r["version"],
                api_version=r["api_version"],
                permissions=tuple(json.loads(r["permissions"] or "[]")),
                entry_point=r["entry_point"],
                subprocess=(
                    r["isolation"] if "isolation" in r.keys() else "subprocess"
                ) != "in_process_trusted",
                source=r["source"],
                source_path=r["source_path"],
                trusted=bool(r["trusted"]) if "trusted" in r.keys() else False,
                install_hash=r["install_hash"],
            )
            state = PluginState(
                plugin_id=r["plugin_id"],
                # A subprocess cannot be assumed alive after Core restart.
                status="stopped" if r["status"] == "running" else r["status"],
                manifest=manifest,
                error=r["error"],
                fail_count=r["fail_count"],
                last_fail_at=r["last_fail_at"] or "",
                started_at=r["started_at"] or "",
            )
            self._plugins[r["plugin_id"]] = state

    def _persist(self, state: PluginState) -> None:
        if state.manifest is None:
            return
        if self._conn is None:
            return  # conn=None: 纯内存模式（测试/旧 API 兼容）
        now = datetime.now(UTC).isoformat()
        perms = json.dumps(list(state.manifest.permissions))
        isolation = "subprocess" if state.manifest.subprocess else "in_process_trusted"
        self._conn.execute(
            """INSERT INTO plugins
               (plugin_id, version, api_version, status, source, source_path,
                entry_point, permissions, install_hash, error, fail_count,
                last_fail_at, started_at, created_at, isolation, trusted, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(plugin_id) DO UPDATE SET
                 version=excluded.version,
                 api_version=excluded.api_version,
                 status=excluded.status,
                 source=excluded.source,
                 source_path=excluded.source_path,
                 entry_point=excluded.entry_point,
                 permissions=excluded.permissions,
                 install_hash=excluded.install_hash,
                 error=excluded.error,
                 fail_count=excluded.fail_count,
                 last_fail_at=excluded.last_fail_at,
                 started_at=excluded.started_at,
                 isolation=excluded.isolation,
                 trusted=excluded.trusted,
                 updated_at=excluded.updated_at""",
            (
                state.plugin_id, state.manifest.version, state.manifest.api_version,
                state.status, state.manifest.source, state.manifest.source_path,
                state.manifest.entry_point, perms, state.manifest.install_hash,
                state.error, state.fail_count, state.last_fail_at,
                state.started_at, now, isolation, int(state.manifest.trusted), now,
            ),
        )
        self._conn.commit()

    # ── Protocol 实现 ──────────────────────────────────────────────

    def discover(self, *paths: str) -> list[PluginManifest]:
        """多源发现：内置 / 用户 / 项目 / pip entry-point。"""
        manifests: list[PluginManifest] = []
        seen: set[str] = set()

        # 内置路径
        for p in self._builtin_paths:
            for m in self._scan_dir(p, source="builtin", trusted=True):
                if m.plugin_id not in seen:
                    seen.add(m.plugin_id)
                    manifests.append(m)

        # 用户路径 ~/.cogito/plugins
        user_path = os.path.expanduser("~/.cogito/plugins")
        for m in self._scan_dir(user_path, source="user"):
            if m.plugin_id not in seen:
                seen.add(m.plugin_id)
                manifests.append(m)

        # 显式传入路径
        for p in paths:
            for m in self._scan_dir(p, source="project"):
                if m.plugin_id not in seen:
                    seen.add(m.plugin_id)
                    manifests.append(m)

        # pip entry-point (可选)
        manifests.extend(self._discover_entry_points(seen))
        return manifests

    def _scan_dir(
        self, path: str, *, source: str = "project", trusted: bool = False,
    ) -> list[PluginManifest]:
        """扫描目录下的 plugin.yaml / plugin.toml。"""
        out: list[PluginManifest] = []
        if not path or not os.path.isdir(path):
            return out
        for name in os.listdir(path):
            sub = os.path.join(path, name)
            if not os.path.isdir(sub):
                continue
            yaml_path = os.path.join(sub, "plugin.yaml")
            toml_path = os.path.join(sub, "plugin.toml")
            manifest_path = yaml_path if os.path.isfile(yaml_path) else (
                toml_path if os.path.isfile(toml_path) else None
            )
            if manifest_path is None:
                continue
            try:
                m = self._parse_manifest(
                    manifest_path, source_path=sub, source=source, trusted=trusted,
                )
                if m is not None:
                    out.append(m)
            except Exception as e:
                _LOGGER.warning("Bad manifest %s: %s", manifest_path, e)
        return out

    def _parse_manifest(
        self,
        path: str,
        source_path: str,
        *,
        source: str = "project",
        trusted: bool = False,
    ) -> PluginManifest | None:
        """解析 plugin.yaml / plugin.toml（纯文本启发式，避免依赖第三方解析库）。"""
        text: str = ""
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            return None

        # 仅支持扁平键值（本项目 plugin.yaml 的最简形状）
        data: dict[str, str | list[str]] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("- "):
                # permissions 列表项
                key = "permissions"
                val = line[2:].strip()
                data.setdefault(key, [])
                if isinstance(data[key], list):
                    data[key].append(val)
                continue
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and v:
                data[k] = v

        pid = data.get("id") or data.get("plugin_id")
        if not pid:
            return None
        perms = data.get("permissions") or []
        if isinstance(perms, str):
            perms = [perms]
        isolation = str(data.get("isolation") or "")
        subprocess_mode = (
            isolation != "in_process_trusted" if isolation
            else str(data.get("subprocess") or "true").lower() != "false"
        )
        return PluginManifest(
            plugin_id=str(pid),
            version=str(data.get("version") or "1.0"),
            api_version=str(data.get("api_version") or "1"),
            permissions=tuple(str(p) for p in perms),
            entry_point=str(data.get("entry_point") or ""),
            dependencies=tuple(),
            subprocess=subprocess_mode,
            source=source,
            source_path=source_path,
            trusted=trusted,
            install_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def _discover_entry_points(self, seen: set[str]) -> list[PluginManifest]:
        """pip entry-point 发现（可选，失败静默）。"""
        try:
            from importlib.metadata import entry_points
        except ImportError:
            return []
        try:
            eps = entry_points(group="cogito.plugins")
        except TypeError:
            try:
                eps = entry_points().get("cogito.plugins", [])
            except Exception:
                return []
        out: list[PluginManifest] = []
        for ep in eps:
            if ep.name in seen:
                continue
            seen.add(ep.name)
            dist = getattr(ep, "dist", None)
            try:
                source_path = str(dist.locate_file("")) if dist is not None else os.getcwd()
            except Exception:
                source_path = os.getcwd()
            out.append(PluginManifest(
                plugin_id=ep.name,
                version=str(getattr(ep, "dist", None) and getattr(ep.dist, "version", "0.0.0")) or "0.0.0",
                api_version="1",
                entry_point=f"{ep.value}",
                source="pip",
                source_path=source_path,
                install_hash=str(getattr(ep, "value", "")),
            ))
        return out

    def validate(self, manifest: PluginManifest) -> bool:
        if not manifest.plugin_id or not manifest.version:
            return False
        if re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", manifest.plugin_id,
        ) is None:
            return False
        api = manifest.api_version.replace(" ", "")
        if not (api in ("1", "1.0") or (">=1" in api and "<2" in api)):
            return False
        if not manifest.subprocess and not (
            manifest.source == "builtin" and manifest.trusted
        ):
            return False
        if manifest.entry_point and not re.fullmatch(
            r"[A-Za-z_][\w.]*(:[A-Za-z_]\w*)?", manifest.entry_point,
        ):
            return False
        return True

    def install(self, manifest: PluginManifest) -> PluginState:
        if not self.validate(manifest):
            raise ValueError(f"invalid plugin manifest: {manifest.plugin_id!r}")
        existing = self._plugins.get(manifest.plugin_id)
        if existing is not None:
            self._snapshot(existing)
            self._supervisor.stop(manifest.plugin_id)
        state = PluginState(
            plugin_id=manifest.plugin_id,
            status="installed",
            manifest=manifest,
        )
        self._plugins[manifest.plugin_id] = state
        self._persist(state)
        self._audit(manifest.plugin_id, "install", "success", manifest.version)
        return state

    def enable(self, plugin_id: str) -> PluginState | None:
        s = self._plugins.get(plugin_id)
        if not s or s.status not in ("installed", "configured", "disabled", "degraded"):
            return None
        if not self._breaker(plugin_id).is_ok:
            s.status = "degraded"
            self._persist(s)
            return s
        s.status = "enabled"
        self._persist(s)
        self._audit(plugin_id, "enable", "success")
        return s

    def disable(self, plugin_id: str) -> PluginState | None:
        s = self._plugins.get(plugin_id)
        if not s:
            return None
        self._supervisor.stop(plugin_id)
        s.process_id = None
        s.status = "disabled"
        self._persist(s)
        self._audit(plugin_id, "disable", "success")
        return s

    def record_failure(self, plugin_id: str) -> None:
        s = self._plugins.get(plugin_id)
        if not s:
            return
        self._breaker(plugin_id).record_failure()
        s.fail_count += 1
        s.last_fail_at = datetime.now(UTC).isoformat()
        if self._breaker(plugin_id).should_disable:
            s.status = "degraded"
            self._supervisor.stop(plugin_id)
            s.process_id = None
        self._persist(s)
        self._audit(
            plugin_id,
            "failure",
            "degraded" if s.status == "degraded" else "recorded",
        )

    def get(self, plugin_id: str) -> PluginState | None:
        return self._plugins.get(plugin_id)

    def list_all(self) -> list[PluginState]:
        return list(self._plugins.values())

    def start(self, plugin_id: str) -> PluginState | None:
        state = self._plugins.get(plugin_id)
        if state is None or state.manifest is None:
            return None
        if state.status not in ("enabled", "stopped"):
            return state
        try:
            self._policy.authorize(state.manifest.permissions)
            if not state.manifest.subprocess:
                if not (
                    state.manifest.source == "builtin" and state.manifest.trusted
                ):
                    raise PermissionError("in-process plugin is not explicitly trusted")
                state.status = "running"
                state.process_id = os.getpid()
            else:
                state.process_id = self._supervisor.start(state.manifest)
                state.status = "running"
            state.started_at = datetime.now(UTC).isoformat()
            state.error = ""
            self._persist(state)
            self._audit(
                plugin_id, "start", "success", f"pid={state.process_id}",
            )
        except Exception as exc:
            state.status = "degraded"
            state.error = type(exc).__name__
            state.fail_count += 1
            state.last_fail_at = datetime.now(UTC).isoformat()
            self._persist(state)
            self._audit(plugin_id, "start", "denied", type(exc).__name__)
        return state

    def stop(self, plugin_id: str) -> PluginState | None:
        state = self._plugins.get(plugin_id)
        if state is None:
            return None
        self._supervisor.stop(plugin_id)
        state.process_id = None
        state.status = "stopped"
        self._persist(state)
        self._audit(plugin_id, "stop", "success")
        return state

    def health(self, plugin_id: str) -> dict[str, Any]:
        state = self._plugins.get(plugin_id)
        if state is None:
            return {"status": "missing", "plugin_id": plugin_id}
        if state.manifest and not state.manifest.subprocess and state.status == "running":
            return {"status": "running", "plugin_id": plugin_id, "pid": os.getpid()}
        health = self._supervisor.health(plugin_id)
        if health["status"] == "crashed" and state.status == "running":
            state.status = "degraded"
            state.error = "process_crashed"
            self._persist(state)
            self._audit(plugin_id, "health", "degraded", "process_crashed")
        return {"plugin_id": plugin_id, **health}

    def rollback(self, plugin_id: str) -> PluginState | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT snapshot_id, manifest_json, status FROM plugin_snapshots "
            "WHERE plugin_id=? ORDER BY created_at DESC, snapshot_id DESC LIMIT 1",
            (plugin_id,),
        ).fetchone()
        if row is None:
            return None
        self._supervisor.stop(plugin_id)
        raw = json.loads(row["manifest_json"])
        raw["permissions"] = tuple(raw.get("permissions") or ())
        raw["dependencies"] = tuple(raw.get("dependencies") or ())
        manifest = PluginManifest(**raw)
        state = PluginState(plugin_id=plugin_id, status="installed", manifest=manifest)
        self._plugins[plugin_id] = state
        self._persist(state)
        self._conn.execute(
            "DELETE FROM plugin_snapshots WHERE snapshot_id=?", (row["snapshot_id"],),
        )
        self._conn.commit()
        self._audit(plugin_id, "rollback", "success", manifest.version)
        return state

    def close(self) -> None:
        for plugin_id, state in list(self._plugins.items()):
            if state.status == "running":
                self.stop(plugin_id)
        self._supervisor.close()

    def _snapshot(self, state: PluginState) -> None:
        if self._conn is None or state.manifest is None:
            return
        self._conn.execute(
            "INSERT INTO plugin_snapshots "
            "(snapshot_id, plugin_id, manifest_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f"psnap-{uuid.uuid4().hex}", state.plugin_id,
                json.dumps(asdict(state.manifest), ensure_ascii=False),
                state.status, datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()

    def _audit(
        self, plugin_id: str, action: str, outcome: str, safe_detail: str = "",
    ) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT INTO plugin_runtime_audit "
            "(audit_id, plugin_id, action, outcome, safe_detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"paudit-{uuid.uuid4().hex}", plugin_id, action, outcome,
                safe_detail[:500], datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()

    # ── 内部 ──────────────────────────────────────────────────────

    def _breaker(self, plugin_id: str) -> CircuitBreaker:
        if plugin_id not in self._breakers:
            self._breakers[plugin_id] = CircuitBreaker()
        return self._breakers[plugin_id]


