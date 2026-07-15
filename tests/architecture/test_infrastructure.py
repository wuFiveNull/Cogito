"""Track E: Profile + Payload + Migration + Backup — Plan 06 M1/M3/M6/M7."""

from __future__ import annotations

import sqlite3
import tempfile

import pytest

from cogito.infrastructure.payload_store import PayloadStore
from cogito.infrastructure.profile import (
    ProfileLayout,
    create_profile,
    display_home,
    get_home,
)


def test_get_home_default() -> None:
    h = get_home()
    assert ".workspace" in str(h)


def test_get_home_profile_isolated() -> None:
    """Profile 之间完全隔离。"""
    a = get_home("profile-a")
    b = get_home("profile-b")
    assert a != b
    assert "profile-a" in str(a)
    assert "profile-b" in str(b)


def test_display_home() -> None:
    assert len(display_home()) > 0


def test_profile_layout_directories() -> None:
    """目录布局完整 (Plan 06 M1 目标布局)。"""
    tmp = tempfile.mkdtemp()
    layout = ProfileLayout(home=__import__("pathlib").Path(tmp))
    layout.ensure_directories()
    assert layout.payload_dir.exists()
    assert layout.plugins_dir.exists()
    assert layout.backups_dir.exists()
    assert layout.database.parent.exists()


def test_payload_store_roundtrip() -> None:
    """内容寻址写入 + 读取 + hash 校验。"""
    tmp = tempfile.mkdtemp()
    store = PayloadStore(tmp, sqlite3.connect(":memory:"))
    obj = store.put(b"hello world", content_type="text/plain")
    assert obj.size_bytes == 11
    assert len(obj.sha256) == 64
    data = store.get(obj.payload_id)
    assert data == b"hello world"


def test_payload_store_hash_verification() -> None:
    """hash 校验检测损坏。"""
    tmp = tempfile.mkdtemp()
    store = PayloadStore(tmp, sqlite3.connect(":memory:"))
    obj = store.put(b"test")
    # 损坏文件
    path_cls = __import__("pathlib").Path
    path_cls(obj.storage_uri).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="corrupted"):
        store.get(obj.payload_id)


def test_payload_store_atomic_write() -> None:
    """原子写：不留下临时文件。"""
    tmp = tempfile.mkdtemp()
    store = PayloadStore(tmp, sqlite3.connect(":memory:"))
    store.put(b"atomic")
    # 不应有 .tmp_ 文件
    path_cls = __import__("pathlib").Path
    tmp_files = list(path_cls(tmp).rglob(".tmp_*"))
    assert len(tmp_files) == 0


def test_create_profile() -> None:
    """从模板创建 Profile。"""
    tmp = tempfile.mkdtemp()
    import cogito.infrastructure.profile as p

    orig = p.get_home
    p.get_home = lambda profile="default": __import__("pathlib").Path(tmp)
    try:
        layout = create_profile("new")
        assert layout.plugins_dir.exists()
    finally:
        p.get_home = orig


def test_gc_dry_run() -> None:
    """GC 默认 dry-run（Plan 06 M3）。"""
    tmp = tempfile.mkdtemp()
    store = PayloadStore(tmp, sqlite3.connect(":memory:"))
    result = store.gc(dry_run=True)
    assert isinstance(result, list)
