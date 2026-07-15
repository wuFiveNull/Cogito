"""FakeRssServer —— 可控的本地 RSS 服务，用于 E2E 测试。

- 动态生成 RSS 2.0 XML
- 支持 ETag / Last-Modified 条件请求（返回 304）
- 故障注入：超时、HTTP 错误、连接断开
- 请求日志（含请求头断言）
- 动态追加条目
"""

from __future__ import annotations

import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


_RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>{title}</title>
<link>{site_link}</link>
<description>Fake RSS feed for testing</description>
{items}
</channel>
</rss>
"""

_ITEM_TEMPLATE = """<item>
<title>{title}</title>
<link>{link}</link>
<guid>{guid}</guid>
<description>{description}</description>
<pubDate>{pub_date}</pubDate>
</item>
"""


class FakeRssServer:
    """可控的本地 RSS HTTP 服务。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._entries: list[dict[str, Any]] = []
        self._request_log: list[dict[str, Any]] = []
        self._feed_title = "Fake Feed"
        self._site_link = "http://example.com"

        # 故障注入
        self._next_status: int | None = None
        self._next_timeout_s: float = 0
        self._drop_connection = False
        self._error_rate: float = 0.0  # 暂未用，保留

        # ETag/Last-Modified 控制
        self._etag_counter = 0
        self._force_304 = False

    # ── 控制接口 ──

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("Server not started")
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}/feed.xml"

    @property
    def request_log(self) -> list[dict[str, Any]]:
        return list(self._request_log)

    @property
    def etag_values(self) -> list[str]:
        """返回所有请求收到的 ETag 值（If-None-Match）。"""
        return [
            r["headers"].get("if-none-match", "")
            for r in self._request_log
            if "if-none-match" in r["headers"]
        ]

    def set_entries(self, entries: list[dict[str, Any]]) -> None:
        """设置完整条目列表。entry: {title, link, description, guid?, pub_date?}"""
        self._entries = list(entries)
        self._etag_counter += 1

    def add_entry(self, title: str, description: str = "", link: str = "") -> str:
        """追加单条，返回 guid。"""
        guid = link or f"urn:uuid:{uuid.uuid4().hex}"
        self._entries.append(
            {
                "title": title,
                "link": link or f"http://example.com/p/{uuid.uuid4().hex[:8]}",
                "description": description,
                "guid": guid,
                "pub_date": None,
            }
        )
        self._etag_counter += 1
        return guid

    def set_next_status(self, status: int) -> None:
        self._next_status = status

    def set_next_timeout(self, seconds: float) -> None:
        self._next_timeout_s = seconds

    def set_drop_connection(self, drop: bool = True) -> None:
        self._drop_connection = drop

    def reset(self) -> None:
        self._request_log.clear()
        self._next_status = None
        self._next_timeout_s = 0
        self._drop_connection = False

    # ── 生命周期 ──

    def start(self) -> None:
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        if self._port == 0:
            self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _make_handler(self):
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # 静默

            def do_GET(self):
                server_self._handle_get(self)

        return Handler

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        # 记录请求
        headers_lower = {k.lower(): v for k, v in handler.headers.items()}
        record = {
            "path": handler.path,
            "headers": headers_lower,
            "time": time.time(),
        }
        self._request_log.append(record)

        # 故障注入
        if self._next_timeout_s > 0:
            time.sleep(self._next_timeout_s)
            self._next_timeout_s = 0

        if self._drop_connection:
            self._drop_connection = False
            handler.wfile.close()
            return

        # 条件请求：If-None-Match / If-Modified-Since
        if_none_match = headers_lower.get("if-none-match")
        if if_none_match and if_none_match == self._current_etag:
            handler.send_response(304)
            handler.send_header("ETag", self._current_etag)
            handler.end_headers()
            return

        # 强制错误状态
        if self._next_status is not None:
            status = self._next_status
            self._next_status = None
            handler.send_response(status)
            handler.send_header("Content-Type", "text/plain")
            handler.end_headers()
            handler.wfile.write(f"error {status}".encode())
            return

        # 正常响应
        body = self._render_feed().encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("ETag", self._current_etag)
        handler.send_header("Last-Modified", "Mon, 07 Jul 2026 12:00:00 GMT")
        handler.end_headers()
        handler.wfile.write(body)

    @property
    def _current_etag(self) -> str:
        return f'W/"{self._etag_counter:08x}-{len(self._entries)}"'

    def _render_feed(self) -> str:
        import email.utils

        items_xml = ""
        for e in self._entries:
            pub = e.get("pub_date") or email.utils.formatdate(usegmt=True)
            items_xml += _ITEM_TEMPLATE.format(
                title=_xml_escape(e.get("title", "")),
                link=_xml_escape(e.get("link", "")),
                guid=_xml_escape(e.get("guid", "")),
                description=_xml_escape(e.get("description", "")),
                pub_date=pub,
            )
        return _RSS_TEMPLATE.format(
            title=_xml_escape(self._feed_title),
            site_link=_xml_escape(self._site_link),
            items=items_xml,
        )


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# ── pytest fixture ──

import pytest


@pytest.fixture
def fake_rss_server():
    """提供一个已启动的 FakeRssServer，测试结束后自动停止。"""
    server = FakeRssServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()
