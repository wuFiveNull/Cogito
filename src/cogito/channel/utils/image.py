"""Image utilities — 从 LangBot 复制。

提供图片下载和格式转换功能，用于 Channel 适配器。

Source: langbot/pkg/utils/image.py
"""
from __future__ import annotations

import base64

import httpx


async def get_qq_official_image_base64(pic_url: str, content_type: str) -> str:
    """下载 QQ 官方图片并转换为 data URL 格式。"""
    async with httpx.AsyncClient() as client:
        response = await client.get(pic_url)
        response.raise_for_status()
        image_data = response.content
        b64_data = base64.b64encode(image_data).decode("utf-8")
        return f"data:{content_type};base64,{b64_data}"
