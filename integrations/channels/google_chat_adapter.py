"""
Google Chat Channel Adapter

Implements Google Chat messaging using webhooks and Chat API.
Designed for Docker-compatible deployments.

Features:
- Webhook-based integration (incoming)
- Chat API for outgoing messages
- Card messages with rich formatting
- Slash commands
- Thread support
- Spaces (rooms) support

Requirements:
- Google Cloud project with Chat API enabled
- Service account or OAuth credentials
- Webhook URL for incoming messages (optional)

Two modes:
1. Webhook-only: Receive messages via webhook, respond inline
2. Full API: Use Chat API for two-way communication
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

from .base import (
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


class GoogleChatAdapter(ChannelAdapter):
    """
    Google Chat adapter supporting webhooks and Chat API.

    Usage (Webhook mode):
        config = ChannelConfig(
            webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages?key=YYY&token=ZZZ"
        )
        adapter = GoogleChatAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()

    Usage (Full API mode):
        config = ChannelConfig(
            extra={
                "service_account_file": "/path/to/credentials.json",
                "scopes": ["https://www.googleapis.com/auth/chat.bot"],
            }
        )
        adapter = GoogleChatAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: ChannelConfig):
        if not HAS_AIOHTTP:
            raise ImportError(
                "aiohttp not installed. "
                "Install with: pip install aiohttp"
            )

        super().__init__(config)
        self._webhook_url = config.webhook_url
        self._session: Optional[aiohttp.ClientSession] = None
        self._chat_service = None
        self._bot_id: Optional[str] = None
        self._slash_commands: Dict[str, Callable] = {}
        self._use_api = config.extra.get("service_account_file") is not None

    @property
    def name(self) -> str:
        return "google_chat"

    async def connect(self) -> bool:
        """Connect to Google Chat."""
        try:
            self._session = aiohttp.ClientSession()

            # Initialize Google Chat API if credentials provided
            if self._use_api:
                if not HAS_GOOGLE:
                    raise ImportError(
                        "Google API client not installed. "
                        "Install with: pip install google-api-python-client google-auth"
                    )

                sa_file = self.config.extra.get("service_account_file")
                scopes = self.config.extra.get("scopes", [
                    "https://www.googleapis.com/auth/chat.bot",
                    "https://www.googleapis.com/auth/chat.messages",
                    "https://www.googleapis.com/auth/chat.messages.create",
                ])

                credentials = service_account.Credentials.from_service_account_file(
                    sa_file, scopes=scopes
                )

                self._chat_service = build("chat", "v1", credentials=credentials)
                logger.info("Connected to Google Chat API with service account")

            elif self._webhook_url:
                # Verify webhook URL is valid
                logger.info("Using webhook-only mode for Google Chat")

            else:
                logger.error("No webhook URL or service account provided")
                return False

            self.status = ChannelStatus.CONNECTED
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Google Chat: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Google Chat."""
        if self._session:
            await self._session.close()
            self._session = None

        self._chat_service = None
        self.status = ChannelStatus.DISCONNECTED
        logger.info("Disconnected from Google Chat")

    def register_slash_command(
        self,
        command: str,
        handler: Callable,
        description: str = "",
    ) -> None:
        """
        Register a slash command handler.

        Args:
            command: Command name (without /)
            handler: Async function to handle the command
            description: Command description
        """
        self._slash_commands[command] = {
            "handler": handler,
            "description": description,
        }

    async def handle_webhook(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Handle incoming webhook from Google Chat.

        This should be called from your webhook endpoint.

        Args:
            data: Webhook payload from Google Chat

        Returns:
            Response to send back (optional)
        """
        event_type = data.get("type", "")
        message_data = data.get("message", {})

        if event_type == "ADDED_TO_SPACE":
            # Bot added to space
            space = data.get("space", {})
            logger.info(f"Added to space: {space.get('displayName', space.get('name'))}")

            # Return welcome message
            return {
                "text": "Thanks for adding me! I'm ready to help."
            }

        elif event_type == "REMOVED_FROM_SPACE":
            # Bot removed from space
            space = data.get("space", {})
            logger.info(f"Removed from space: {space.get('displayName', space.get('name'))}")
            return None

        elif event_type == "MESSAGE":
            # Handle incoming message
            message = self._convert_message(data)
            if message:
                # Check for slash commands
                slash_command = message_data.get("slashCommand")
                if slash_command:
                    command_id = slash_command.get("commandId")
                    # Find command by ID (requires matching configuration)
                    for cmd_name, cmd_info in self._slash_commands.items():
                        if str(command_id) == cmd_name or cmd_name in message.text:
                            handler = cmd_info["handler"]
                            result = handler(message)
                            if asyncio.iscoroutine(result):
                                result = await result
                            if isinstance(result, dict):
                                return result
                            elif isinstance(result, str):
                                return {"text": result}

                # Dispatch to registered handlers
                await self._dispatch_message(message)

            return None

        elif event_type == "CARD_CLICKED":
            # Handle card button click
            action = data.get("action", {})
            action_name = action.get("actionMethodName", "")
            parameters = {
                p.get("key"): p.get("value")
                for p in action.get("parameters", [])
            }

            # Create message-like event
            message = Message(
                id=data.get("eventTime", str(datetime.now().timestamp())),
                channel=self.name,
                sender_id=data.get("user", {}).get("name", ""),
                sender_name=data.get("user", {}).get("displayName", ""),
                chat_id=data.get("space", {}).get("name", ""),
                text=f"[button:{action_name}]",
                raw={
                    "action": action_name,
                    "parameters": parameters,
                    "card_clicked": True,
                },
            )
            await self._dispatch_message(message)

            return None

        return None

    def _convert_message(self, data: Dict[str, Any]) -> Optional[Message]:
        """Convert Google Chat message to unified Message format."""
        message_data = data.get("message", {})

        if not message_data:
            return None

        sender = message_data.get("sender", {})
        space = data.get("space", {})
        thread = message_data.get("thread", {})

        # Determine if group/space or DM
        space_type = space.get("type", "")
        is_group = space_type in ("ROOM", "SPACE")

        # Process attachments
        media = []
        for attachment in message_data.get("attachment", []):
            att_data = attachment.get("attachmentDataRef", {})
            media.append(MediaAttachment(
                type=MessageType.DOCUMENT,
                file_id=att_data.get("resourceName"),
                file_name=attachment.get("contentName"),
                mime_type=attachment.get("contentType"),
            ))

        # Check for mentions
        annotations = message_data.get("annotations", [])
        is_mentioned = any(
            ann.get("type") == "USER_MENTION" and
            ann.get("userMention", {}).get("type") == "BOT"
            for ann in annotations
        )

        return Message(
            id=message_data.get("name", ""),
            channel=self.name,
            sender_id=sender.get("name", ""),
            sender_name=sender.get("displayName", ""),
            chat_id=space.get("name", ""),
            text=message_data.get("text", "") or message_data.get("argumentText", ""),
            media=media,
            reply_to_id=thread.get("name") if thread else None,
            timestamp=datetime.now(),  # Google Chat doesn't include timestamp in webhook
            is_group=is_group,
            is_bot_mentioned=is_mentioned,
            raw={
                "space": space,
                "thread": thread,
                "sender": sender,
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
        """Send a message to Google Chat."""
        try:
            # Build message payload
            message = {"text": text}

            # Add thread for reply
            if reply_to:
                message["thread"] = {"name": reply_to}

            # Add cards for buttons
            if buttons:
                message["cards"] = [self._build_card(text, buttons)]
                # Text is in card, don't duplicate
                del message["text"]

            # Use API if available
            if self._chat_service:
                return await self._send_via_api(chat_id, message, reply_to)
            elif self._webhook_url:
                return await self._send_via_webhook(message)
            else:
                return SendResult(success=False, error="No sending method configured")

        except Exception as e:
            logger.error(f"Failed to send Google Chat message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_via_api(
        self,
        space_name: str,
        message: Dict[str, Any],
        thread_key: Optional[str] = None,
    ) -> SendResult:
        """Send message using Chat API."""
        try:
            # Run in executor since googleapiclient is synchronous
            loop = asyncio.get_event_loop()

            request = self._chat_service.spaces().messages().create(
                parent=space_name,
                body=message,
                messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" if thread_key else None,
            )

            result = await loop.run_in_executor(None, request.execute)

            return SendResult(
                success=True,
                message_id=result.get("name", ""),
                raw=result,
            )

        except Exception as e:
            logger.error(f"Chat API error: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_via_webhook(self, message: Dict[str, Any]) -> SendResult:
        """Send message via webhook."""
        try:
            async with self._session.post(
                self._webhook_url,
                json=message,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status in (200, 201):
                    data = await response.json()
                    return SendResult(
                        success=True,
                        message_id=data.get("name", ""),
                        raw=data,
                    )
                else:
                    error_text = await response.text()
                    logger.error(f"Webhook error: {error_text}")
                    return SendResult(success=False, error=error_text)

        except Exception as e:
            logger.error(f"Webhook send error: {e}")
            return SendResult(success=False, error=str(e))

    def _build_card(
        self,
        text: str,
        buttons: List[Dict],
        header: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Build a Google Chat card message."""
        card = {
            "sections": [
                {
                    "widgets": [
                        {"textParagraph": {"text": text}},
                    ]
                }
            ]
        }

        # Add header if provided
        if header:
            card["header"] = {
                "title": header.get("title", ""),
                "subtitle": header.get("subtitle"),
                "imageUrl": header.get("image_url"),
            }

        # Add buttons
        button_list = []
        for btn in buttons:
            if btn.get("url"):
                button_list.append({
                    "textButton": {
                        "text": btn["text"],
                        "onClick": {
                            "openLink": {"url": btn["url"]}
                        }
                    }
                })
            else:
                button_list.append({
                    "textButton": {
                        "text": btn["text"],
                        "onClick": {
                            "action": {
                                "actionMethodName": btn.get("callback_data", btn["text"]),
                                "parameters": btn.get("parameters", []),
                            }
                        }
                    }
                })

        if button_list:
            card["sections"][0]["widgets"].append({
                "buttons": button_list
            })

        return card

    def build_card_v2(
        self,
        title: str,
        sections: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Build a Cards v2 format message.

        Args:
            title: Card title
            sections: List of section definitions

        Returns:
            Card message payload

        Example section:
            {
                "header": "Section Header",
                "widgets": [
                    {"text": "Some text"},
                    {"image": {"url": "https://..."}},
                    {"buttons": [{"text": "Click", "url": "https://..."}]},
                ]
            }
        """
        card_sections = []

        for section in sections:
            widgets = []

            for widget in section.get("widgets", []):
                if "text" in widget:
                    widgets.append({
                        "decoratedText": {
                            "text": widget["text"],
                            "wrapText": True,
                        }
                    })
                elif "image" in widget:
                    widgets.append({
                        "image": {
                            "imageUrl": widget["image"]["url"],
                            "altText": widget["image"].get("alt", ""),
                        }
                    })
                elif "buttons" in widget:
                    button_list = []
                    for btn in widget["buttons"]:
                        if btn.get("url"):
                            button_list.append({
                                "text": btn["text"],
                                "onClick": {"openLink": {"url": btn["url"]}},
                            })
                        else:
                            button_list.append({
                                "text": btn["text"],
                                "onClick": {
                                    "action": {
                                        "function": btn.get("action", btn["text"]),
                                        "parameters": btn.get("parameters", []),
                                    }
                                },
                            })
                    widgets.append({"buttonList": {"buttons": button_list}})

            card_sections.append({
                "header": section.get("header"),
                "widgets": widgets,
            })

        return {
            "cardsV2": [
                {
                    "cardId": "main",
                    "card": {
                        "header": {"title": title},
                        "sections": card_sections,
                    }
                }
            ]
        }

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing message."""
        if not self._chat_service:
            return SendResult(success=False, error="API mode required for editing")

        try:
            message = {"text": text}

            if buttons:
                message["cards"] = [self._build_card(text, buttons)]
                del message["text"]

            loop = asyncio.get_event_loop()

            request = self._chat_service.spaces().messages().update(
                name=message_id,
                updateMask="text,cards",
                body=message,
            )

            result = await loop.run_in_executor(None, request.execute)

            return SendResult(
                success=True,
                message_id=result.get("name", ""),
                raw=result,
            )

        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a message."""
        if not self._chat_service:
            return False

        try:
            loop = asyncio.get_event_loop()

            request = self._chat_service.spaces().messages().delete(
                name=message_id
            )

            await loop.run_in_executor(None, request.execute)
            return True

        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator (not supported in Google Chat)."""
        # Google Chat doesn't have typing indicators
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a space."""
        if not self._chat_service:
            return None

        try:
            loop = asyncio.get_event_loop()

            request = self._chat_service.spaces().get(name=chat_id)
            result = await loop.run_in_executor(None, request.execute)

            return {
                "id": result.get("name"),
                "type": result.get("type"),
                "display_name": result.get("displayName"),
                "single_user_bot_dm": result.get("singleUserBotDm"),
            }

        except Exception as e:
            logger.error(f"Failed to get space info: {e}")
            return None

    async def list_spaces(self) -> List[Dict[str, Any]]:
        """List spaces the bot is a member of."""
        if not self._chat_service:
            return []

        try:
            loop = asyncio.get_event_loop()

            request = self._chat_service.spaces().list()
            result = await loop.run_in_executor(None, request.execute)

            return result.get("spaces", [])

        except Exception as e:
            logger.error(f"Failed to list spaces: {e}")
            return []

    async def create_space(
        self,
        name: str,
        external_user_allowed: bool = False,
    ) -> Optional[str]:
        """Create a new space."""
        if not self._chat_service:
            return None

        try:
            loop = asyncio.get_event_loop()

            request = self._chat_service.spaces().create(
                body={
                    "displayName": name,
                    "spaceType": "SPACE",
                    "externalUserAllowed": external_user_allowed,
                }
            )

            result = await loop.run_in_executor(None, request.execute)
            return result.get("name")

        except Exception as e:
            logger.error(f"Failed to create space: {e}")
            return None


def create_google_chat_adapter(
    webhook_url: str = None,
    service_account_file: str = None,
    **kwargs
) -> GoogleChatAdapter:
    """
    Factory function to create Google Chat adapter.

    Args:
        webhook_url: Webhook URL for incoming messages (or set GOOGLE_CHAT_WEBHOOK env var)
        service_account_file: Path to service account JSON (or set GOOGLE_CHAT_SA_FILE env var)
        **kwargs: Additional config options

    Returns:
        Configured GoogleChatAdapter
    """
    webhook_url = webhook_url or os.getenv("GOOGLE_CHAT_WEBHOOK")
    service_account_file = service_account_file or os.getenv("GOOGLE_CHAT_SA_FILE")

    if not webhook_url and not service_account_file:
        raise ValueError("Either webhook_url or service_account_file required")

    config = ChannelConfig(
        webhook_url=webhook_url,
        extra={
            "service_account_file": service_account_file,
            **kwargs.get("extra", {}),
        },
        **{k: v for k, v in kwargs.items() if k != "extra"},
    )
    return GoogleChatAdapter(config)
