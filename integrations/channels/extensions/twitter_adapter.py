"""
Twitter/X Channel Adapter

Implements Twitter/X Direct Messages and mentions handling.
Based on SantaClaw extension patterns for Twitter.

Features:
- Direct Messages (DMs) send/receive
- Mention tracking and replies
- Media attachments
- Typing indicators (conversation events)
- Welcome messages
- Quick replies for DMs
- Account Activity API webhooks
- OAuth 1.0a authentication
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hmac
import hashlib
import base64
import time
import urllib.parse
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
import aiohttp

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


# Twitter API endpoints
TWITTER_API_BASE = "https://api.twitter.com"
TWITTER_API_V2 = f"{TWITTER_API_BASE}/2"
TWITTER_API_V1 = f"{TWITTER_API_BASE}/1.1"

# DM endpoints (v1.1 still used for DMs)
TWITTER_DM_SEND = f"{TWITTER_API_V1}/direct_messages/events/new.json"
TWITTER_DM_LIST = f"{TWITTER_API_V1}/direct_messages/events/list.json"
TWITTER_DM_SHOW = f"{TWITTER_API_V1}/direct_messages/events/show.json"
TWITTER_DM_TYPING = f"{TWITTER_API_V1}/direct_messages/indicate_typing.json"
TWITTER_DM_MARK_READ = f"{TWITTER_API_V1}/direct_messages/mark_read.json"

# Tweet endpoints (v2)
TWITTER_TWEETS = f"{TWITTER_API_V2}/tweets"
TWITTER_USERS = f"{TWITTER_API_V2}/users"

# Media endpoints
TWITTER_MEDIA_UPLOAD = "https://upload.twitter.com/1.1/media/upload.json"

# Webhook endpoints
TWITTER_WEBHOOKS = f"{TWITTER_API_V1}/account_activity/all"


@dataclass
class TwitterConfig(ChannelConfig):
    """Twitter-specific configuration."""
    consumer_key: str = ""
    consumer_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""
    bearer_token: Optional[str] = None  # For app-only auth
    environment_name: str = "production"  # For Account Activity API
    enable_dm: bool = True
    enable_mentions: bool = True
    enable_welcome_message: bool = False
    welcome_message_id: Optional[str] = None


@dataclass
class QuickReplyOption:
    """Quick reply option for DMs."""
    label: str
    description: Optional[str] = None
    metadata: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        result = {"label": self.label[:36]}  # Max 36 chars
        if self.description:
            result["description"] = self.description[:72]  # Max 72 chars
        if self.metadata:
            result["metadata"] = self.metadata[:1000]  # Max 1000 chars
        return result


@dataclass
class CallToAction:
    """Call to action button for DMs."""
    type: str = "web_url"  # web_url
    label: str = ""
    url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        return {
            "type": self.type,
            "label": self.label[:36],
            "url": self.url,
        }


class TwitterOAuth:
    """
    OAuth 1.0a helper for Twitter API authentication.
    """

    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
    ):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret

    def _generate_nonce(self) -> str:
        """Generate OAuth nonce."""
        return base64.b64encode(os.urandom(32)).decode('utf-8').replace('+', '').replace('/', '')[:32]

    def _generate_timestamp(self) -> str:
        """Generate OAuth timestamp."""
        return str(int(time.time()))

    def _create_signature_base(
        self,
        method: str,
        url: str,
        params: Dict[str, str],
    ) -> str:
        """Create the signature base string."""
        # Sort and encode parameters
        sorted_params = sorted(params.items())
        encoded_params = urllib.parse.urlencode(sorted_params, safe='')

        # Create base string
        base = f"{method.upper()}&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(encoded_params, safe='')}"
        return base

    def _create_signature(
        self,
        method: str,
        url: str,
        params: Dict[str, str],
    ) -> str:
        """Create OAuth signature."""
        base = self._create_signature_base(method, url, params)

        # Create signing key
        key = f"{urllib.parse.quote(self.consumer_secret, safe='')}&{urllib.parse.quote(self.access_token_secret, safe='')}"

        # Calculate HMAC-SHA1
        signature = hmac.new(
            key.encode('utf-8'),
            base.encode('utf-8'),
            hashlib.sha1
        ).digest()

        return base64.b64encode(signature).decode('utf-8')

    def get_auth_header(
        self,
        method: str,
        url: str,
        body_params: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Generate OAuth 1.0a Authorization header.
        """
        # OAuth parameters
        oauth_params = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_nonce": self._generate_nonce(),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": self._generate_timestamp(),
            "oauth_token": self.access_token,
            "oauth_version": "1.0",
        }

        # Combine with body params for signature
        all_params = {**oauth_params}
        if body_params:
            all_params.update(body_params)

        # Generate signature
        oauth_params["oauth_signature"] = self._create_signature(method, url, all_params)

        # Build header
        header_params = ', '.join(
            f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
            for k, v in sorted(oauth_params.items())
        )

        return f"OAuth {header_params}"


class TwitterAdapter(ChannelAdapter):
    """
    Twitter/X messaging adapter for DMs and mentions.

    Usage:
        config = TwitterConfig(
            consumer_key="your-key",
            consumer_secret="your-secret",
            access_token="your-token",
            access_token_secret="your-token-secret",
        )
        adapter = TwitterAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: TwitterConfig):
        super().__init__(config)
        self.twitter_config: TwitterConfig = config
        self._oauth: Optional[TwitterOAuth] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._user_id: Optional[str] = None
        self._username: Optional[str] = None
        self._mention_handlers: List[Callable] = []
        self._dm_handlers: Dict[str, Callable] = {}
        self._user_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "twitter"

    async def connect(self) -> bool:
        """Initialize Twitter API connection."""
        if not all([
            self.twitter_config.consumer_key,
            self.twitter_config.consumer_secret,
            self.twitter_config.access_token,
            self.twitter_config.access_token_secret,
        ]):
            logger.error("Twitter OAuth credentials required")
            return False

        try:
            # Create OAuth helper
            self._oauth = TwitterOAuth(
                self.twitter_config.consumer_key,
                self.twitter_config.consumer_secret,
                self.twitter_config.access_token,
                self.twitter_config.access_token_secret,
            )

            # Create HTTP session
            self._session = aiohttp.ClientSession()

            # Verify credentials
            user_info = await self._verify_credentials()
            if not user_info:
                logger.error("Failed to verify Twitter credentials")
                return False

            self._user_id = user_info.get("id")
            self._username = user_info.get("username")

            self.status = ChannelStatus.CONNECTED
            logger.info(f"Twitter adapter connected as: @{self._username}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Twitter: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect Twitter adapter."""
        if self._session:
            await self._session.close()
            self._session = None

        self._oauth = None
        self.status = ChannelStatus.DISCONNECTED

    async def _verify_credentials(self) -> Optional[Dict[str, Any]]:
        """Verify OAuth credentials and get user info."""
        if not self._session or not self._oauth:
            return None

        try:
            url = f"{TWITTER_API_V2}/users/me"

            auth_header = self._oauth.get_auth_header("GET", url)

            async with self._session.get(
                url,
                headers={"Authorization": auth_header}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data")
                else:
                    data = await response.json()
                    logger.error(f"Failed to verify credentials: {data}")
                    return None

        except Exception as e:
            logger.error(f"Error verifying credentials: {e}")
            return None

    def verify_webhook(self, crc_token: str) -> str:
        """
        Generate CRC response for webhook verification.
        Returns the response_token to send back.
        """
        signature = hmac.new(
            self.twitter_config.consumer_secret.encode('utf-8'),
            crc_token.encode('utf-8'),
            hashlib.sha256
        ).digest()

        return f"sha256={base64.b64encode(signature).decode('utf-8')}"

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """Verify webhook request signature."""
        if not signature.startswith("sha256="):
            return False

        expected = hmac.new(
            self.twitter_config.consumer_secret.encode('utf-8'),
            body,
            hashlib.sha256
        ).digest()

        expected_sig = f"sha256={base64.b64encode(expected).decode('utf-8')}"
        return hmac.compare_digest(expected_sig, signature)

    async def handle_webhook(self, body: str, signature: Optional[str] = None) -> None:
        """
        Handle incoming webhook from Twitter Account Activity API.
        """
        try:
            # Verify signature
            if signature and not self.verify_signature(body.encode('utf-8'), signature):
                logger.error("Invalid webhook signature")
                return

            data = json.loads(body)

            # Get user ID for this subscription
            for_user_id = data.get("for_user_id")

            # Handle direct message events
            if "direct_message_events" in data and self.twitter_config.enable_dm:
                for dm_event in data["direct_message_events"]:
                    await self._handle_dm_event(dm_event, data.get("users", {}))

            # Handle direct message indicate typing
            if "direct_message_indicate_typing_events" in data:
                for event in data["direct_message_indicate_typing_events"]:
                    logger.debug(f"User typing: {event.get('sender_id')}")

            # Handle direct message mark read
            if "direct_message_mark_read_events" in data:
                for event in data["direct_message_mark_read_events"]:
                    logger.debug(f"Messages read by: {event.get('sender_id')}")

            # Handle tweet create events (mentions)
            if "tweet_create_events" in data and self.twitter_config.enable_mentions:
                for tweet in data["tweet_create_events"]:
                    await self._handle_mention(tweet, data.get("users", {}))

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")

    async def _handle_dm_event(
        self,
        event: Dict[str, Any],
        users: Dict[str, Any],
    ) -> None:
        """Handle DM event from webhook."""
        event_type = event.get("type")

        if event_type != "message_create":
            return

        message_data = event.get("message_create", {})
        sender_id = message_data.get("sender_id")

        # Ignore own messages
        if sender_id == self._user_id:
            return

        # Convert to unified message
        message = self._convert_dm_message(event, users)

        # Check for quick reply payload
        quick_reply = message_data.get("message_data", {}).get("quick_reply_response", {})
        if quick_reply.get("metadata"):
            metadata = quick_reply["metadata"]
            if metadata in self._dm_handlers:
                handler = self._dm_handlers[metadata]
                await handler(event)
                return

        await self._dispatch_message(message)

    async def _handle_mention(
        self,
        tweet: Dict[str, Any],
        users: Dict[str, Any],
    ) -> None:
        """Handle mention from webhook."""
        # Ignore own tweets
        user_data = tweet.get("user", {})
        if user_data.get("id_str") == self._user_id:
            return

        # Check if it mentions the bot
        mentions = tweet.get("entities", {}).get("user_mentions", [])
        is_mentioned = any(m.get("id_str") == self._user_id for m in mentions)

        if not is_mentioned:
            return

        # Dispatch to mention handlers
        for handler in self._mention_handlers:
            try:
                result = handler(tweet)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Error in mention handler: {e}")

        # Also dispatch as regular message if no specific handlers
        if not self._mention_handlers:
            message = self._convert_tweet_message(tweet)
            await self._dispatch_message(message)

    def _convert_dm_message(
        self,
        event: Dict[str, Any],
        users: Dict[str, Any],
    ) -> Message:
        """Convert Twitter DM event to unified Message format."""
        message_create = event.get("message_create", {})
        message_data = message_create.get("message_data", {})
        sender_id = message_create.get("sender_id", "")
        target_id = message_create.get("target", {}).get("recipient_id", "")

        # Get user info
        sender_info = users.get(sender_id, {})
        sender_name = sender_info.get("name") or sender_info.get("screen_name", "")

        # Cache user
        if sender_id:
            self._user_cache[sender_id] = sender_info

        # Extract text
        text = message_data.get("text", "")

        # Process attachments
        media = []
        attachment = message_data.get("attachment", {})
        if attachment:
            att_type = attachment.get("type")
            att_media = attachment.get("media", {})

            if att_type == "media":
                media_type = att_media.get("type", "photo")
                if media_type == "photo":
                    media.append(MediaAttachment(
                        type=MessageType.IMAGE,
                        url=att_media.get("media_url_https"),
                    ))
                elif media_type in ("video", "animated_gif"):
                    # Get video URL from variants
                    variants = att_media.get("video_info", {}).get("variants", [])
                    video_url = next(
                        (v.get("url") for v in variants if v.get("content_type") == "video/mp4"),
                        None
                    )
                    media.append(MediaAttachment(
                        type=MessageType.VIDEO,
                        url=video_url or att_media.get("media_url_https"),
                    ))
            elif att_type == "location":
                shared_place = attachment.get("location", {}).get("shared_place", {})
                name = shared_place.get("full_name", "")
                coords = shared_place.get("coordinates", {}).get("coordinates", [])
                if coords:
                    text = f"[location:{coords[1]},{coords[0]}] {name}"

        # Handle quick reply
        quick_reply = message_data.get("quick_reply_response", {})
        if quick_reply.get("metadata"):
            text = text or f"[quick_reply:{quick_reply['metadata']}]"

        # Timestamp
        created_at = int(event.get("created_timestamp", 0))

        return Message(
            id=event.get("id", ""),
            channel=self.name,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=sender_id,  # Use sender ID as chat ID for DMs
            text=text,
            media=media,
            timestamp=datetime.fromtimestamp(created_at / 1000) if created_at else datetime.now(),
            is_group=False,
            raw={
                'type': 'dm',
                'target_id': target_id,
                'quick_reply': quick_reply.get('metadata'),
            },
        )

    def _convert_tweet_message(self, tweet: Dict[str, Any]) -> Message:
        """Convert Twitter tweet to unified Message format."""
        user_data = tweet.get("user", {})
        sender_id = user_data.get("id_str", "")
        sender_name = user_data.get("name", "")
        screen_name = user_data.get("screen_name", "")

        # Get text
        text = tweet.get("text", "") or tweet.get("full_text", "")

        # Remove @mention of bot from text
        if self._username:
            text = text.replace(f"@{self._username}", "").strip()

        # Process media
        media = []
        entities = tweet.get("extended_entities", {}) or tweet.get("entities", {})
        for entity_media in entities.get("media", []):
            media_type = entity_media.get("type", "photo")
            if media_type == "photo":
                media.append(MediaAttachment(
                    type=MessageType.IMAGE,
                    url=entity_media.get("media_url_https"),
                ))
            elif media_type in ("video", "animated_gif"):
                variants = entity_media.get("video_info", {}).get("variants", [])
                video_url = next(
                    (v.get("url") for v in variants if v.get("content_type") == "video/mp4"),
                    None
                )
                media.append(MediaAttachment(
                    type=MessageType.VIDEO,
                    url=video_url,
                ))

        return Message(
            id=tweet.get("id_str", ""),
            channel=self.name,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=tweet.get("id_str", ""),  # Tweet ID as chat ID
            text=text,
            media=media,
            timestamp=datetime.now(),  # Parse created_at if needed
            is_group=True,  # Mentions are public
            is_bot_mentioned=True,
            raw={
                'type': 'mention',
                'screen_name': screen_name,
                'in_reply_to_status_id': tweet.get('in_reply_to_status_id_str'),
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
        """
        Send a message.

        For DMs, chat_id is the user ID.
        For replies to tweets, chat_id is the tweet ID (use reply_to_tweet instead).
        """
        if not self._session or not self._oauth:
            return SendResult(success=False, error="Not connected")

        # If it looks like a tweet ID (numeric string), reply to tweet
        if reply_to and len(reply_to) > 15:  # Tweet IDs are long
            return await self.reply_to_tweet(reply_to, text, media)

        # Otherwise send DM
        return await self.send_dm(chat_id, text, media, buttons)

    async def send_dm(
        self,
        user_id: str,
        text: str,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a direct message."""
        if not self._session or not self._oauth:
            return SendResult(success=False, error="Not connected")

        try:
            # Build message data
            message_data: Dict[str, Any] = {"text": text}

            # Handle media - need to upload first
            if media and len(media) > 0:
                media_id = await self._upload_media(media[0])
                if media_id:
                    message_data["attachment"] = {
                        "type": "media",
                        "media": {"id": media_id}
                    }

            # Handle buttons as quick replies
            if buttons:
                quick_replies = []
                for btn in buttons[:3]:  # Max 3 options
                    quick_replies.append({
                        "label": btn.get("text", "")[:36],
                        "metadata": btn.get("callback_data", btn.get("text", ""))[:1000],
                    })
                message_data["quick_reply"] = {
                    "type": "options",
                    "options": quick_replies,
                }

            # Build event payload
            payload = {
                "event": {
                    "type": "message_create",
                    "message_create": {
                        "target": {"recipient_id": user_id},
                        "message_data": message_data,
                    }
                }
            }

            auth_header = self._oauth.get_auth_header("POST", TWITTER_DM_SEND)

            async with self._session.post(
                TWITTER_DM_SEND,
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                data = await response.json()

                if response.status == 200:
                    event = data.get("event", {})
                    return SendResult(
                        success=True,
                        message_id=event.get("id"),
                    )
                else:
                    errors = data.get("errors", [{}])
                    error_msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"

                    # Check for rate limiting
                    if response.status == 429:
                        raise ChannelRateLimitError(60)

                    return SendResult(success=False, error=error_msg)

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"Failed to send DM: {e}")
            return SendResult(success=False, error=str(e))

    async def _upload_media(self, media: MediaAttachment) -> Optional[str]:
        """Upload media and return media_id."""
        if not self._session or not self._oauth:
            return None

        try:
            # Get media data
            if media.file_path:
                with open(media.file_path, 'rb') as f:
                    media_data = f.read()
            elif media.url:
                async with self._session.get(media.url) as resp:
                    media_data = await resp.read()
            else:
                return None

            # Encode as base64
            media_b64 = base64.b64encode(media_data).decode('utf-8')

            # Determine media category
            if media.type == MessageType.IMAGE:
                media_category = "dm_image"
            elif media.type == MessageType.VIDEO:
                media_category = "dm_video"
            elif media.type == MessageType.AUDIO:
                media_category = "dm_video"  # Twitter uses video for audio
            else:
                media_category = "dm_image"

            # Upload
            form_data = {
                "media_data": media_b64,
                "media_category": media_category,
            }

            auth_header = self._oauth.get_auth_header("POST", TWITTER_MEDIA_UPLOAD, form_data)

            async with self._session.post(
                TWITTER_MEDIA_UPLOAD,
                headers={"Authorization": auth_header},
                data=form_data,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("media_id_string")
                else:
                    data = await response.json()
                    logger.error(f"Media upload failed: {data}")
                    return None

        except Exception as e:
            logger.error(f"Error uploading media: {e}")
            return None

    async def reply_to_tweet(
        self,
        tweet_id: str,
        text: str,
        media: Optional[List[MediaAttachment]] = None,
    ) -> SendResult:
        """Reply to a tweet."""
        if not self._session or not self._oauth:
            return SendResult(success=False, error="Not connected")

        try:
            # Build tweet payload
            payload: Dict[str, Any] = {
                "text": text,
                "reply": {
                    "in_reply_to_tweet_id": tweet_id,
                }
            }

            # Handle media
            if media and len(media) > 0:
                media_id = await self._upload_media(media[0])
                if media_id:
                    payload["media"] = {"media_ids": [media_id]}

            auth_header = self._oauth.get_auth_header("POST", TWITTER_TWEETS)

            async with self._session.post(
                TWITTER_TWEETS,
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                data = await response.json()

                if response.status in (200, 201):
                    tweet_data = data.get("data", {})
                    return SendResult(
                        success=True,
                        message_id=tweet_data.get("id"),
                    )
                else:
                    error = data.get("detail") or data.get("title") or "Unknown error"
                    if response.status == 429:
                        raise ChannelRateLimitError(60)
                    return SendResult(success=False, error=error)

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"Failed to reply to tweet: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Twitter doesn't support message editing for DMs."""
        logger.warning("Twitter doesn't support DM editing")
        return await self.send_dm(chat_id, text, buttons=buttons)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """
        Delete a DM.
        Note: Only deletes for the authenticated user.
        """
        if not self._session or not self._oauth:
            return False

        try:
            url = f"{TWITTER_API_V1}/direct_messages/events/destroy.json"
            params = {"id": message_id}

            auth_header = self._oauth.get_auth_header("DELETE", url, params)

            async with self._session.delete(
                url,
                headers={"Authorization": auth_header},
                params=params,
            ) as response:
                return response.status == 204

        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator for DMs."""
        if not self._session or not self._oauth:
            return

        try:
            form_data = {"recipient_id": chat_id}
            auth_header = self._oauth.get_auth_header("POST", TWITTER_DM_TYPING, form_data)

            await self._session.post(
                TWITTER_DM_TYPING,
                headers={"Authorization": auth_header},
                data=form_data,
            )
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get user information."""
        return await self.get_user(chat_id)

    # Twitter-specific methods

    def on_mention(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        """Register a handler for mention events."""
        self._mention_handlers.append(handler)

    def register_quick_reply_handler(
        self,
        metadata: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """Register a handler for quick reply selections."""
        self._dm_handlers[metadata] = handler

    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user information by ID."""
        # Check cache
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        if not self._session or not self._oauth:
            return None

        try:
            url = f"{TWITTER_API_V2}/users/{user_id}"
            params = {"user.fields": "id,name,username,profile_image_url,description"}

            auth_header = self._oauth.get_auth_header("GET", url)

            async with self._session.get(
                url,
                headers={"Authorization": auth_header},
                params=params,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    user_data = data.get("data")
                    if user_data:
                        self._user_cache[user_id] = user_data
                    return user_data
                return None

        except Exception as e:
            logger.error(f"Error getting user: {e}")
            return None

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user information by username."""
        if not self._session or not self._oauth:
            return None

        try:
            # Remove @ if present
            username = username.lstrip('@')

            url = f"{TWITTER_API_V2}/users/by/username/{username}"
            params = {"user.fields": "id,name,username,profile_image_url,description"}

            auth_header = self._oauth.get_auth_header("GET", url)

            async with self._session.get(
                url,
                headers={"Authorization": auth_header},
                params=params,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data")
                return None

        except Exception as e:
            logger.error(f"Error getting user by username: {e}")
            return None

    async def mark_read(self, user_id: str, last_read_message_id: str) -> bool:
        """Mark DM conversation as read."""
        if not self._session or not self._oauth:
            return False

        try:
            form_data = {
                "recipient_id": user_id,
                "last_read_event_id": last_read_message_id,
            }

            auth_header = self._oauth.get_auth_header("POST", TWITTER_DM_MARK_READ, form_data)

            async with self._session.post(
                TWITTER_DM_MARK_READ,
                headers={"Authorization": auth_header},
                data=form_data,
            ) as response:
                return response.status == 204

        except Exception as e:
            logger.error(f"Error marking read: {e}")
            return False

    async def send_dm_with_quick_replies(
        self,
        user_id: str,
        text: str,
        options: List[QuickReplyOption],
    ) -> SendResult:
        """Send a DM with quick reply options."""
        buttons = [
            {"text": opt.label, "callback_data": opt.metadata or opt.label}
            for opt in options
        ]
        return await self.send_dm(user_id, text, buttons=buttons)

    async def send_dm_with_cta(
        self,
        user_id: str,
        text: str,
        ctas: List[CallToAction],
    ) -> SendResult:
        """Send a DM with call-to-action buttons."""
        if not self._session or not self._oauth:
            return SendResult(success=False, error="Not connected")

        try:
            message_data = {
                "text": text,
                "ctas": [cta.to_dict() for cta in ctas[:3]],
            }

            payload = {
                "event": {
                    "type": "message_create",
                    "message_create": {
                        "target": {"recipient_id": user_id},
                        "message_data": message_data,
                    }
                }
            }

            auth_header = self._oauth.get_auth_header("POST", TWITTER_DM_SEND)

            async with self._session.post(
                TWITTER_DM_SEND,
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                data = await response.json()

                if response.status == 200:
                    event = data.get("event", {})
                    return SendResult(success=True, message_id=event.get("id"))
                else:
                    errors = data.get("errors", [{}])
                    error_msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
                    return SendResult(success=False, error=error_msg)

        except Exception as e:
            logger.error(f"Failed to send DM with CTA: {e}")
            return SendResult(success=False, error=str(e))


def create_twitter_adapter(
    consumer_key: str = None,
    consumer_secret: str = None,
    access_token: str = None,
    access_token_secret: str = None,
    **kwargs
) -> TwitterAdapter:
    """
    Factory function to create Twitter adapter.

    Args:
        consumer_key: Twitter API consumer key (or set TWITTER_CONSUMER_KEY env var)
        consumer_secret: Twitter API consumer secret (or set TWITTER_CONSUMER_SECRET env var)
        access_token: Twitter access token (or set TWITTER_ACCESS_TOKEN env var)
        access_token_secret: Twitter access token secret (or set TWITTER_ACCESS_TOKEN_SECRET env var)
        **kwargs: Additional config options

    Returns:
        Configured TwitterAdapter
    """
    consumer_key = consumer_key or os.getenv("TWITTER_CONSUMER_KEY")
    consumer_secret = consumer_secret or os.getenv("TWITTER_CONSUMER_SECRET")
    access_token = access_token or os.getenv("TWITTER_ACCESS_TOKEN")
    access_token_secret = access_token_secret or os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

    if not all([consumer_key, consumer_secret, access_token, access_token_secret]):
        raise ValueError("Twitter OAuth credentials required")

    config = TwitterConfig(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
        **kwargs
    )
    return TwitterAdapter(config)
