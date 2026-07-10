"""QQOneBotAdapter —— Core ChannelAdapter Facade，包装 LangBot aiocqhttp Adapter。

QQ-ONEBOT-E2E-01 / PR 3:
- 持有 AiocqhttpAdapter（复用 LangBot/OneBot 协议实现）
- 持有 LangBotLoggerAdapter
- LangBot Event → canonical Inbound（通过 onebot_models）
- Core send request → LangBot MessageChain → aiocqhttp
- 管理 status/readiness/task cancellation

它不读取 SQLite，不调用 AgentRunner，不处理 Memory，不直接更新 Delivery。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from cogito.channel.adapters.aiocqhttp import AiocqhttpAdapter
from cogito.channel.base import (
    AdapterStatus,
    ChannelCapabilities,
    ChannelSendRequest,
    ChannelSendResult,
)
from cogito.channel.drivers.onebot_models import (
    OneBotPolicy,
    friend_event_to_inbound,
    group_event_to_inbound,
)
from cogito.channel.vendor.langbot.compatibility import events as lb_events
from cogito.channel.vendor.langbot.compatibility import message as lb_message
from cogito.channel.vendor.langbot.compatibility.logger import EventLogger
from cogito.config import QQOneBotConfig
from cogito.contracts.inbound import InboundHandler

_LOG = logging.getLogger("cogito.channel.qq_onebot")


class QQOneBotAdapter:
    """Core ChannelAdapter Facade for QQ OneBot 11。

    包装 LangBot aiocqhttp Adapter，实现 Core ChannelAdapter Protocol。
    """

    def __init__(self, config: QQOneBotConfig) -> None:
        self._config = config
        self._policy = OneBotPolicy(
            owner_qq_ids=set(str(x) for x in config.owner_qq_ids),
            allow_private=config.allow_private,
            allowed_group_ids=set(str(x) for x in config.allowed_group_ids),
            require_mention_in_group=config.require_mention_in_group,
        )
        self._bot_ids: set[str] = set()
        self._inbound_handler: InboundHandler | None = None
        self._status = AdapterStatus.created
        self._ready = False
        self._task: asyncio.Task | None = None
        self._log = EventLogger(f"cogito.channel.qq.{config.instance_id}")

        # 创建 LangBot aiocqhttp Adapter
        # 使用配置副本，避免其删除 access-token 时修改 Core 配置
        adapter_config = {
            "host": config.host,
            "port": config.port,
            "access-token": config.access_token,
        }
        self._lb_logger = EventLogger(f"cogito.lb.qq.{config.instance_id}")
        self._adapter = AiocqhttpAdapter(
            config=adapter_config,
            logger=self._lb_logger,
        )

    # ── ChannelAdapter 协议实现 ──────────────────────────────────────────

    @property
    def adapter_id(self) -> str:
        return self._config.instance_id

    @property
    def channel_type(self) -> str:
        return "qq"

    @property
    def status(self) -> AdapterStatus:
        return self._status

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        """设置入站消息处理器。"""
        self._inbound_handler = handler

    async def start(self) -> None:
        """启动适配器 —— 注册 LangBot 监听器并启动 aiocqhttp server。"""
        self._status = AdapterStatus.starting
        try:
            # 注册 LangBot 监听器
            self._adapter.register_listener(
                lb_events.FriendMessage,
                self._handle_friend_message,
            )
            self._adapter.register_listener(
                lb_events.GroupMessage,
                self._handle_group_message,
            )

            # 启动 aiocqhttp server（后台任务）
            self._task = asyncio.create_task(
                self._run_adapter(),
                name=f"channel:qq:{self.adapter_id}",
            )
            self._status = AdapterStatus.running
            self._ready = True
            _LOG.info("QQ adapter %s started (host=%s port=%s)",
                      self.adapter_id, self._config.host, self._config.port)
        except Exception as e:
            self._status = AdapterStatus.error
            _LOG.error("QQ adapter %s failed to start: %s", self.adapter_id, e)
            raise

    async def stop(self) -> None:
        """停止适配器 —— 取消后台任务。"""
        self._ready = False
        self._status = AdapterStatus.stopped
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        # LangBot adapter kill() 当前返回 False（连接不会关闭），
        # 但取消 task 会停止 Quart server
        try:
            await self._adapter.kill()
        except Exception:
            pass
        _LOG.info("QQ adapter %s stopped", self.adapter_id)

    async def send(
        self,
        conversation_id: str,
        message: str,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """遗留 send 接口 —— 委托 send_request()。"""
        request = ChannelSendRequest(
            delivery_id="",
            attempt_id="",
            idempotency_key=f"legacy_{uuid.uuid4().hex[:8]}",
            channel_instance_id=self.adapter_id,
            target_endpoint_ref="",
            platform_conversation_id=conversation_id,
            reply_to_platform_message_id=reply_to_message_id,
            text=message,
        )
        result = await self.send_request(request)
        return {
            "status": result.status,
            "platform_message_id": result.platform_message_id,
            "error_code": result.error_code,
        }

    async def send_request(self, request: ChannelSendRequest) -> ChannelSendResult:
        """结构化发送 —— 将纯文本包装为 LangBot MessageChain 并发送。"""
        if self._status != AdapterStatus.running:
            return ChannelSendResult(
                status="temporary",
                error_code="adapter_not_running",
            )

        # 构建 MessageChain
        chain = lb_message.MessageChain([lb_message.Plain(text=request.text)])

        # 根据 target_endpoint_ref 或 platform_conversation_id 选择 person/group
        target_type, target_id = self._resolve_target(request)

        if not target_id:
            return ChannelSendResult(
                status="permanent",
                error_code="missing_target_id",
            )

        try:
            result = await self._adapter.send_message(
                target_type=target_type,
                target_id=target_id,
                message=chain,
            )
            # OneBot send_group_msg/send_private_msg 返回 { "message_id": int }
            if isinstance(result, dict) and "message_id" in result:
                return ChannelSendResult(
                    status="sent",
                    platform_message_id=str(result["message_id"]),
                )
            # 无返回或结构不明 → unknown
            return ChannelSendResult(
                status="unknown",
                error_code="no_response",
            )
        except ConnectionError:
            return ChannelSendResult(
                status="temporary",
                error_code="connection_error",
            )
        except TimeoutError:
            return ChannelSendResult(
                status="unknown",
                error_code="timeout",
            )
        except Exception as e:
            error_name = type(e).__name__
            # 认证错误 → permanent
            if "unauthorized" in str(e).lower() or "forbidden" in str(e).lower():
                return ChannelSendResult(
                    status="permanent",
                    error_code="auth_error",
                )
            _LOG.exception("QQ send failed: %s", e)
            return ChannelSendResult(
                status="unknown",
                error_code=error_name,
            )

    def capabilities(self) -> ChannelCapabilities:
        """返回 QQ OneBot 第一版能力声明。"""
        return ChannelCapabilities(
            supports_streaming=False,
            supports_edit=False,
            supports_buttons=False,
            supports_threads=False,
            supports_files=False,
            supports_delete=False,
            max_message_length=4000,
        )

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _resolve_target(self, request: ChannelSendRequest) -> tuple[str, str]:
        """根据 request 确定 target_type 和 target_id。"""
        # 优先从 target_endpoint_ref 解析
        ref = request.target_endpoint_ref
        if ref:
            parts = ref.split(":")
            # qq:qq-main:person:123456 或 qq:qq-main:group:123456
            if len(parts) >= 4:
                return parts[2], parts[3]

        # fallback: 从 platform_conversation_id 解析
        convo_id = request.platform_conversation_id
        if convo_id.startswith("private:"):
            return "person", convo_id.split(":", 1)[1]
        if convo_id.startswith("group:"):
            return "group", convo_id.split(":", 1)[1]

        return "", ""

    async def _run_adapter(self) -> None:
        """运行 aiocqhttp server 的后台任务。"""
        try:
            await self._adapter.run_async()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _LOG.error("QQ adapter %s run_async failed: %s", self.adapter_id, e)
            self._status = AdapterStatus.error

    async def _handle_friend_message(
        self,
        event: lb_events.FriendMessage,
        adapter: AiocqhttpAdapter,
    ) -> None:
        """处理私聊消息。"""
        # 更新 bot_account_id
        if not self._bot_ids and hasattr(adapter, "bot_account_id") and adapter.bot_account_id:
            self._bot_ids.add(str(adapter.bot_account_id))

        inbound, reason = friend_event_to_inbound(
            event,
            instance_id=self.adapter_id,
            policy=self._policy,
            bot_ids=self._bot_ids,
        )
        if inbound is None:
            _LOG.debug("Friend message filtered: %s", reason)
            return

        if self._inbound_handler is not None:
            await self._inbound_handler(inbound)

    async def _handle_group_message(
        self,
        event: lb_events.GroupMessage,
        adapter: AiocqhttpAdapter,
    ) -> None:
        """处理群聊消息。"""
        if not self._bot_ids and hasattr(adapter, "bot_account_id") and adapter.bot_account_id:
            self._bot_ids.add(str(adapter.bot_account_id))

        inbound, reason = group_event_to_inbound(
            event,
            instance_id=self.adapter_id,
            policy=self._policy,
            bot_ids=self._bot_ids,
        )
        if inbound is None:
            _LOG.debug("Group message filtered: %s", reason)
            return

        if self._inbound_handler is not None:
            await self._inbound_handler(inbound)

    # ── readiness ──────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._ready and self._status == AdapterStatus.running
