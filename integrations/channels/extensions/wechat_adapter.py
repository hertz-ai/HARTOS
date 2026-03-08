"""
WeChat Channel Adapter

Implements WeChat messaging via Official Account API and Mini-Programs.
Based on HevolveBot extension patterns for WeChat.

Features:
- Official Account API (Service Account / Subscription Account)
- Mini-Programs support
- Template messages
- Custom menus
- QR code generation
- Media upload/download
- Customer service messages
- Message encryption/decryption
- Event handling (subscribe, unsubscribe, scan, location, click)
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import time
import hashlib
import base64
import struct
import socket
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
from xml.etree import ElementTree
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

from ..base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
    ChannelConnectionError,
    ChannelSendError,
    ChannelRateLimitError,
)

logger = logging.getLogger(__name__)


# WeChat API endpoints
WECHAT_API_BASE = "https://api.weixin.qq.com/cgi-bin"
WECHAT_API_SEND = f"{WECHAT_API_BASE}/message/custom/send"
WECHAT_API_TEMPLATE = f"{WECHAT_API_BASE}/message/template/send"
WECHAT_API_MEDIA_UPLOAD = f"{WECHAT_API_BASE}/media/upload"
WECHAT_API_MEDIA_GET = f"{WECHAT_API_BASE}/media/get"
WECHAT_API_TOKEN = f"{WECHAT_API_BASE}/token"
WECHAT_API_MENU = f"{WECHAT_API_BASE}/menu/create"
WECHAT_API_QR = f"{WECHAT_API_BASE}/qrcode/create"
WECHAT_API_USER_INFO = f"{WECHAT_API_BASE}/user/info"

# Mini-Program API endpoints
WECHAT_MP_API_BASE = "https://api.weixin.qq.com/wxa"
WECHAT_MP_CODE = f"{WECHAT_MP_API_BASE}/getwxacode"
WECHAT_MP_MSG_SEND = f"{WECHAT_MP_API_BASE}/msg_sec_check"


@dataclass
class WeChatConfig(ChannelConfig):
    """WeChat-specific configuration."""
    app_id: str = ""
    app_secret: str = ""
    encoding_aes_key: Optional[str] = None  # For message encryption
    token: str = ""  # Verification token
    account_type: str = "service"  # service, subscription, mini_program
    enable_encryption: bool = False
    enable_mini_program: bool = False
    mini_program_app_id: Optional[str] = None
    mini_program_secret: Optional[str] = None
    template_ids: Dict[str, str] = field(default_factory=dict)


@dataclass
class WeChatUser:
    """WeChat user information."""
    openid: str
    unionid: Optional[str] = None
    nickname: Optional[str] = None
    sex: int = 0  # 0: unknown, 1: male, 2: female
    city: Optional[str] = None
    province: Optional[str] = None
    country: Optional[str] = None
    headimgurl: Optional[str] = None
    subscribe: bool = True
    subscribe_time: Optional[int] = None
    language: str = "zh_CN"


@dataclass
class TemplateMessage:
    """Template message builder."""
    template_id: str
    touser: str
    url: Optional[str] = None
    miniprogram: Optional[Dict[str, str]] = None
    data: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def add_field(self, key: str, value: str, color: str = "#173177") -> 'TemplateMessage':
        """Add a data field to the template."""
        self.data[key] = {"value": value, "color": color}
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API request format."""
        result = {
            "touser": self.touser,
            "template_id": self.template_id,
            "data": self.data,
        }
        if self.url:
            result["url"] = self.url
        if self.miniprogram:
            result["miniprogram"] = self.miniprogram
        return result


class WeChatMessageCrypto:
    """
    WeChat message encryption/decryption handler.

    Implements AES-256-CBC encryption as specified by WeChat.
    """

    def __init__(self, app_id: str, encoding_aes_key: str, token: str):
        if not HAS_CRYPTO:
            raise ImportError("pycryptodome required for encryption. Install with: pip install pycryptodome")

        self.app_id = app_id
        self.token = token
        # Decode the encoding key (43 chars Base64 -> 32 bytes)
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    def _pad(self, data: bytes) -> bytes:
        """PKCS#7 padding."""
        block_size = 32
        padding_len = block_size - (len(data) % block_size)
        return data + bytes([padding_len] * padding_len)

    def _unpad(self, data: bytes) -> bytes:
        """Remove PKCS#7 padding."""
        padding_len = data[-1]
        return data[:-padding_len]

    def encrypt(self, message: str) -> str:
        """Encrypt a message."""
        # Generate random 16-byte string
        random_str = os.urandom(16)

        # Build plaintext: random(16) + msg_len(4) + msg + app_id
        msg_bytes = message.encode('utf-8')
        msg_len = struct.pack('>I', len(msg_bytes))
        app_id_bytes = self.app_id.encode('utf-8')
        plaintext = random_str + msg_len + msg_bytes + app_id_bytes

        # Encrypt
        iv = self.aes_key[:16]
        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(self._pad(plaintext))

        return base64.b64encode(ciphertext).decode('utf-8')

    def decrypt(self, encrypted: str) -> str:
        """Decrypt a message."""
        ciphertext = base64.b64decode(encrypted)

        # Decrypt
        iv = self.aes_key[:16]
        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv)
        plaintext = self._unpad(cipher.decrypt(ciphertext))

        # Extract message
        # Skip random(16), read msg_len(4), extract msg
        msg_len = struct.unpack('>I', plaintext[16:20])[0]
        message = plaintext[20:20 + msg_len].decode('utf-8')

        return message

    def verify_signature(self, signature: str, timestamp: str, nonce: str, encrypt: str = "") -> bool:
        """Verify message signature."""
        parts = sorted([self.token, timestamp, nonce, encrypt])
        sign_str = ''.join(parts)
        computed = hashlib.sha1(sign_str.encode('utf-8')).hexdigest()
        return computed == signature


class WeChatAdapter(ChannelAdapter):
    """
    WeChat messaging adapter for Official Accounts and Mini-Programs.

    Usage:
        config = WeChatConfig(
            app_id="your-app-id",
            app_secret="your-app-secret",
            token="your-verification-token",
        )
        adapter = WeChatAdapter(config)
        adapter.on_message(my_handler)
        # Use with webhook endpoint for receiving messages
    """

    def __init__(self, config: WeChatConfig):
        super().__init__(config)
        self.wechat_config: WeChatConfig = config
        self._access_token: Optional[str] = None
        self._token_expires_at: int = 0
        self._crypto: Optional[WeChatMessageCrypto] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._event_handlers: Dict[str, Callable] = {}
        self._user_cache: Dict[str, WeChatUser] = {}

    @property
    def name(self) -> str:
        return "wechat"

    async def connect(self) -> bool:
        """Initialize WeChat API connection."""
        if not self.wechat_config.app_id or not self.wechat_config.app_secret:
            logger.error("WeChat app ID and app secret required")
            return False

        try:
            # Create HTTP session
            self._session = aiohttp.ClientSession()

            # Get initial access token
            token_obtained = await self._refresh_access_token()
            if not token_obtained:
                logger.error("Failed to obtain WeChat access token")
                return False

            # Setup encryption if enabled
            if self.wechat_config.enable_encryption:
                if not self.wechat_config.encoding_aes_key:
                    logger.error("Encoding AES key required for encryption")
                    return False

                self._crypto = WeChatMessageCrypto(
                    self.wechat_config.app_id,
                    self.wechat_config.encoding_aes_key,
                    self.wechat_config.token,
                )

            self.status = ChannelStatus.CONNECTED
            logger.info(f"WeChat adapter connected for app: {self.wechat_config.app_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to WeChat: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect WeChat adapter."""
        if self._session:
            await self._session.close()
            self._session = None

        self._access_token = None
        self._crypto = None
        self.status = ChannelStatus.DISCONNECTED

    async def _refresh_access_token(self) -> bool:
        """Refresh the access token from WeChat API."""
        if not self._session:
            return False

        try:
            params = {
                "grant_type": "client_credential",
                "appid": self.wechat_config.app_id,
                "secret": self.wechat_config.app_secret,
            }

            async with self._session.get(WECHAT_API_TOKEN, params=params) as response:
                data = await response.json()

                if "access_token" in data:
                    self._access_token = data["access_token"]
                    expires_in = data.get("expires_in", 7200)
                    self._token_expires_at = int(time.time()) + expires_in - 300  # 5 min buffer
                    return True
                else:
                    logger.error(f"Failed to get access token: {data}")
                    return False

        except Exception as e:
            logger.error(f"Error refreshing access token: {e}")
            return False

    async def _ensure_token(self) -> bool:
        """Ensure we have a valid access token."""
        if not self._access_token or time.time() >= self._token_expires_at:
            return await self._refresh_access_token()
        return True

    def verify_webhook(self, signature: str, timestamp: str, nonce: str) -> str:
        """
        Verify webhook request from WeChat.
        Should be called from your webhook endpoint for GET requests.

        Returns echostr if valid, empty string if invalid.
        """
        if self._crypto:
            if self._crypto.verify_signature(signature, timestamp, nonce):
                return nonce
        else:
            # Basic verification without encryption
            parts = sorted([self.wechat_config.token, timestamp, nonce])
            sign_str = ''.join(parts)
            computed = hashlib.sha1(sign_str.encode('utf-8')).hexdigest()
            if computed == signature:
                return nonce

        return ""

    async def handle_webhook(
        self,
        body: str,
        signature: str,
        timestamp: str,
        nonce: str,
        msg_signature: Optional[str] = None,
    ) -> Optional[str]:
        """
        Handle incoming webhook POST request from WeChat.
        Returns response XML string.
        """
        try:
            # Decrypt if needed
            xml_content = body
            if self.wechat_config.enable_encryption and self._crypto and msg_signature:
                # Parse encrypted XML
                root = ElementTree.fromstring(body)
                encrypted = root.find('Encrypt').text

                # Verify signature
                if not self._crypto.verify_signature(msg_signature, timestamp, nonce, encrypted):
                    logger.error("Invalid message signature")
                    return None

                # Decrypt
                xml_content = self._crypto.decrypt(encrypted)

            # Parse XML message
            root = ElementTree.fromstring(xml_content)
            msg_type = root.find('MsgType').text

            # Handle different message types
            if msg_type == 'text':
                await self._handle_text_message(root)
            elif msg_type == 'image':
                await self._handle_image_message(root)
            elif msg_type == 'voice':
                await self._handle_voice_message(root)
            elif msg_type == 'video':
                await self._handle_video_message(root)
            elif msg_type == 'location':
                await self._handle_location_message(root)
            elif msg_type == 'event':
                await self._handle_event(root)

            # Return success (empty string means success)
            return "success"

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")
            return None

    async def _handle_text_message(self, root: ElementTree.Element) -> None:
        """Handle text message."""
        message = self._convert_message(root)
        await self._dispatch_message(message)

    async def _handle_image_message(self, root: ElementTree.Element) -> None:
        """Handle image message."""
        message = self._convert_message(root, MessageType.IMAGE)
        await self._dispatch_message(message)

    async def _handle_voice_message(self, root: ElementTree.Element) -> None:
        """Handle voice message."""
        message = self._convert_message(root, MessageType.VOICE)
        await self._dispatch_message(message)

    async def _handle_video_message(self, root: ElementTree.Element) -> None:
        """Handle video message."""
        message = self._convert_message(root, MessageType.VIDEO)
        await self._dispatch_message(message)

    async def _handle_location_message(self, root: ElementTree.Element) -> None:
        """Handle location message."""
        message = self._convert_message(root, MessageType.LOCATION)
        await self._dispatch_message(message)

    async def _handle_event(self, root: ElementTree.Element) -> None:
        """Handle event message."""
        event_type = root.find('Event').text.lower()
        openid = root.find('FromUserName').text

        # Check for registered event handler
        if event_type in self._event_handlers:
            handler = self._event_handlers[event_type]
            await handler(root)

        # Log events
        if event_type == 'subscribe':
            logger.info(f"User subscribed: {openid}")
        elif event_type == 'unsubscribe':
            logger.info(f"User unsubscribed: {openid}")
        elif event_type == 'scan':
            event_key = root.find('EventKey').text if root.find('EventKey') is not None else None
            logger.info(f"User scanned QR: {openid}, key: {event_key}")
        elif event_type == 'click':
            event_key = root.find('EventKey').text
            logger.info(f"Menu click: {openid}, key: {event_key}")

    def _convert_message(
        self,
        root: ElementTree.Element,
        media_type: Optional[MessageType] = None,
    ) -> Message:
        """Convert WeChat XML message to unified Message format."""
        openid = root.find('FromUserName').text
        msg_id = root.find('MsgId').text if root.find('MsgId') is not None else str(int(time.time() * 1000))
        create_time = int(root.find('CreateTime').text)

        text = ""
        media = []

        msg_type = root.find('MsgType').text

        if msg_type == 'text':
            text = root.find('Content').text
        elif msg_type == 'image':
            pic_url = root.find('PicUrl').text
            media_id = root.find('MediaId').text
            media.append(MediaAttachment(
                type=MessageType.IMAGE,
                url=pic_url,
                file_id=media_id,
            ))
        elif msg_type == 'voice':
            media_id = root.find('MediaId').text
            recognition = root.find('Recognition')
            if recognition is not None:
                text = recognition.text
            media.append(MediaAttachment(
                type=MessageType.VOICE,
                file_id=media_id,
            ))
        elif msg_type == 'video' or msg_type == 'shortvideo':
            media_id = root.find('MediaId').text
            thumb_media_id = root.find('ThumbMediaId').text
            media.append(MediaAttachment(
                type=MessageType.VIDEO,
                file_id=media_id,
            ))
        elif msg_type == 'location':
            lat = root.find('Location_X').text
            lon = root.find('Location_Y').text
            scale = root.find('Scale').text
            label = root.find('Label').text
            text = f"[location:{lat},{lon}] {label}"

        return Message(
            id=msg_id,
            channel=self.name,
            sender_id=openid,
            chat_id=openid,  # WeChat uses openid for both
            text=text,
            media=media,
            timestamp=datetime.fromtimestamp(create_time),
            is_group=False,  # Official account messages are 1:1
            raw={
                'msg_type': msg_type,
                'openid': openid,
            },
        )

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a customer service message to a user."""
        if not await self._ensure_token():
            return SendResult(success=False, error="Failed to get access token")

        try:
            # Build message based on content type
            if media and len(media) > 0:
                return await self._send_media_message(chat_id, media[0], text)

            # Send text message
            payload = {
                "touser": chat_id,
                "msgtype": "text",
                "text": {
                    "content": text
                }
            }

            url = f"{WECHAT_API_SEND}?access_token={self._access_token}"

            async with self._session.post(url, json=payload) as response:
                data = await response.json()

                if data.get("errcode", 0) == 0:
                    return SendResult(success=True)
                elif data.get("errcode") == 45015:
                    # User not interacting in 48 hours
                    return SendResult(success=False, error="User inactive for 48 hours")
                elif data.get("errcode") == 45047:
                    # Rate limited
                    raise ChannelRateLimitError(60)
                else:
                    return SendResult(success=False, error=data.get("errmsg", "Unknown error"))

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"Failed to send WeChat message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_media_message(
        self,
        chat_id: str,
        media: MediaAttachment,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Send a media message."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # Determine message type
            if media.type == MessageType.IMAGE:
                msgtype = "image"
            elif media.type == MessageType.VOICE:
                msgtype = "voice"
            elif media.type == MessageType.VIDEO:
                msgtype = "video"
            else:
                msgtype = "file"

            # Need media_id - upload if we have URL/file
            media_id = media.file_id

            if not media_id and (media.url or media.file_path):
                media_id = await self._upload_media(media)

            if not media_id:
                return SendResult(success=False, error="No media ID")

            payload = {
                "touser": chat_id,
                "msgtype": msgtype,
                msgtype: {
                    "media_id": media_id
                }
            }

            url = f"{WECHAT_API_SEND}?access_token={self._access_token}"

            async with self._session.post(url, json=payload) as response:
                data = await response.json()

                if data.get("errcode", 0) == 0:
                    return SendResult(success=True)
                else:
                    return SendResult(success=False, error=data.get("errmsg", "Unknown error"))

        except Exception as e:
            logger.error(f"Failed to send media message: {e}")
            return SendResult(success=False, error=str(e))

    async def _upload_media(self, media: MediaAttachment) -> Optional[str]:
        """Upload media to WeChat servers."""
        if not self._session or not await self._ensure_token():
            return None

        try:
            # Determine media type
            if media.type == MessageType.IMAGE:
                media_type = "image"
            elif media.type == MessageType.VOICE:
                media_type = "voice"
            elif media.type == MessageType.VIDEO:
                media_type = "video"
            else:
                media_type = "file"

            url = f"{WECHAT_API_MEDIA_UPLOAD}?access_token={self._access_token}&type={media_type}"

            # Get file data
            if media.file_path:
                with open(media.file_path, 'rb') as f:
                    file_data = f.read()
                filename = os.path.basename(media.file_path)
            elif media.url:
                async with self._session.get(media.url) as resp:
                    file_data = await resp.read()
                filename = media.file_name or "file"
            else:
                return None

            # Upload
            data = aiohttp.FormData()
            data.add_field(
                'media',
                file_data,
                filename=filename,
                content_type=media.mime_type or 'application/octet-stream'
            )

            async with self._session.post(url, data=data) as response:
                result = await response.json()
                return result.get("media_id")

        except Exception as e:
            logger.error(f"Failed to upload media: {e}")
            return None

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Edit a message.
        Note: WeChat doesn't support message editing, sends new message.
        """
        logger.warning("WeChat doesn't support message editing, sending new message")
        return await self.send_message(chat_id, text, buttons=buttons)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """
        Delete a message.
        Note: WeChat doesn't support message deletion.
        """
        logger.warning("WeChat doesn't support message deletion")
        return False

    async def send_typing(self, chat_id: str) -> None:
        """
        Send typing indicator.
        Note: WeChat doesn't have a typing indicator.
        """
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get user information by OpenID."""
        user = await self.get_user_info(chat_id)
        if user:
            return {
                'openid': user.openid,
                'unionid': user.unionid,
                'nickname': user.nickname,
                'avatar': user.headimgurl,
                'sex': user.sex,
                'city': user.city,
                'province': user.province,
                'country': user.country,
                'subscribed': user.subscribe,
            }
        return None

    # WeChat-specific methods

    def register_event_handler(
        self,
        event_type: str,
        handler: Callable[[ElementTree.Element], Any],
    ) -> None:
        """Register a handler for WeChat events."""
        self._event_handlers[event_type.lower()] = handler

    async def get_user_info(self, openid: str) -> Optional[WeChatUser]:
        """Get user profile information."""
        if not await self._ensure_token():
            return None

        # Check cache
        if openid in self._user_cache:
            return self._user_cache[openid]

        try:
            url = f"{WECHAT_API_USER_INFO}?access_token={self._access_token}&openid={openid}&lang=zh_CN"

            async with self._session.get(url) as response:
                data = await response.json()

                if data.get("errcode"):
                    logger.error(f"Failed to get user info: {data}")
                    return None

                user = WeChatUser(
                    openid=data.get("openid"),
                    unionid=data.get("unionid"),
                    nickname=data.get("nickname"),
                    sex=data.get("sex", 0),
                    city=data.get("city"),
                    province=data.get("province"),
                    country=data.get("country"),
                    headimgurl=data.get("headimgurl"),
                    subscribe=data.get("subscribe") == 1,
                    subscribe_time=data.get("subscribe_time"),
                    language=data.get("language", "zh_CN"),
                )

                self._user_cache[openid] = user
                return user

        except Exception as e:
            logger.error(f"Error getting user info: {e}")
            return None

    async def send_template_message(
        self,
        template: TemplateMessage,
    ) -> SendResult:
        """Send a template message."""
        if not await self._ensure_token():
            return SendResult(success=False, error="Failed to get access token")

        try:
            url = f"{WECHAT_API_TEMPLATE}?access_token={self._access_token}"

            async with self._session.post(url, json=template.to_dict()) as response:
                data = await response.json()

                if data.get("errcode", 0) == 0:
                    return SendResult(
                        success=True,
                        message_id=str(data.get("msgid")),
                    )
                else:
                    return SendResult(
                        success=False,
                        error=data.get("errmsg", "Unknown error"),
                    )

        except Exception as e:
            logger.error(f"Failed to send template message: {e}")
            return SendResult(success=False, error=str(e))

    async def create_menu(self, menu: Dict[str, Any]) -> bool:
        """Create custom menu for Official Account."""
        if not await self._ensure_token():
            return False

        try:
            url = f"{WECHAT_API_MENU}?access_token={self._access_token}"

            async with self._session.post(url, json=menu) as response:
                data = await response.json()
                return data.get("errcode", 0) == 0

        except Exception as e:
            logger.error(f"Failed to create menu: {e}")
            return False

    async def create_qr_code(
        self,
        scene: str,
        permanent: bool = False,
        expire_seconds: int = 2592000,
    ) -> Optional[str]:
        """
        Create a QR code for a scene.

        Returns the URL to get the QR code image.
        """
        if not await self._ensure_token():
            return None

        try:
            url = f"{WECHAT_API_QR}?access_token={self._access_token}"

            if permanent:
                payload = {
                    "action_name": "QR_LIMIT_STR_SCENE",
                    "action_info": {
                        "scene": {"scene_str": scene}
                    }
                }
            else:
                payload = {
                    "expire_seconds": expire_seconds,
                    "action_name": "QR_STR_SCENE",
                    "action_info": {
                        "scene": {"scene_str": scene}
                    }
                }

            async with self._session.post(url, json=payload) as response:
                data = await response.json()

                if "ticket" in data:
                    ticket = data["ticket"]
                    return f"https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket={ticket}"

            return None

        except Exception as e:
            logger.error(f"Failed to create QR code: {e}")
            return None

    async def get_media_content(self, media_id: str) -> Optional[bytes]:
        """Download media content by media ID."""
        if not await self._ensure_token():
            return None

        try:
            url = f"{WECHAT_API_MEDIA_GET}?access_token={self._access_token}&media_id={media_id}"

            async with self._session.get(url) as response:
                if response.content_type.startswith('application/json'):
                    # Error response
                    data = await response.json()
                    logger.error(f"Failed to get media: {data}")
                    return None

                return await response.read()

        except Exception as e:
            logger.error(f"Error getting media content: {e}")
            return None

    # Mini-Program methods

    async def get_mini_program_qr_code(
        self,
        path: str,
        width: int = 430,
    ) -> Optional[bytes]:
        """Generate Mini-Program QR code."""
        if not self.wechat_config.enable_mini_program:
            return None

        if not await self._ensure_token():
            return None

        try:
            url = f"{WECHAT_MP_CODE}?access_token={self._access_token}"

            payload = {
                "path": path,
                "width": width,
            }

            async with self._session.post(url, json=payload) as response:
                if response.content_type.startswith('image'):
                    return await response.read()

                data = await response.json()
                logger.error(f"Failed to get mini program QR: {data}")
                return None

        except Exception as e:
            logger.error(f"Error getting mini program QR: {e}")
            return None


def create_wechat_adapter(
    app_id: str = None,
    app_secret: str = None,
    token: str = None,
    **kwargs
) -> WeChatAdapter:
    """
    Factory function to create WeChat adapter.

    Args:
        app_id: WeChat app ID (or set WECHAT_APP_ID env var)
        app_secret: WeChat app secret (or set WECHAT_APP_SECRET env var)
        token: Verification token (or set WECHAT_TOKEN env var)
        **kwargs: Additional config options

    Returns:
        Configured WeChatAdapter
    """
    app_id = app_id or os.getenv("WECHAT_APP_ID")
    app_secret = app_secret or os.getenv("WECHAT_APP_SECRET")
    token = token or os.getenv("WECHAT_TOKEN")

    if not app_id:
        raise ValueError("WeChat app ID required")
    if not app_secret:
        raise ValueError("WeChat app secret required")

    config = WeChatConfig(
        app_id=app_id,
        app_secret=app_secret,
        token=token or "",
        **kwargs
    )
    return WeChatAdapter(config)
