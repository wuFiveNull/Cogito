"""合成去敏数据集（PLAN-13 P13-15 §16.2）。

固定 seed，覆盖 §15.1 的 15 类语料核心项。
无真实 PII / Secret / 私聊全文。
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Doc:
    doc_id: str
    content: str
    expected_queries: list[str]  # 应召回此 doc 的 query


def generate_dataset(seed: int = 42) -> list[Doc]:
    """生成固定 seed 的合成数据集。"""
    rng = random.Random(seed)
    docs: list[Doc] = []

    # ── 1. 中文多轮对话（跨 Session 召回 #1，不强化 #15）──
    conversations = [
        ("用户偏好使用 Python 进行数据分析，常用 pandas 和 numpy。",
         ["Python 数据分析", "pandas 用户"]),
        ("用户在北京工作，负责推荐系统方向。",
         ["北京 推荐系统", "工作地点"]),
        ("用户喜欢低压力、能长期坚持的创作方式。",
         ["创作方式", "低压力工作"]),
        ("用户明确要求记住：项目截止日期是 2026-08-15。",
         ["截止日期", "2026-08-15"]),
        ("用户不喜欢在周末讨论工作。",
         ["周末 工作", "偏好"]),
    ]
    for i, (content, queries) in enumerate(conversations):
        docs.append(Doc(doc_id=f"conv_{i}", content=content, expected_queries=queries))

    # ── 2. Markdown 项目文档（段落召回 #9）──
    docs.append(Doc(
        doc_id="doc_arch",
        content=(
            "# 系统架构\n\n"
            "本系统采用分层架构：interaction-web → agent-api ↔ agent-worker → sqlite。\n\n"
            "## 记忆层\n\n"
            "记忆分为短期上下文（Session/Message）和长期认知（MemoryItem）。\n\n"
            "## 检索\n\n"
            "检索使用 FTS5 全文索引 + LIKE 降级，支持中文。"
        ),
        expected_queries=["分层架构", "记忆层", "FTS5 检索"],
    ))
    docs.append(Doc(
        doc_id="doc_deploy",
        content=(
            "# 部署手册\n\n"
            "使用 conda 激活 cogito 环境。\n\n"
            "## 配置\n\n"
            "API key 存放在 config.toml，不进入版本控制。"
        ),
        expected_queries=["部署", "conda 激活", "config.toml"],
    ))

    # ── 3. Python 代码片段（代码符号召回 #10）──
    docs.append(Doc(
        doc_id="code_extract",
        content=(
            "def compute_retrieval_weight(importance, source_trust, decay_rate, days):\n"
            "    base = importance * 0.6 + source_trust * 0.2\n"
            "    decay = math.exp(-decay_rate * days)\n"
            "    return base * decay\n"
        ),
        expected_queries=["compute_retrieval_weight", "衰减函数", "importance"],
    ))
    docs.append(Doc(
        doc_id="code_memory",
        content=(
            "class MemoryItem:\n"
            "    def __init__(self, kind: str, subject: str, value: str):\n"
            "        self.kind = kind\n"
            "        self.subject = subject\n"
            "        self.value = value\n"
        ),
        expected_queries=["MemoryItem", "class 定义", "subject"],
    ))

    # ── 4. 长日志（精确检索 #7）──
    for i in range(3):
        docs.append(Doc(
            doc_id=f"log_{i}",
            content=(
                f"[{20260711 + i}] INFO user_action login user_id=U{i:04d} "
                f"ip=192.168.1.{i} result=success "
                f"session=ses_{rng.randint(1000,9999)}"
            ),
            expected_queries=[f"U{i:04d}", f"20260711 + {i}", "login"],
        ))

    # ── 5. 矛盾偏好 + 删除场景（#3 矛盾、#11/#12 失效）──
    docs.append(Doc(
        doc_id="pref_theme_v1",
        content="用户偏好深色主题（dark mode）。",
        expected_queries=["主题", "dark mode", "深色"],
    ))
    docs.append(Doc(
        doc_id="pref_theme_v2",
        content="用户更新偏好：现在喜欢浅色主题（light mode）。",
        expected_queries=["主题", "light mode", "浅色"],
    ))
    docs.append(Doc(
        doc_id="delete_me",
        content="这条信息已被用户删除，不应再被召回。",
        expected_queries=["删除", "信息"],
    ))

    rng.shuffle(docs)
    return docs
