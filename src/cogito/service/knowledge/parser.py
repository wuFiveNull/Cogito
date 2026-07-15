"""知识解析器 Port + 内置 Markdown 解析。

PLAN-13 M4 §11.4：parser/segmenter/embedding 都是可替换 Port。
首版内置 Markdown heading 切分（结构边界优先）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ParsedBlock:
    """解析后的文本块。"""

    kind: str  # paragraph | heading | code | list_item | table
    text: str
    heading_path: str = ""
    start_offset: int = 0
    end_offset: int = 0


class ContentParser(Protocol):
    """内容解析器 Port（PLAN-13 M4）。"""

    @property
    def parser_id(self) -> str: ...
    @property
    def parser_version(self) -> str: ...

    def parse(self, raw_text: str) -> list[ParsedBlock]: ...


class MarkdownParser:
    """Markdown 结构解析器：优先按 heading 切分。"""

    @property
    def parser_id(self) -> str:
        return "markdown"

    @property
    def parser_version(self) -> str:
        return "1"

    def parse(self, raw_text: str) -> list[ParsedBlock]:
        """按 heading/段落/代码块切分 Markdown。"""
        blocks: list[ParsedBlock] = []
        if not raw_text.strip():
            return blocks
        lines = raw_text.split("\n")
        current_heading_stack: list[tuple[int, str]] = []  # (level, text)
        buf: list[str] = []
        block_start = 0
        offset = 0

        def _flush(end_offset: int) -> None:
            if buf:
                text = "\n".join(buf).strip()
                if text:
                    path = " > ".join(h[1] for h in current_heading_stack)
                    blocks.append(
                        ParsedBlock(
                            kind="paragraph",
                            text=text,
                            heading_path=path,
                            start_offset=block_start,
                            end_offset=end_offset,
                        )
                    )
                buf.clear()

        for line in lines:
            heading = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
            if heading:
                _flush(offset)
                level = len(heading.group(1))
                title = heading.group(2).strip()
                # 维护 heading 栈
                current_heading_stack = [h for h in current_heading_stack if h[0] < level]
                current_heading_stack.append((level, title))
                blocks.append(
                    ParsedBlock(
                        kind="heading",
                        text=title,
                        heading_path=title,
                        start_offset=offset,
                        end_offset=offset + len(line),
                    )
                )
            else:
                buf.append(line)
            offset += len(line) + 1  # +1 for newline

        _flush(offset)
        return blocks


class PlainTextParser:
    """纯文本解析器：按段落切分。"""

    @property
    def parser_id(self) -> str:
        return "plain_text"

    @property
    def parser_version(self) -> str:
        return "1"

    def parse(self, raw_text: str) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        for para in re.split(r"\n\s*\n", raw_text.strip()):
            para = para.strip()
            if para:
                blocks.append(ParsedBlock(kind="paragraph", text=para))
        return blocks
