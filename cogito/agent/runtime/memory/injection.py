# cogito/agent/runtime/memory/injection.py
#
# MemoryInjector — reads memory markdown files and produces text blocks
# for injection into ContextAssemblyPhase as candidate blocks.
#
# Which files are injected:
#   AGENT.md          → "agent_self_cognition" block (priority 5)
#   MEMORY.md         → "user_long_term_memory" block (priority 35)
#   RECENT_CONTEXT.md → "recent_context" block (priority 45)
#
# Other files (HISTORY.md, PENDING.md) are NOT injected directly.
# HISTORY.md is used via grep retrieval, not full-text.
# PENDING.md is a buffer awaiting Optimizer, not ready for injection.

from __future__ import annotations

from cogito.agent.runtime.memory.files import MemoryFileManager


class MemoryInjector:
    """Reads memory files and returns text blocks for context injection.

    Args:
        files: MemoryFileManager for reading files.
    """

    def __init__(self, files: MemoryFileManager) -> None:
        self._files = files

    # ── Block builders ────────────────────────────────────────────────

    def build_agent_block(self) -> str | None:
        """Build agent self-cognition block (priority 5, highest among memory blocks).

        Returns rendered text or None if AGENT.md is empty.
        """
        content = self._files.read("AGENT.md").strip()
        if not content:
            return None
        return f"## 自我认知\n\n{content}"

    def build_memory_block(self) -> str | None:
        """Build user long-term memory block (priority 35).

        Returns rendered text or None if MEMORY.md is empty.
        """
        content = self._files.read("MEMORY.md").strip()
        if not content:
            return None
        return f"## 用户长期记忆\n\n{content}"

    def build_recent_context_block(self) -> str | None:
        """Build recent context block (priority 45).

        Returns rendered text or None if RECENT_CONTEXT.md is empty.
        """
        content = self._files.read("RECENT_CONTEXT.md").strip()
        if not content:
            return None
        return f"## 近期上下文\n\n{content}"

    # ── Convenience: get all blocks ───────────────────────────────────

    def build_all_blocks(self) -> list[tuple[str, str, int]]:
        """Return all non-empty memory blocks as (block_id, text, priority).

        Ordered by priority (lowest = highest importance).
        """
        blocks: list[tuple[str, str, int]] = []

        agent = self.build_agent_block()
        if agent:
            blocks.append(("memory:agent", agent, 5))

        memory = self.build_memory_block()
        if memory:
            blocks.append(("memory:user_memory", memory, 35))

        recent = self.build_recent_context_block()
        if recent:
            blocks.append(("memory:recent_context", recent, 45))

        return blocks
