"""
Discord Channel Adapter

Implements Discord messaging using discord.py library.
Supports text channels, DMs, threads, embeds, and reactions.

Features:
- Text messages
- Embeds (rich content)
- Reactions
- Slash commands
- DM/Server detection
- Thread support
- File attachments
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, List, Dict, Any
from datetime import datetime

try:
    import discord
    from discord import Intents, Message as DiscordMessage, Embed, File
    from discord.ext import commands
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

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


class DiscordAdapter(ChannelAdapter):
    """
    Discord messaging adapter.

    Usage:
        config = ChannelConfig(token="BOT_TOKEN")
        adapter = DiscordAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: ChannelConfig):
        if not HAS_DISCORD:
            raise ImportError(
                "discord.py not installed. "
                "Install with: pip install discord.py"
            )

        super().__init__(config)

        # Set up intents
        intents = Intents.default()
        intents.message_content = True
        intents.members = True
        intents.dm_messages = True
        intents.guild_messages = True

        self._bot = commands.Bot(
            command_prefix=config.extra.get("prefix", "!"),
            intents=intents,
        )
        self._bot_user_id: Optional[int] = None
        self._setup_events()

    @property
    def name(self) -> str:
        return "discord"

    def _setup_events(self) -> None:
        """Set up Discord event handlers."""

        @self._bot.event
        async def on_ready():
            self._bot_user_id = self._bot.user.id
            self.status = ChannelStatus.CONNECTED
            logger.info(f"Discord connected as {self._bot.user.name}#{self._bot.user.discriminator}")

        @self._bot.event
        async def on_message(discord_msg: DiscordMessage):
            # Ignore own messages
            if discord_msg.author.id == self._bot_user_id:
                return

            # Convert and dispatch
            message = self._convert_message(discord_msg)
            await self._dispatch_message(message)

        @self._bot.event
        async def on_disconnect():
            self.status = ChannelStatus.DISCONNECTED
            logger.warning("Discord disconnected")

        @self._bot.event
        async def on_error(event, *args, **kwargs):
            logger.error(f"Discord error in {event}: {args}")
            self.status = ChannelStatus.ERROR

    async def connect(self) -> bool:
        """Connect to Discord using bot token."""
        if not self.config.token:
            logger.error("Discord bot token not provided")
            return False

        try:
            # Start bot in background
            self.status = ChannelStatus.CONNECTING
            asyncio.create_task(self._bot.start(self.config.token))

            # Wait for ready
            for _ in range(30):  # 30 second timeout
                if self.status == ChannelStatus.CONNECTED:
                    return True
                await asyncio.sleep(1)

            logger.error("Discord connection timeout")
            self.status = ChannelStatus.ERROR
            return False

        except discord.LoginFailure as e:
            logger.error(f"Discord login failed: {e}")
            self.status = ChannelStatus.ERROR
            return False
        except Exception as e:
            logger.error(f"Discord connection error: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Discord."""
        try:
            await self._bot.close()
        except Exception as e:
            logger.error(f"Error disconnecting from Discord: {e}")
        finally:
            self.status = ChannelStatus.DISCONNECTED

    def _convert_message(self, discord_msg: DiscordMessage) -> Message:
        """Convert Discord message to unified Message format."""
        # Check if bot is mentioned
        is_mentioned = self._bot.user in discord_msg.mentions if self._bot.user else False

        # Process attachments
        media = []
        for attachment in discord_msg.attachments:
            # Determine media type from content type
            content_type = attachment.content_type or ""
            if content_type.startswith("image/"):
                media_type = MessageType.IMAGE
            elif content_type.startswith("video/"):
                media_type = MessageType.VIDEO
            elif content_type.startswith("audio/"):
                media_type = MessageType.AUDIO
            else:
                media_type = MessageType.DOCUMENT

            media.append(MediaAttachment(
                type=media_type,
                url=attachment.url,
                file_name=attachment.filename,
                file_size=attachment.size,
                mime_type=content_type,
            ))

        # Determine if group (guild) or DM
        is_group = discord_msg.guild is not None

        return Message(
            id=str(discord_msg.id),
            channel=self.name,
            sender_id=str(discord_msg.author.id),
            sender_name=discord_msg.author.display_name,
            chat_id=str(discord_msg.channel.id),
            text=discord_msg.content,
            media=media,
            reply_to_id=str(discord_msg.reference.message_id) if discord_msg.reference else None,
            timestamp=discord_msg.created_at,
            is_group=is_group,
            is_bot_mentioned=is_mentioned,
            raw={
                "guild_id": str(discord_msg.guild.id) if discord_msg.guild else None,
                "guild_name": discord_msg.guild.name if discord_msg.guild else None,
                "channel_name": discord_msg.channel.name if hasattr(discord_msg.channel, 'name') else "DM",
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
        """Send a message to a Discord channel."""
        try:
            channel = self._bot.get_channel(int(chat_id))
            if not channel:
                # Try fetching if not in cache
                channel = await self._bot.fetch_channel(int(chat_id))

            if not channel:
                return SendResult(success=False, error="Channel not found")

            # Build embed if buttons provided (Discord uses embeds for rich content)
            embed = None
            view = None
            if buttons:
                embed, view = self._build_embed_with_buttons(text, buttons)
                text = None  # Text goes in embed

            # Handle media attachments
            files = []
            if media:
                for m in media:
                    if m.file_path:
                        files.append(File(m.file_path, filename=m.file_name))

            # Get reference message for reply
            reference = None
            if reply_to:
                try:
                    ref_msg = await channel.fetch_message(int(reply_to))
                    reference = ref_msg
                except Exception:
                    pass

            # Send message
            msg = await channel.send(
                content=text,
                embed=embed,
                files=files if files else None,
                reference=reference,
                view=view,
            )

            return SendResult(
                success=True,
                message_id=str(msg.id),
                raw={"jump_url": msg.jump_url},
            )

        except discord.Forbidden:
            logger.error(f"Permission denied to send to channel {chat_id}")
            return SendResult(success=False, error="Permission denied")
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                raise ChannelRateLimitError(retry_after=e.retry_after)
            logger.error(f"Discord HTTP error: {e}")
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")
            return SendResult(success=False, error=str(e))

    def _build_embed_with_buttons(self, text: str, buttons: List[Dict]) -> tuple:
        """Build Discord embed and view with buttons."""
        embed = Embed(description=text)

        # Create view with buttons
        view = discord.ui.View()
        for btn in buttons:
            if btn.get("url"):
                # Link button
                view.add_item(discord.ui.Button(
                    label=btn["text"],
                    url=btn["url"],
                    style=discord.ButtonStyle.link,
                ))
            else:
                # Callback button (would need custom handling)
                view.add_item(discord.ui.Button(
                    label=btn["text"],
                    custom_id=btn.get("callback_data", btn["text"]),
                    style=discord.ButtonStyle.primary,
                ))

        return embed, view

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing Discord message."""
        try:
            channel = self._bot.get_channel(int(chat_id))
            if not channel:
                channel = await self._bot.fetch_channel(int(chat_id))

            message = await channel.fetch_message(int(message_id))

            embed = None
            view = None
            if buttons:
                embed, view = self._build_embed_with_buttons(text, buttons)
                text = None

            await message.edit(content=text, embed=embed, view=view)

            return SendResult(success=True, message_id=message_id)

        except discord.NotFound:
            return SendResult(success=False, error="Message not found")
        except discord.Forbidden:
            return SendResult(success=False, error="Permission denied")
        except Exception as e:
            logger.error(f"Failed to edit Discord message: {e}")
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a Discord message."""
        try:
            channel = self._bot.get_channel(int(chat_id))
            if not channel:
                channel = await self._bot.fetch_channel(int(chat_id))

            message = await channel.fetch_message(int(message_id))
            await message.delete()
            return True

        except Exception as e:
            logger.error(f"Failed to delete Discord message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        try:
            channel = self._bot.get_channel(int(chat_id))
            if channel:
                await channel.typing()
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Discord channel."""
        try:
            channel = self._bot.get_channel(int(chat_id))
            if not channel:
                channel = await self._bot.fetch_channel(int(chat_id))

            info = {
                "id": channel.id,
                "type": str(channel.type),
            }

            if hasattr(channel, 'name'):
                info["name"] = channel.name
            if hasattr(channel, 'guild'):
                info["guild_id"] = channel.guild.id
                info["guild_name"] = channel.guild.name

            return info

        except Exception as e:
            logger.error(f"Failed to get Discord channel info: {e}")
            return None

    async def add_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Add a reaction to a message."""
        try:
            channel = self._bot.get_channel(int(chat_id))
            if not channel:
                channel = await self._bot.fetch_channel(int(chat_id))

            message = await channel.fetch_message(int(message_id))
            await message.add_reaction(emoji)
            return True

        except Exception as e:
            logger.error(f"Failed to add reaction: {e}")
            return False

    async def create_thread(
        self,
        chat_id: str,
        message_id: str,
        name: str,
    ) -> Optional[str]:
        """Create a thread from a message."""
        try:
            channel = self._bot.get_channel(int(chat_id))
            if not channel:
                channel = await self._bot.fetch_channel(int(chat_id))

            message = await channel.fetch_message(int(message_id))
            thread = await message.create_thread(name=name)
            return str(thread.id)

        except Exception as e:
            logger.error(f"Failed to create thread: {e}")
            return None


def create_discord_adapter(token: str = None, **kwargs) -> DiscordAdapter:
    """
    Factory function to create Discord adapter.

    Args:
        token: Bot token (or set DISCORD_BOT_TOKEN env var)
        **kwargs: Additional config options

    Returns:
        Configured DiscordAdapter
    """
    token = token or os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("Discord bot token required")

    config = ChannelConfig(token=token, **kwargs)
    return DiscordAdapter(config)
