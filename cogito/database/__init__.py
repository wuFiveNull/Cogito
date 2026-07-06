"""
cogito.database — 个人 Agent SQLite 数据库层
"""

from __future__ import annotations

from cogito.database.connection import AsyncDatabase
from cogito.database.ids import new_uuid, new_uuid_hex
from cogito.database.manager import DatabaseManager
from cogito.database.migrations import run_migrations
from cogito.database.schema import SCHEMA_VERSION
from cogito.database.service.memory_retriever import (
    deserialize_embedding,
    hybrid_search,
    keyword_search,
    keyword_search_multi,
    serialize_embedding,
    vector_search,
)

__all__ = [
    "AsyncDatabase",
    "DatabaseManager",
    "deserialize_embedding",
    "hybrid_search",
    "keyword_search",
    "keyword_search_multi",
    "new_uuid",
    "new_uuid_hex",
    "run_migrations",
    "SCHEMA_VERSION",
    "serialize_embedding",
    "vector_search",
]
