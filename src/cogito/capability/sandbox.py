"""Sandbox Runtime profiles (Plan 03 M4).

实现 4 种 Sandbox Profile：
- read_only: 只读访问
- workspace_write: 工作区写入
- network_restricted: 受限网络
- plugin_process: 第三方插件进程外

每个 Profile 固定：工作目录和允许 Root、环境变量白名单、Secret 注入方式、
Host/IP/端口/协议、CPU/内存/进程数/磁盘/输出/超时、取消和进程树清理。

当前阶段为纯配置声明 + 校验逻辑（实际进程隔离由 Phase 2 部署形态决定）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxProfile:
    """Sandbox 执行 Profile（Plan 03 M4）。"""

    name: str
    description: str = ""
    allowed_roots: tuple[str, ...] = ()  # 允许的工作目录 Root
    env_whitelist: tuple[str, ...] = ()  # 环境变量白名单
    allow_network: bool = False
    allow_shell: bool = False
    allowed_hosts: tuple[str, ...] = ()  # host + port + scheme allowlist
    max_cpu_s: int = 30  # CPU 时间限制
    max_memory_mb: int = 256  # 内存限制
    max_processes: int = 4  # 进程数限制
    max_disk_mb: int = 100
    max_output_chars: int = 100_000
    timeout_s: int = 30
    subprocess: bool = False  # 是否进程外执行


# ── 预定义 Profile（Plan 03 M4）──

READ_ONLY = SandboxProfile(
    name="read_only",
    description="只读访问：不允许写入、网络、Shell",
    allowed_roots=("/workspace",),
    allow_network=False,
    allow_shell=False,
    max_output_chars=50_000,
)

WORKSPACE_WRITE = SandboxProfile(
    name="workspace_write",
    description="工作区写入：允许写工作区文件，无网络/Shell",
    allowed_roots=("/workspace", "/tmp"),
    allow_network=False,
    allow_shell=False,
    max_disk_mb=50,
)

NETWORK_RESTRICTED = SandboxProfile(
    name="network_restricted",
    description="受限网络：仅允许白名单 Host，阻止 loopback/云元数据",
    allowed_roots=("/workspace", "/tmp"),
    allow_network=True,
    allow_shell=False,
    allowed_hosts=("api.example.com:443",),
    max_cpu_s=10,
)

PLUGIN_PROCESS = SandboxProfile(
    name="plugin_process",
    description="第三方插件默认进程外：完全隔离，可被终止且无残留进程",
    allowed_roots=("/workspace/plugins",),
    allow_network=False,
    allow_shell=False,
    subprocess=True,
    max_memory_mb=128,
    max_processes=1,
    timeout_s=15,
)


def get_profile(name: str) -> SandboxProfile:
    """按名称获取预定义 Profile。"""
    profiles = {
        "read_only": READ_ONLY,
        "workspace_write": WORKSPACE_WRITE,
        "network_restricted": NETWORK_RESTRICTED,
        "plugin_process": PLUGIN_PROCESS,
    }
    if name not in profiles:
        raise ValueError(f"Unknown sandbox profile: {name!r}")
    return profiles[name]


def validate_profile(profile: SandboxProfile) -> list[str]:
    """校验 Profile 声明完整性（Plan 03 M4 逃逸测试准备）。"""
    errors: list[str] = []
    if not profile.allowed_roots:
        errors.append(f"profile {profile.name}: no allowed_roots declared")
    if profile.allow_network and not profile.allowed_hosts:
        errors.append(f"profile {profile.name}: network allowed but no host allowlist")
    if profile.allow_shell and not profile.env_whitelist:
        errors.append(f"profile {profile.name}: shell allowed but no env whitelist")
    return errors
