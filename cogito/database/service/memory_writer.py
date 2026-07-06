"""
cogito.database.service.memory_writer — MemoryWriter

对应设计文档第 11、12、13 节。

职责：
- 校验候选记忆
- 按 memory_key 去重（新增或增强）
- 替代旧记忆（搬家等场景）
- 提取完成事务（记忆写入 + 事件状态更新）
"""

from __future__ import annotations

import json
from typing import Any

from cogito.database.connection import AsyncDatabase
from cogito.database.ids import new_uuid
from cogito.database.repository.memories import MemoryRepository
from cogito.database.service.event_service import EventService
from cogito.database.utils import utcnow, json_list, json_obj


class MemoryWriter:
    """记忆写入业务服务。"""

    MEMORY_TYPES = frozenset({"fact", "preference", "rule", "event"})

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db
        self._repo = MemoryRepository(db)
        self._event_service = EventService(db)

    # ── 写入单条记忆（含去重） ──────────────────────────────────

    async def upsert_memory(
        self,
        *,
        user_id: str,
        memory_type: str,
        memory_key: str,
        content: str,
        value_json: dict[str, Any] | None = None,
        importance: float = 0.5,
        confidence: float = 0.8,
        valid_from: str | None = None,
        valid_until: str | None = None,
        source_group_id: str | None = None,
        source_event_ids: list[str] | None = None,
        embedding: bytes | None = None,
        embedding_dim: int | None = None,
        embedding_model: str | None = None,
        created_by_span_id: str | None = None,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        """写入记忆，自动去重。

        规则：
        - 如果 memory_key 已有 active 记录且内容基本一致 → 增强置信度
        - 如果 memory_key 已有 active 记录但内容不同 → 替代旧记忆
        - 如果 memory_key 无 active 记录 → 新建

        Args:
            user_id: 用户 ID
            memory_type: 记忆类型 (fact/preference/rule/event)
            memory_key: 记忆键
            content: 记忆内容（自然语言描述）
            value_json: 结构化数据
            importance: 重要性 [0.0, 1.0]
            confidence: 置信度 [0.0, 1.0]
            source_event_ids: 来源事件 ID 列表
            embedding: 向量 BLOB
            embedding_dim: 向量维度
            embedding_model: 向量模型名称
            created_by_span_id: 创建 span ID

        Returns:
            写入后的记忆记录
        """
        # 校验
        if memory_type not in self.MEMORY_TYPES:
            raise ValueError(
                f"Invalid memory_type: {memory_type}. "
                f"Must be one of {self.MEMORY_TYPES}"
            )

        try:
            await self._db.begin_immediate()

            existing = await self._repo.get_active_by_key(user_id, memory_key)
            source_ids_json = json_list(source_event_ids or [])

            if existing is not None:
                if _is_similar(existing, content, confidence, importance):
                    result = await self._reinforce_memory(
                        existing,
                        source_group_id=source_group_id,
                        source_event_ids_json=source_ids_json,
                        created_by_span_id=created_by_span_id,
                    )
                else:
                    result = await self._supersede_memory(
                        existing_memory=existing,
                        user_id=user_id,
                        memory_type=memory_type,
                        memory_key=memory_key,
                        content=content,
                        value_json=value_json,
                        importance=importance,
                        confidence=confidence,
                        valid_from=valid_from,
                        source_group_id=source_group_id,
                        source_event_ids_json=source_ids_json,
                        embedding=embedding,
                        embedding_dim=embedding_dim,
                        embedding_model=embedding_model,
                        created_by_span_id=created_by_span_id,
                        memory_id=memory_id,
                    )
            else:
                result = await self._repo.insert({
                    "id": memory_id or new_uuid(),
                    "user_id": user_id,
                    "memory_type": memory_type,
                    "memory_key": memory_key,
                    "content": content,
                    "value_json": json_obj(value_json or {}),
                    "embedding": embedding,
                    "embedding_dim": embedding_dim,
                    "embedding_model": embedding_model,
                    "importance": importance,
                    "confidence": confidence,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "source_group_id": source_group_id,
                    "source_event_ids_json": source_ids_json,
                    "created_by_span_id": created_by_span_id,
                    "updated_by_span_id": created_by_span_id,
                })

            await self._db.commit()
            return result
        except Exception:
            await self._db.rollback()
            raise

    async def _reinforce_memory(
        self,
        existing: dict[str, Any],
        source_group_id: str | None,
        source_event_ids_json: str,
        created_by_span_id: str | None,
    ) -> dict[str, Any]:
        """重复记忆 — 增强置信度。

        对应文档第 12.4 节。
        """
        new_confidence = min(1.0, existing["confidence"] + 0.05)
        return await self._repo.update_status(
            existing["id"],
            {
                "confidence": new_confidence,
                "source_group_id": source_group_id,
                "source_event_ids_json": source_event_ids_json,
                "updated_by_span_id": created_by_span_id,
            },
        )

    async def _supersede_memory(
        self,
        existing_memory: dict[str, Any],
        user_id: str,
        memory_type: str,
        memory_key: str,
        content: str,
        value_json: dict[str, Any] | None,
        importance: float,
        confidence: float,
        valid_from: str | None,
        source_group_id: str | None,
        source_event_ids_json: str,
        embedding: bytes | None,
        embedding_dim: int | None,
        embedding_model: str | None,
        created_by_span_id: str | None,
        memory_id: str | None,
    ) -> dict[str, Any]:
        """替代旧记忆 — 调用方必须已在事务中执行。

        对应文档第 12.5 节。
        由 upsert_memory 或 complete_extraction 负责事务边界。
        """
        now = utcnow()

        # 旧记忆设为 superseded
        await self._repo.update_status(
            existing_memory["id"],
            {
                "status": "superseded",
                "valid_until": valid_from or now,
                "updated_by_span_id": created_by_span_id,
            },
        )

        # 新记忆（linked via supersedes_id）
        new_id = memory_id or new_uuid()
        new_mem = await self._repo.insert({
            "id": new_id,
            "user_id": user_id,
            "memory_type": memory_type,
            "memory_key": memory_key,
            "content": content,
            "value_json": json_obj(value_json or {}),
            "embedding": embedding,
            "embedding_dim": embedding_dim,
            "embedding_model": embedding_model,
            "importance": importance,
            "confidence": confidence,
            "valid_from": valid_from or now,
            "source_group_id": source_group_id,
            "source_event_ids_json": source_event_ids_json,
            "supersedes_id": existing_memory["id"],
            "created_by_span_id": created_by_span_id,
            "updated_by_span_id": created_by_span_id,
        })

        return new_mem

    # ── 提取完成事务 ────────────────────────────────────────────

    async def complete_extraction(
        self,
        group_id: str,
        memories: list[dict[str, Any]],
        *,
        created_by_span_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """完成一次记忆提取：写入记忆（自动去重）+ 标记事件完成。

        对应文档第 13.1 节「记忆写入和事件状态更新放在同一事务中」。
        整个提取流程（查询、写入/更新、事件完成）在单个 IMMEDIATE 事务中执行。

        Args:
            group_id: 提取组 ID
            memories: 候选记忆列表，每项包含 memory_type, memory_key, content 等
            created_by_span_id: 创建 span ID

        Returns:
            写入后的记忆记录列表
        """
        written: list[dict[str, Any]] = []

        try:
            await self._db.begin_immediate()

            for mem in memories:
                user_id = mem.get("user_id", "")
                memory_key = mem.get("memory_key", "")
                content = mem.get("content", "")
                confidence = mem.get("confidence", 0.8)

                existing = await self._repo.get_active_by_key(user_id, memory_key)
                source_ids = json_list(mem.get("source_event_ids", []))

                if existing is not None:
                    if _is_similar(existing, content, confidence, mem.get("importance", 0.5)):
                        result = await self._reinforce_memory(
                            existing,
                            source_group_id=group_id,
                            source_event_ids_json=source_ids,
                            created_by_span_id=created_by_span_id,
                        )
                    else:
                        result = await self._supersede_memory(
                            existing_memory=existing,
                            user_id=user_id,
                            memory_type=mem.get("memory_type", "fact"),
                            memory_key=memory_key,
                            content=content,
                            value_json=mem.get("value_json"),
                            importance=mem.get("importance", 0.5),
                            confidence=confidence,
                            valid_from=mem.get("valid_from"),
                            source_group_id=group_id,
                            source_event_ids_json=source_ids,
                            created_by_span_id=created_by_span_id,
                        )
                else:
                    result = await self._repo.insert({
                        "id": new_uuid(),
                        "user_id": user_id,
                        "memory_type": mem.get("memory_type", "fact"),
                        "memory_key": memory_key,
                        "content": content,
                        "value_json": json_obj(mem.get("value_json", {})),
                        "importance": mem.get("importance", 0.5),
                        "confidence": confidence,
                        "source_group_id": group_id,
                        "source_event_ids_json": source_ids,
                        "created_by_span_id": created_by_span_id,
                        "updated_by_span_id": created_by_span_id,
                    })
                written.append(result)

            # 更新事件状态（同一事务中）
            await self._event_service.complete_extraction(group_id)
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            await self._event_service.fail_extraction(
                group_id, "Extraction write failed",
            )
            raise

        return written

def _is_similar(
    existing: dict[str, Any],
    new_content: str,
    new_confidence: float,
    new_importance: float,
) -> bool:
    """判断新旧记忆是否含义相同。

    MVP 规则：
    1. 如果 value_json 不同 → 不相似（结构化数据优先）
    2. 如果内容完全相同 → 相似
    3. 如果内容不同但 value_json 相同或都为空 → 用内容关键词重叠率判断

    后续可以引入语义相似度模型。
    """
    old_content = existing.get("content", "")

    # 完全相同
    if old_content == new_content:
        return True

    # value_json 不同 → 不相似
    old_value = existing.get("value_json", "{}")
    if isinstance(old_value, str):
        try:
            old_value_obj = json.loads(old_value)
        except (json.JSONDecodeError, TypeError):
            old_value_obj = {}
    else:
        old_value_obj = old_value or {}

    # 如果调用时没有传 value_json，不作为判断依据
    # (这个函数由 upsert_memory 调用，但 new_* 参数来自调用者)

    # 关键词重叠率 — 提高阈值到 0.85
    old_words = set(_tokenize(old_content))
    new_words = set(_tokenize(new_content))

    if not old_words or not new_words:
        return False

    intersection = old_words & new_words
    overlap = len(intersection) / max(len(old_words), len(new_words))

    return overlap >= 0.85


def _tokenize(text: str) -> list[str]:
    """简易分词。"""
    import re

    # 分割成中文单字 + 英文单词
    tokens: list[str] = []
    for part in re.split(r"([一-鿿])", text):
        part = part.strip()
        if not part:
            continue
        if re.match(r"^[一-鿿]$", part):
            tokens.append(part)
        else:
            tokens.extend(part.lower().split())
    return tokens
