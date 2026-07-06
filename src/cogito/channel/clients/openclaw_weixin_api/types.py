"""Type definitions for the OpenClaw WeChat API, mirroring the upstream protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SESSION_EXPIRED_ERRCODE = -14


class ApiError(Exception):
    """Structured error raised by the OpenClaw WeChat API."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: int | None = None,
        payload: Any = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload

    @property
    def is_session_expired(self) -> bool:
        return self.code == SESSION_EXPIRED_ERRCODE


@dataclass
class CDNMedia:
    encrypt_query_param: str | None = None
    aes_key: str | None = None
    encrypt_type: int | None = None


@dataclass
class TextItem:
    text: str | None = None


@dataclass
class ImageItem:
    media: CDNMedia | None = None
    thumb_media: CDNMedia | None = None
    aeskey: str | None = None
    url: str | None = None
    mid_size: int | None = None
    thumb_size: int | None = None
    thumb_height: int | None = None
    thumb_width: int | None = None
    hd_size: int | None = None
    _downloaded_bytes: bytes | None = field(default=None, repr=False)


@dataclass
class VoiceItem:
    media: CDNMedia | None = None
    encode_type: int | None = None
    bits_per_sample: int | None = None
    sample_rate: int | None = None
    playtime: int | None = None
    text: str | None = None
    _downloaded_bytes: bytes | None = field(default=None, repr=False)


@dataclass
class FileItem:
    media: CDNMedia | None = None
    file_name: str | None = None
    md5: str | None = None
    len: str | None = None
    _downloaded_bytes: bytes | None = field(default=None, repr=False)


@dataclass
class VideoItem:
    media: CDNMedia | None = None
    video_size: int | None = None
    play_length: int | None = None
    video_md5: str | None = None
    thumb_media: CDNMedia | None = None
    thumb_size: int | None = None
    thumb_height: int | None = None
    thumb_width: int | None = None
    _downloaded_bytes: bytes | None = field(default=None, repr=False)


@dataclass
class RefMessage:
    message_item: MessageItem | None = None
    title: str | None = None


@dataclass
class MessageItem:
    """A single content item inside a WeixinMessage."""

    # Item types
    NONE = 0
    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5

    type: int | None = None
    create_time_ms: int | None = None
    update_time_ms: int | None = None
    is_completed: bool | None = None
    msg_id: str | None = None
    ref_msg: RefMessage | None = None
    text_item: TextItem | None = None
    image_item: ImageItem | None = None
    voice_item: VoiceItem | None = None
    file_item: FileItem | None = None
    video_item: VideoItem | None = None


@dataclass
class WeixinMessage:
    """Unified message from getUpdates or for sendMessage."""

    # Message types
    TYPE_USER = 1
    TYPE_BOT = 2

    # Message states
    STATE_NEW = 0
    STATE_GENERATING = 1
    STATE_FINISH = 2

    seq: int | None = None
    message_id: int | None = None
    from_user_id: str | None = None
    to_user_id: str | None = None
    client_id: str | None = None
    create_time_ms: int | None = None
    update_time_ms: int | None = None
    delete_time_ms: int | None = None
    session_id: str | None = None
    group_id: str | None = None
    message_type: int | None = None
    message_state: int | None = None
    item_list: list[MessageItem] | None = None
    context_token: str | None = None


@dataclass
class GetUpdatesResponse:
    ret: int | None = None
    errcode: int | None = None
    errmsg: str | None = None
    msgs: list[WeixinMessage] = field(default_factory=list)
    get_updates_buf: str | None = None
    longpolling_timeout_ms: int | None = None


@dataclass
class GetConfigResponse:
    ret: int | None = None
    errmsg: str | None = None
    typing_ticket: str | None = None


@dataclass
class GetUploadUrlResponse:
    upload_param: str | None = None
    thumb_upload_param: str | None = None


@dataclass
class QRCodeResponse:
    """Response from get_bot_qrcode endpoint."""

    qrcode: str | None = None
    qrcode_img_content: str | None = None


@dataclass
class QRStatusResponse:
    """Response from get_qrcode_status endpoint."""

    status: str | None = None  # "wait" | "scaned" | "confirmed" | "expired"
    bot_token: str | None = None
    ilink_bot_id: str | None = None
    baseurl: str | None = None
    ilink_user_id: str | None = None


@dataclass
class LoginResult:
    """Result returned by the login flow."""

    token: str
    base_url: str
    account_id: str
    qr_image_base64: str | None = None  # data URI of the last QR code shown
