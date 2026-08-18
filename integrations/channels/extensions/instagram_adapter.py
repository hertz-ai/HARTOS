"""
Instagram Direct Messages Channel Adapter

Implements Instagram Direct Messages API via Meta Graph API.
Based on SantaClaw extension patterns for Instagram.

Features:
- Instagram DM API (via Facebook Graph API)
- Message types (text, image, video, story share)
- Ice breakers (conversation starters)
- Quick replies
- Generic templates
- Typing indicators
- Read receipts
- Story mentions
- Comment mentions
- Webhook handling
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hashlib
import hmac
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

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


# Meta Graph API endpoints
GRAPH_API_VERSION = "v18.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


@dataclass
class InstagramConfig(ChannelConfig):
    """Instagram-specific configuration."""
    page_access_token: str = ""  # Facebook page token linked to Instagram
    app_secret: str = ""
    verify_token: str = ""
    instagram_account_id: Optional[str] = None
    enable_story_replies: bool = True
    enable_comment_replies: bool = True
    api_version: str = GRAPH_API_VERSION


@dataclass
class IceBreaker:
    """Ice breaker (conversation starter) configuration."""
    question: str
    payload: str

    def to_dict(self) -> Dict[str, str]:
        """Convert to API format."""
        return {
            "question": self.question[:80],  # Max 80 chars
            "payload": self.payload,
        }


@dataclass
class QuickReply:
    """Quick reply button for Instagram."""
    content_type: str = "text"
    title: Optional[str] = None
    payload: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        result = {"content_type": self.content_type}
        if self.title:
            result["title"] = self.title[:20]
        if self.payload:
            result["payload"] = self.payload
        return result


@dataclass
class GenericElement:
    """Generic template element for Instagram."""
    title: str
    subtitle: Optional[str] = None
    image_url: Optional[str] = None
    buttons: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        result = {"title": self.title[:80]}
        if self.subtitle:
            result["subtitle"] = self.subtitle[:80]
        if self.image_url:
            result["image_url"] = self.image_url
        if self.buttons:
            result["buttons"] = self.buttons[:3]
        return result


class InstagramAdapter(ChannelAdapter):
    """
    Instagram Direct Messages adapter using Meta Graph API.

    Note: Instagram Messaging API requires:
    - A Facebook Page linked to the Instagram Business/Creator account
    - Approved Instagram Messaging permission
    - Business verification for some features

    Usage:
        config = InstagramConfig(
            page_access_token="your-page-token",
            app_secret="your-app-secret",
            verify_token="your-verify-token",
        )
        adapter = InstagramAdapter(config)
        adapter.on_message(my_handler)
        # Use with webhook endpoint
    """

    def __init__(self, config: InstagramConfig):
        super().__init__(config)
        self.instagram_config: InstagramConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._postback_handlers: Dict[str, Callable] = {}
        self._story_handlers: List[Callable] = []
        self._comment_handlers: List[Callable] = []
        self._api_base: str = f"https://graph.facebook.com/{config.api_version}"

    @property
    def name(self) -> str:
        return "instagram"

    async def connect(self) -> bool:
        """Initialize Instagram API connection."""
        if not self.instagram_config.page_access_token:
            logger.error("Instagram page access token required")
            return False

        try:
            # Create HTTP session
            self._session = aiohttp.ClientSession()

            # Get Instagram account ID
            account_info = await self._get_instagram_account()
            if not account_info:
                logger.error("Failed to get Instagram account info")
                return False

            self.instagram_config.instagram_account_id = account_info.get("id")

            self.status = ChannelStatus.CONNECTED
            username = account_info.get("username", "Unknown")
            logger.info(f"Instagram adapter connected as: @{username}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Instagram: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect Instagram adapter."""
        if self._session:
            await self._session.close()
            self._session = None

        self.status = ChannelStatus.DISCONNECTED

    async def _get_instagram_account(self) -> Optional[Dict[str, Any]]:
        """Get Instagram Business Account info linked to the page."""
        if not self._session:
            return None

        try:
            # First get the page's Instagram account
            url = f"{self._api_base}/me"
            params = {
                "access_token": self.instagram_config.page_access_token,
                "fields": "instagram_business_account{id,username,profile_picture_url,name}",
            }

            async with self._session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("instagram_business_account")
                else:
                    data = await response.json()
                    logger.error(f"Failed to get Instagram account: {data}")
                    return None

        except Exception as e:
            logger.error(f"Error getting Instagram account: {e}")
            return None

    def verify_webhook(self, mode: str, token: str, challenge: str) -> Optional[str]:
        """
        Verify webhook subscription request.
        Returns challenge string if valid, None if invalid.
        """
        if mode == "subscribe" and token == self.instagram_config.verify_token:
            return challenge
        return None

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """Verify webhook request signature."""
        if not self.instagram_config.app_secret:
            return True

        if not signature.startswith("sha256="):
            return False

        expected = "sha256=" + hmac.new(
            self.instagram_config.app_secret.encode('utf-8'),
            body,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    async def handle_webhook(self, body: str, signature: Optional[str] = None) -> None:
        """
        Handle incoming webhook POST request from Instagram.
        Should be called from your webhook endpoint.
        """
        try:
            # Verify signature
            if signature and not self.verify_signature(body.encode('utf-8'), signature):
                logger.error("Invalid webhook signature")
                return

            data = json.loads(body)

            # Verify it's an Instagram webhook
            if data.get("object") != "instagram":
                return

            # Process each entry
            for entry in data.get("entry", []):
                # Handle messaging events
                for messaging in entry.get("messaging", []):
                    await self._process_messaging_event(messaging)

                # Handle changes (comments, story mentions)
                for change in entry.get("changes", []):
                    await self._process_change_event(change)

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")

    async def _process_messaging_event(self, event: Dict[str, Any]) -> None:
        """Process a messaging event."""
        if "message" in event:
            await self._handle_message(event)
        elif "postback" in event:
            await self._handle_postback(event)
        elif "read" in event:
            logger.debug(f"Message read: {event}")
        elif "reaction" in event:
            await self._handle_reaction(event)

    async def _process_change_event(self, change: Dict[str, Any]) -> None:
        """Process a change event (comments, story mentions)."""
        field = change.get("field")

        if field == "story_insights":
            # Story mention
            if self.instagram_config.enable_story_replies:
                await self._handle_story_mention(change.get("value", {}))
        elif field == "comments":
            # Comment on post
            if self.instagram_config.enable_comment_replies:
                await self._handle_comment(change.get("value", {}))

    async def _handle_message(self, event: Dict[str, Any]) -> None:
        """Handle incoming message event."""
        message = self._convert_message(event)
        await self._dispatch_message(message)

    async def _handle_postback(self, event: Dict[str, Any]) -> None:
        """Handle postback event."""
        payload = event.get("postback", {}).get("payload")
        sender_id = event.get("sender", {}).get("id")

        if payload in self._postback_handlers:
            handler = self._postback_handlers[payload]
            await handler(event)
        else:
            # Convert to message
            message = Message(
                id=f"postback_{int(datetime.now().timestamp() * 1000)}",
                channel=self.name,
                sender_id=sender_id,
                chat_id=sender_id,
                text=f"[postback:{payload}]",
                timestamp=datetime.fromtimestamp(event.get("timestamp", 0) / 1000),
                is_group=False,
                raw={'postback': event.get('postback')},
            )
            await self._dispatch_message(message)

    async def _handle_reaction(self, event: Dict[str, Any]) -> None:
        """Handle message reaction event."""
        reaction = event.get("reaction", {})
        logger.debug(f"Reaction received: {reaction}")

    async def _handle_story_mention(self, data: Dict[str, Any]) -> None:
        """Handle story mention event."""
        for handler in self._story_handlers:
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Error in story handler: {e}")

    async def _handle_comment(self, data: Dict[str, Any]) -> None:
        """Handle comment event."""
        for handler in self._comment_handlers:
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Error in comment handler: {e}")

    def _convert_message(self, event: Dict[str, Any]) -> Message:
        """Convert Instagram event to unified Message format."""
        sender_id = event.get("sender", {}).get("id", "")
        message_data = event.get("message", {})
        timestamp = event.get("timestamp", int(datetime.now().timestamp() * 1000))

        msg_id = message_data.get("mid", "")
        text = message_data.get("text", "")

        # Process attachments
        media = []
        for attachment in message_data.get("attachments", []):
            att_type = attachment.get("type")
            payload = attachment.get("payload", {})

            if att_type == "image":
                media.append(MediaAttachment(
                    type=MessageType.IMAGE,
                    url=payload.get("url"),
                ))
            elif att_type == "video":
                media.append(MediaAttachment(
                    type=MessageType.VIDEO,
                    url=payload.get("url"),
                ))
            elif att_type == "audio":
                media.append(MediaAttachment(
                    type=MessageType.AUDIO,
                    url=payload.get("url"),
                ))
            elif att_type == "share":
                # Shared post/reel
                url = payload.get("url", "")
                text = f"[shared:{url}]"
            elif att_type == "story_mention":
                # Story mention
                url = payload.get("url", "")
                text = f"[story_mention:{url}]"
            elif att_type == "reel":
                media.append(MediaAttachment(
                    type=MessageType.VIDEO,
                    url=payload.get("url"),
                ))

        # Handle quick reply
        quick_reply = message_data.get("quick_reply", {})
        if quick_reply.get("payload"):
            text = text or f"[quick_reply:{quick_reply['payload']}]"

        # Check if it's a story reply
        reply_to = message_data.get("reply_to", {})
        is_story_reply = reply_to.get("story", {}).get("url") is not None

        return Message(
            id=msg_id,
            channel=self.name,
            sender_id=sender_id,
            chat_id=sender_id,
            text=text,
            media=media,
            timestamp=datetime.fromtimestamp(timestamp / 1000),
            is_group=False,
            raw={
                'is_story_reply': is_story_reply,
                'story': reply_to.get('story'),
                'is_deleted': message_data.get('is_deleted', False),
                'is_unsupported': message_data.get('is_unsupported', False),
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
        """Send a message to an Instagram user."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # Handle media
            if media and len(media) > 0:
                return await self._send_media_message(chat_id, media[0])

            # Build message
            message_data: Dict[str, Any] = {"text": text}

            return await self._send_api_request(chat_id, message_data)

        except Exception as e:
            logger.error(f"Failed to send Instagram message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_media_message(
        self,
        chat_id: str,
        media: MediaAttachment,
    ) -> SendResult:
        """Send a media message."""
        try:
            # Determine attachment type
            if media.type == MessageType.IMAGE:
                message_data = {
                    "attachment": {
                        "type": "image",
                        "payload": {"url": media.url}
                    }
                }
            elif media.type in (MessageType.VIDEO, MessageType.AUDIO):
                message_data = {
                    "attachment": {
                        "type": "video",
                        "payload": {"url": media.url}
                    }
                }
            else:
                # Instagram only supports image and video
                return SendResult(success=False, error="Unsupported media type")

            return await self._send_api_request(chat_id, message_data)

        except Exception as e:
            logger.error(f"Failed to send media: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_api_request(
        self,
        recipient_id: str,
        message: Dict[str, Any],
    ) -> SendResult:
        """Send a request to the Instagram Send API."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self._api_base}/me/messages"
            params = {"access_token": self.instagram_config.page_access_token}

            payload = {
                "recipient": {"id": recipient_id},
                "message": message,
            }

            async with self._session.post(url, params=params, json=payload) as response:
                data = await response.json()

                if response.status == 200:
                    return SendResult(
                        success=True,
                        message_id=data.get("message_id"),
                    )
                else:
                    error = data.get("error", {})
                    error_code = error.get("code")
                    error_msg = error.get("message", "Unknown error")

                    if error_code == 613:
                        raise ChannelRateLimitError(60)

                    # Handle 24-hour messaging window
                    if error_code == 10:
                        return SendResult(
                            success=False,
                            error="24-hour messaging window expired"
                        )

                    return SendResult(success=False, error=error_msg)

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"API request failed: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Instagram doesn't support message editing."""
        logger.warning("Instagram doesn't support message editing")
        return await self.send_message(chat_id, text)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Instagram doesn't support message deletion by bots."""
        logger.warning("Instagram doesn't support message deletion")
        return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if not self._session:
            return

        try:
            url = f"{self._api_base}/me/messages"
            params = {"access_token": self.instagram_config.page_access_token}

            payload = {
                "recipient": {"id": chat_id},
                "sender_action": "typing_on",
            }

            await self._session.post(url, params=params, json=payload)

        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get user profile information."""
        return await self.get_user_profile(chat_id)

    # Instagram-specific methods

    def register_postback_handler(
        self,
        payload: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """Register a handler for postback events."""
        self._postback_handlers[payload] = handler

    def on_story_mention(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        """Register a handler for story mention events."""
        self._story_handlers.append(handler)

    def on_comment(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        """Register a handler for comment events."""
        self._comment_handlers.append(handler)

    async def get_user_profile(
        self,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get user profile information.

        Note: Limited fields available for Instagram users.
        """
        if not self._session:
            return None

        try:
            url = f"{self._api_base}/{user_id}"
            params = {
                "access_token": self.instagram_config.page_access_token,
                "fields": "id,name,username,profile_pic",
            }

            async with self._session.get(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                return None

        except Exception as e:
            logger.error(f"Error getting user profile: {e}")
            return None

    async def send_quick_replies(
        self,
        chat_id: str,
        text: str,
        quick_replies: List[QuickReply],
    ) -> SendResult:
        """Send a message with quick reply buttons."""
        message_data = {
            "text": text,
            "quick_replies": [qr.to_dict() for qr in quick_replies[:13]],
        }

        return await self._send_api_request(chat_id, message_data)

    async def send_generic_template(
        self,
        chat_id: str,
        elements: List[GenericElement],
    ) -> SendResult:
        """Send a generic template (carousel)."""
        message_data = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": [elem.to_dict() for elem in elements[:10]],
                }
            }
        }

        return await self._send_api_request(chat_id, message_data)

    async def send_heart_reaction(self, chat_id: str, message_id: str) -> bool:
        """Send a heart reaction to a message."""
        if not self._session:
            return False

        try:
            url = f"{self._api_base}/me/messages"
            params = {"access_token": self.instagram_config.page_access_token}

            payload = {
                "recipient": {"id": chat_id},
                "sender_action": "react",
                "payload": {
                    "message_id": message_id,
                    "reaction": "love",
                }
            }

            async with self._session.post(url, params=params, json=payload) as response:
                return response.status == 200

        except Exception as e:
            logger.error(f"Failed to send reaction: {e}")
            return False

    # ── Content Publishing (feed posts) ──────────────────────────────
    #
    # Everything above this line is MESSAGING: DMs, quick replies, reactions,
    # webhooks. That is a different Meta product from putting a post on the
    # feed, and the adapter had only the first one. `send_message` cannot
    # publish, so "post to Instagram" had no code path at all.
    #
    # Feed publishing is a three-step dance, not a POST:
    #   1. create a media CONTAINER per image        -> creation_id
    #   2. (carousel) create a parent container      -> children=[ids]
    #   3. publish the container                     -> permalink
    #
    # Instagram FETCHES the image from image_url, so the URL must be public
    # HTTPS that Meta's crawler can reach. Bytes cannot be uploaded, and
    # localhost or a signed-expiring URL will fail at step 1.
    #
    # Requires an Instagram Business/Creator account and the
    # `instagram_content_publish` permission, which needs App Review to work
    # outside dev mode. Without it every call here returns Meta's permission
    # error rather than silently doing nothing.

    # Meta's documented ceilings. Enforced locally so a bad post fails here,
    # with a clear reason, instead of after N network round-trips.
    CAROUSEL_MIN_ITEMS = 2
    CAROUSEL_MAX_ITEMS = 10
    CAPTION_MAX_CHARS = 2200

    # A container is not immediately publishable. Images are usually instant,
    # but the API still reports IN_PROGRESS, and publishing early fails with
    # code 9007. Poll instead of sleeping a guessed constant.
    _CONTAINER_POLL_INTERVAL_S = 2
    _CONTAINER_POLL_MAX_ATTEMPTS = 15

    async def publish_photo(
        self,
        image_url: str,
        caption: str = "",
    ) -> SendResult:
        """Publish ONE image to the feed. Returns the media id on success."""
        container = await self._create_media_container({
            "image_url": image_url,
            "caption": caption[:self.CAPTION_MAX_CHARS],
        })
        if not container.success:
            return container
        return await self._publish_container(container.message_id)

    async def publish_carousel(
        self,
        image_urls: List[str],
        caption: str = "",
    ) -> SendResult:
        """Publish a multi-slide carousel, the format of a facts post.

        Each slide becomes its own container with is_carousel_item=true, then
        one parent container ties them together. A slide that fails to build
        aborts the whole post: publishing 6 of 9 slides would ship a broken
        argument, which is worse than not posting.
        """
        if not (self.CAROUSEL_MIN_ITEMS <= len(image_urls) <= self.CAROUSEL_MAX_ITEMS):
            return SendResult(
                success=False,
                error=(f"carousel needs {self.CAROUSEL_MIN_ITEMS}-"
                       f"{self.CAROUSEL_MAX_ITEMS} images, got {len(image_urls)}"),
            )

        children: List[str] = []
        for index, url in enumerate(image_urls):
            item = await self._create_media_container({
                "image_url": url,
                "is_carousel_item": "true",
            })
            if not item.success:
                return SendResult(
                    success=False,
                    error=f"slide {index + 1}/{len(image_urls)} failed: {item.error}",
                )
            children.append(item.message_id)

        parent = await self._create_media_container({
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption[:self.CAPTION_MAX_CHARS],
        })
        if not parent.success:
            return parent
        return await self._publish_container(parent.message_id)

    async def _create_media_container(
        self,
        params: Dict[str, Any],
    ) -> SendResult:
        """POST /{ig-user-id}/media. message_id carries the creation_id."""
        if not self._session:
            return SendResult(success=False, error="Not connected")
        account_id = self.instagram_config.instagram_account_id
        if not account_id:
            return SendResult(
                success=False,
                error="No instagram_account_id; call connect() first",
            )

        try:
            url = f"{self._api_base}/{account_id}/media"
            payload = dict(params)
            payload["access_token"] = self.instagram_config.page_access_token

            async with self._session.post(url, data=payload) as response:
                data = await response.json()
                if response.status == 200 and data.get("id"):
                    return SendResult(success=True, message_id=data["id"])
                return self._publish_error(data)

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"Instagram container create failed: {e}")
            return SendResult(success=False, error=str(e))

    async def _publish_container(self, creation_id: str) -> SendResult:
        """Wait for the container to finish, then POST media_publish."""
        ready = await self._wait_for_container(creation_id)
        if not ready.success:
            return ready

        if not self._session:
            return SendResult(success=False, error="Not connected")
        account_id = self.instagram_config.instagram_account_id

        try:
            url = f"{self._api_base}/{account_id}/media_publish"
            payload = {
                "creation_id": creation_id,
                "access_token": self.instagram_config.page_access_token,
            }
            async with self._session.post(url, data=payload) as response:
                data = await response.json()
                if response.status == 200 and data.get("id"):
                    logger.info("Instagram post published: %s", data["id"])
                    return SendResult(success=True, message_id=data["id"])
                return self._publish_error(data)

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"Instagram publish failed: {e}")
            return SendResult(success=False, error=str(e))

    async def _wait_for_container(self, creation_id: str) -> SendResult:
        """Poll status_code until FINISHED, or report why it will never be.

        ERROR is terminal: retrying a container Meta already rejected just
        burns quota. EXPIRED means it sat unpublished past 24h.
        """
        if not self._session:
            return SendResult(success=False, error="Not connected")

        for _ in range(self._CONTAINER_POLL_MAX_ATTEMPTS):
            try:
                url = f"{self._api_base}/{creation_id}"
                params = {
                    "fields": "status_code,status",
                    "access_token": self.instagram_config.page_access_token,
                }
                async with self._session.get(url, params=params) as response:
                    data = await response.json()
                    state = data.get("status_code")

                    if state == "FINISHED":
                        return SendResult(success=True, message_id=creation_id)
                    if state in ("ERROR", "EXPIRED"):
                        return SendResult(
                            success=False,
                            error=(f"container {creation_id} {state}: "
                                   f"{data.get('status', 'no detail')}"),
                        )
            except Exception as e:
                logger.debug("container poll error: %s", e)

            await asyncio.sleep(self._CONTAINER_POLL_INTERVAL_S)

        return SendResult(
            success=False,
            error=(f"container {creation_id} not ready after "
                   f"{self._CONTAINER_POLL_MAX_ATTEMPTS} polls"),
        )

    async def get_publishing_limit(self) -> Dict[str, Any]:
        """Remaining posts in the rolling 24h window (Meta allows 50).

        Worth reading BEFORE composing: hitting the cap mid-carousel leaves
        orphaned containers that count against nothing but confuse the logs.
        """
        if not self._session or not self.instagram_config.instagram_account_id:
            return {"error": "Not connected"}
        try:
            url = (f"{self._api_base}/"
                   f"{self.instagram_config.instagram_account_id}"
                   f"/content_publishing_limit")
            params = {
                "fields": "config,quota_usage",
                "access_token": self.instagram_config.page_access_token,
            }
            async with self._session.get(url, params=params) as response:
                data = await response.json()
                entries = data.get("data") or [{}]
                return entries[0]
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _publish_error(data: Dict[str, Any]) -> SendResult:
        """Turn a Graph API error body into a SendResult, or raise on throttle.

        Codes 4/17/32 are the app/user/page throughput limits. They are
        retryable and must NOT be reported as a content problem, or a caller
        will keep rewriting a post that was never the issue.
        """
        error = data.get("error", {}) or {}
        code = error.get("code")
        if code in (4, 17, 32):
            raise ChannelRateLimitError(3600)
        message = error.get("message", "Unknown error")
        subcode = error.get("error_subcode")
        if subcode:
            message = f"{message} (subcode {subcode})"
        return SendResult(success=False, error=message)

    async def set_ice_breakers(self, ice_breakers: List[IceBreaker]) -> bool:
        """
        Set ice breakers (conversation starters) for the Instagram account.

        Ice breakers appear when a user opens a new conversation.
        """
        if not self._session or not self.instagram_config.instagram_account_id:
            return False

        try:
            url = f"{self._api_base}/{self.instagram_config.instagram_account_id}/messenger_profile"
            params = {"access_token": self.instagram_config.page_access_token}

            payload = {
                "ice_breakers": [ib.to_dict() for ib in ice_breakers[:4]],  # Max 4
            }

            async with self._session.post(url, params=params, json=payload) as response:
                data = await response.json()
                return data.get("result") == "success"

        except Exception as e:
            logger.error(f"Error setting ice breakers: {e}")
            return False

    async def delete_ice_breakers(self) -> bool:
        """Delete ice breakers."""
        if not self._session or not self.instagram_config.instagram_account_id:
            return False

        try:
            url = f"{self._api_base}/{self.instagram_config.instagram_account_id}/messenger_profile"
            params = {"access_token": self.instagram_config.page_access_token}

            payload = {"fields": ["ice_breakers"]}

            async with self._session.delete(url, params=params, json=payload) as response:
                data = await response.json()
                return data.get("result") == "success"

        except Exception as e:
            logger.error(f"Error deleting ice breakers: {e}")
            return False

    async def reply_to_story(
        self,
        chat_id: str,
        story_id: str,
        text: str,
    ) -> SendResult:
        """Reply to a user's story."""
        message_data = {
            "text": text,
            "reply_to": {
                "story_id": story_id,
            }
        }

        return await self._send_api_request(chat_id, message_data)

    async def reply_to_comment(
        self,
        comment_id: str,
        text: str,
    ) -> SendResult:
        """Reply to a comment on a post."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self._api_base}/{comment_id}/replies"
            params = {"access_token": self.instagram_config.page_access_token}

            payload = {"message": text}

            async with self._session.post(url, params=params, json=payload) as response:
                data = await response.json()

                if response.status == 200:
                    return SendResult(success=True, message_id=data.get("id"))
                else:
                    error = data.get("error", {}).get("message", "Unknown error")
                    return SendResult(success=False, error=error)

        except Exception as e:
            logger.error(f"Failed to reply to comment: {e}")
            return SendResult(success=False, error=str(e))

    async def mark_seen(self, chat_id: str) -> bool:
        """Mark messages as seen."""
        if not self._session:
            return False

        try:
            url = f"{self._api_base}/me/messages"
            params = {"access_token": self.instagram_config.page_access_token}

            payload = {
                "recipient": {"id": chat_id},
                "sender_action": "mark_seen",
            }

            async with self._session.post(url, params=params, json=payload) as response:
                return response.status == 200

        except Exception:
            return False


def create_instagram_adapter(
    page_access_token: str = None,
    app_secret: str = None,
    verify_token: str = None,
    **kwargs
) -> InstagramAdapter:
    """
    Factory function to create Instagram adapter.

    Args:
        page_access_token: Facebook page access token (or set INSTAGRAM_PAGE_TOKEN env var)
        app_secret: Facebook app secret (or set INSTAGRAM_APP_SECRET env var)
        verify_token: Webhook verification token (or set INSTAGRAM_VERIFY_TOKEN env var)
        **kwargs: Additional config options

    Returns:
        Configured InstagramAdapter
    """
    page_access_token = page_access_token or os.getenv("INSTAGRAM_PAGE_TOKEN")
    app_secret = app_secret or os.getenv("INSTAGRAM_APP_SECRET")
    verify_token = verify_token or os.getenv("INSTAGRAM_VERIFY_TOKEN")

    if not page_access_token:
        raise ValueError("Instagram page access token required")

    config = InstagramConfig(
        page_access_token=page_access_token,
        app_secret=app_secret or "",
        verify_token=verify_token or "",
        **kwargs
    )
    return InstagramAdapter(config)
