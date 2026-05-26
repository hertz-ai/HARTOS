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
from .room_capable import RoomCapableAdapter, UnsupportedRoomError

logger = logging.getLogger(__name__)


class DiscordAdapter(ChannelAdapter, RoomCapableAdapter):
    """
    Discord messaging adapter.

    Usage:
        config = ChannelConfig(token="BOT_TOKEN")
        adapter = DiscordAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()

    Room semantics (UNIF-G2 ``RoomCapableAdapter``):
        Discord text channels — bot is auto-member of every channel
        in the guilds it has been invited to; ``join_room`` validates
        access + caches the room id for downstream filtering.

        Discord voice channels — ``join_room`` opens a ``VoiceClient``
        via ``voice_channel.connect()`` so the bot is visibly present.
        The audio-receive path (frames → STT) is wired in
        ``agent_voice_bridge._tick()`` (UNIF-G3 / W1.2).

        DMs — raises ``UnsupportedRoomError`` (DMs aren't rooms).
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
        intents.voice_states = True  # UNIF-G2: voice room presence + member listing

        self._bot = commands.Bot(
            command_prefix=config.extra.get("prefix", "!"),
            intents=intents,
        )
        self._bot_user_id: Optional[int] = None
        # UNIF-G2: per-room voice-client registry.  Keyed by Discord
        # voice-channel id (int).  Populated by ``join_room`` for voice
        # channels, drained by ``leave_room``.  Only voice channels live
        # here — text channels are presence-by-default.
        self._voice_clients: Dict[int, Any] = {}
        # UNIF-G2: text-room "presence" registry.  Discord doesn't have
        # an explicit join handshake for text channels (the bot is a
        # member of every guild text channel it can see), so we track
        # the bot's deliberate-presence intent locally for downstream
        # callers (e.g. message-filtering, leave_room semantics).
        self._joined_text_rooms: set = set()
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

    # ─── UNIF-G2: RoomCapableAdapter implementation ──────────────────

    async def _resolve_channel(self, room_id: str):
        """Best-effort channel resolve — cache → REST fallback.

        Returns the discord channel object or ``None`` if it can't be
        found (deleted / wrong id / no access).  Centralized so
        ``join_room``/``leave_room``/``list_room_members`` share the
        same lookup path instead of duplicating ``get_channel``+fallback
        ladder three times.
        """
        try:
            cid = int(room_id)
        except (TypeError, ValueError):
            return None
        channel = self._bot.get_channel(cid)
        if channel is not None:
            return channel
        try:
            return await self._bot.fetch_channel(cid)
        except discord.NotFound:
            return None
        except discord.Forbidden:
            return None
        except Exception as e:
            logger.error(f"Discord _resolve_channel({cid}) failed: {e}")
            return None

    async def join_room(self, room_id: str,
                        role: str = 'participant') -> bool:
        """Join a Discord channel/room as the agent's presence.

        - Text channel → validate access, cache presence intent, return True.
        - Voice channel → connect via ``VoiceClient`` so the bot is
          visibly in the call.  Audio frames are picked up later by
          ``agent_voice_bridge._tick()`` (UNIF-G3 / W1.2 wiring).
        - DM (no guild) → raise ``UnsupportedRoomError``.
        - Channel not found / forbidden → return False.

        Idempotent on (room_id) — calling twice does NOT double-join
        a voice channel; the cached client is returned.
        """
        channel = await self._resolve_channel(room_id)
        if channel is None:
            logger.warning(
                "Discord.join_room: channel %s not found / forbidden",
                room_id)
            return False

        if isinstance(channel, discord.DMChannel):
            raise UnsupportedRoomError(
                "Discord DMs are not rooms — use send_message for 1:1.")

        if isinstance(channel, discord.VoiceChannel):
            cid = int(room_id)
            existing = self._voice_clients.get(cid)
            if existing is not None and existing.is_connected():
                return True
            try:
                # UNIF-G7 Producer A: prefer VoiceRecvClient when the
                # discord-ext-voice-recv lib is installed, so the
                # HevolveStreamingSink can pipe per-speaker PCM through
                # the canonical streaming-STT WS server.  When the lib
                # is absent, fall back to the bare connect() call —
                # voice room PRESENCE behavior is unchanged.
                connect_kwargs = {'reconnect': True, 'self_deaf': False}
                try:
                    from .discord_voice_recv_sink import (
                        HAS_VOICE_RECV, VoiceRecvClient,
                    )
                    if HAS_VOICE_RECV and VoiceRecvClient is not None:
                        connect_kwargs['cls'] = VoiceRecvClient
                except Exception:
                    pass
                voice_client = await channel.connect(**connect_kwargs)
                self._voice_clients[cid] = voice_client
                # Best-effort attach the streaming sink.  Returns
                # False (silent) when the recv lib isn't installed or
                # the connected client doesn't support listen().
                try:
                    from .discord_voice_recv_sink import (
                        maybe_attach_recv_sink,
                    )
                    maybe_attach_recv_sink(
                        voice_client, call_id=str(cid),
                        bot_user_id=self._bot_user_id)
                except Exception as e:
                    logger.debug(
                        "Discord.join_room: sink attach skipped (%s)", e)
                logger.info(
                    "Discord.join_room: voice channel %s connected (role=%s)",
                    cid, role)
                return True
            except discord.ClientException as e:
                # Already connected from another path — treat as success.
                logger.info(
                    "Discord.join_room: voice %s already-connected (%s)",
                    cid, e)
                return True
            except (discord.opus.OpusNotLoaded, AttributeError) as e:
                logger.warning(
                    "Discord.join_room: opus library unavailable for "
                    "voice %s (%s); voice presence requires libopus on "
                    "the host", cid, e)
                return False
            except Exception as e:
                logger.error(
                    "Discord.join_room: voice connect failed for %s: %s",
                    cid, e)
                return False

        # Text channel / thread / news / forum / stage etc. — Discord
        # doesn't expose an explicit "join" for non-voice channels, but
        # we record presence-intent so leave_room is symmetric.
        self._joined_text_rooms.add(int(room_id))
        logger.info(
            "Discord.join_room: text room %s presence cached (role=%s)",
            room_id, role)
        return True

    async def leave_room(self, room_id: str) -> bool:
        """Leave a Discord channel/room.

        - Voice channel → disconnect the cached ``VoiceClient``.
        - Text channel → drop the cached presence intent.
        - Unknown / never-joined → return False (caller decides whether
          that's a problem).
        """
        try:
            cid = int(room_id)
        except (TypeError, ValueError):
            return False

        # Voice path
        voice_client = self._voice_clients.pop(cid, None)
        if voice_client is not None:
            try:
                if voice_client.is_connected():
                    await voice_client.disconnect(force=False)
                logger.info("Discord.leave_room: voice %s disconnected", cid)
                return True
            except Exception as e:
                logger.error(
                    "Discord.leave_room: voice disconnect %s failed: %s",
                    cid, e)
                return False

        # Text path
        if cid in self._joined_text_rooms:
            self._joined_text_rooms.discard(cid)
            logger.info("Discord.leave_room: text %s presence cleared", cid)
            return True

        logger.debug("Discord.leave_room: %s not in joined registry", cid)
        return False

    async def list_room_members(self, room_id: str) -> List[Dict[str, Any]]:
        """List members of a Discord channel/voice channel.

        Text channels — uses ``channel.members`` (requires GUILD_MEMBERS
        intent, which we declare).  Voice channels — uses
        ``channel.members`` populated from voice-state cache.

        Returns ``[]`` on any error / unknown channel; callers shouldn't
        rely on member listing being authoritative.
        """
        channel = await self._resolve_channel(room_id)
        if channel is None:
            return []
        members_attr = getattr(channel, 'members', None)
        if not members_attr:
            return []
        result: List[Dict[str, Any]] = []
        try:
            for m in members_attr:
                # Skip the bot itself in member listings — caller asked
                # who else is here.
                if m.id == self._bot_user_id:
                    continue
                result.append({
                    'id': str(m.id),
                    'display_name': getattr(m, 'display_name', None) or m.name,
                    'is_bot': bool(getattr(m, 'bot', False)),
                })
        except Exception as e:
            logger.error(
                "Discord.list_room_members: iterating %s failed: %s",
                room_id, e)
            return []
        return result


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
