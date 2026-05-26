"""
Telegram Channel Adapter

Implements Telegram messaging using python-telegram-bot library.
Supports both polling and webhook modes.

Features:
- Text messages
- Media (images, videos, documents, voice)
- Inline keyboards
- Reply threading
- Group/DM detection
- Bot mention detection
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, List, Dict, Any
from datetime import datetime

try:
    from telegram import (
        Update,
        Bot,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        InputMediaPhoto,
        InputMediaVideo,
        InputMediaDocument,
        InputMediaAudio,
    )
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        filters,
        ContextTypes,
    )
    from telegram.constants import ChatAction, ParseMode
    from telegram.error import TelegramError, RetryAfter
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False

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


class TelegramAdapter(ChannelAdapter):
    """
    Telegram messaging adapter.

    Usage:
        config = ChannelConfig(token="BOT_TOKEN")
        adapter = TelegramAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: ChannelConfig):
        if not HAS_TELEGRAM:
            raise ImportError(
                "python-telegram-bot not installed. "
                "Install with: pip install python-telegram-bot"
            )

        super().__init__(config)
        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
        self._bot_username: Optional[str] = None

    @property
    def name(self) -> str:
        return "telegram"

    async def connect(self) -> bool:
        """Connect to Telegram using bot token."""
        if not self.config.token:
            logger.error("Telegram bot token not provided")
            return False

        try:
            # Build application
            self._app = (
                ApplicationBuilder()
                .token(self.config.token)
                .build()
            )
            self._bot = self._app.bot

            # Get bot info
            bot_info = await self._bot.get_me()
            self._bot_username = bot_info.username
            logger.info(f"Connected as @{self._bot_username}")

            # Register handlers
            self._register_handlers()

            # Start polling in background
            await self._app.initialize()
            await self._app.start()
            asyncio.create_task(self._app.updater.start_polling(drop_pending_updates=True))

            self.status = ChannelStatus.CONNECTED
            return True

        except TelegramError as e:
            logger.error(f"Failed to connect to Telegram: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Telegram."""
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.error(f"Error disconnecting: {e}")
            finally:
                self._app = None
                self._bot = None
                self.status = ChannelStatus.DISCONNECTED

    def _register_handlers(self) -> None:
        """Register message handlers."""
        # Command handlers
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("help", self._handle_help))
        self._app.add_handler(CommandHandler("status", self._handle_status))

        # Message handlers
        self._app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self._handle_message
        ))
        self._app.add_handler(MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.DOCUMENT | filters.AUDIO | filters.VOICE,
            self._handle_media
        ))

        # Callback query handler (for inline keyboards)
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        await update.message.reply_text(
            "Hello! I'm your AI assistant. Send me a message to get started.\n\n"
            "Commands:\n"
            "/start - Start the bot\n"
            "/help - Show help\n"
            "/status - Check bot status"
        )

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        await update.message.reply_text(
            "I can help you with various tasks. Just send me a message!\n\n"
            "I support:\n"
            "- Text messages\n"
            "- Images and photos\n"
            "- Documents\n"
            "- Voice messages"
        )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        await update.message.reply_text(
            f"Bot Status: {self.status.value}\n"
            f"Bot Username: @{self._bot_username}"
        )

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages."""
        message = self._convert_message(update.message)
        await self._dispatch_message(message)

    async def _handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming media messages."""
        message = self._convert_message(update.message)
        await self._dispatch_message(message)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle callback queries from inline keyboards."""
        query = update.callback_query
        await query.answer()

        # Create a message-like event for the callback
        message = Message(
            id=str(query.id),
            channel=self.name,
            sender_id=str(query.from_user.id),
            sender_name=query.from_user.full_name,
            chat_id=str(query.message.chat.id),
            text=f"[callback:{query.data}]",
            is_group=query.message.chat.type != "private",
            raw={"callback_query": query.to_dict()},
        )
        await self._dispatch_message(message)

    def _convert_message(self, tg_message) -> Message:
        """Convert Telegram message to unified Message format."""
        chat = tg_message.chat
        user = tg_message.from_user

        # Determine if bot is mentioned
        is_mentioned = False
        text = tg_message.text or tg_message.caption or ""

        if self._bot_username:
            is_mentioned = f"@{self._bot_username}" in text

        # Check for media
        media = []
        if tg_message.photo:
            # Get largest photo
            photo = max(tg_message.photo, key=lambda p: p.width * p.height)
            media.append(MediaAttachment(
                type=MessageType.IMAGE,
                file_id=photo.file_id,
                caption=tg_message.caption,
            ))
        if tg_message.video:
            media.append(MediaAttachment(
                type=MessageType.VIDEO,
                file_id=tg_message.video.file_id,
                file_name=tg_message.video.file_name,
                mime_type=tg_message.video.mime_type,
                caption=tg_message.caption,
            ))
        if tg_message.document:
            media.append(MediaAttachment(
                type=MessageType.DOCUMENT,
                file_id=tg_message.document.file_id,
                file_name=tg_message.document.file_name,
                mime_type=tg_message.document.mime_type,
            ))
        if tg_message.audio:
            media.append(MediaAttachment(
                type=MessageType.AUDIO,
                file_id=tg_message.audio.file_id,
                file_name=tg_message.audio.file_name,
                mime_type=tg_message.audio.mime_type,
            ))
        if tg_message.voice:
            media.append(MediaAttachment(
                type=MessageType.VOICE,
                file_id=tg_message.voice.file_id,
                mime_type=tg_message.voice.mime_type,
            ))

        return Message(
            id=str(tg_message.message_id),
            channel=self.name,
            sender_id=str(user.id),
            sender_name=user.full_name,
            chat_id=str(chat.id),
            text=text,
            media=media,
            reply_to_id=str(tg_message.reply_to_message.message_id) if tg_message.reply_to_message else None,
            timestamp=tg_message.date,
            is_group=chat.type in ("group", "supergroup"),
            is_bot_mentioned=is_mentioned,
            raw=tg_message.to_dict(),
        )

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message to a Telegram chat."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            # Build keyboard if buttons provided
            keyboard = None
            if buttons:
                keyboard = self._build_keyboard(buttons)

            # Handle media
            if media and len(media) > 0:
                return await self._send_media(chat_id, text, media, reply_to, keyboard)

            # Send text message
            reply_to_id = int(reply_to) if reply_to else None
            msg = await self._bot.send_message(
                chat_id=int(chat_id),
                text=text,
                reply_to_message_id=reply_to_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )

            return SendResult(
                success=True,
                message_id=str(msg.message_id),
                raw=msg.to_dict(),
            )

        except RetryAfter as e:
            raise ChannelRateLimitError(retry_after=e.retry_after)
        except TelegramError as e:
            logger.error(f"Failed to send message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_media(
        self,
        chat_id: str,
        caption: str,
        media: List[MediaAttachment],
        reply_to: Optional[str],
        keyboard: Optional[InlineKeyboardMarkup],
    ) -> SendResult:
        """Send media message."""
        reply_to_id = int(reply_to) if reply_to else None
        first_media = media[0]

        try:
            if first_media.type == MessageType.IMAGE:
                msg = await self._bot.send_photo(
                    chat_id=int(chat_id),
                    photo=first_media.file_id or first_media.file_path or first_media.url,
                    caption=caption,
                    reply_to_message_id=reply_to_id,
                    reply_markup=keyboard,
                )
            elif first_media.type == MessageType.VIDEO:
                msg = await self._bot.send_video(
                    chat_id=int(chat_id),
                    video=first_media.file_id or first_media.file_path or first_media.url,
                    caption=caption,
                    reply_to_message_id=reply_to_id,
                    reply_markup=keyboard,
                )
            elif first_media.type == MessageType.DOCUMENT:
                msg = await self._bot.send_document(
                    chat_id=int(chat_id),
                    document=first_media.file_id or first_media.file_path or first_media.url,
                    caption=caption,
                    reply_to_message_id=reply_to_id,
                    reply_markup=keyboard,
                )
            elif first_media.type == MessageType.AUDIO:
                msg = await self._bot.send_audio(
                    chat_id=int(chat_id),
                    audio=first_media.file_id or first_media.file_path or first_media.url,
                    caption=caption,
                    reply_to_message_id=reply_to_id,
                    reply_markup=keyboard,
                )
            elif first_media.type == MessageType.VOICE:
                msg = await self._bot.send_voice(
                    chat_id=int(chat_id),
                    voice=first_media.file_id or first_media.file_path or first_media.url,
                    caption=caption,
                    reply_to_message_id=reply_to_id,
                    reply_markup=keyboard,
                )
            else:
                return SendResult(success=False, error=f"Unsupported media type: {first_media.type}")

            return SendResult(
                success=True,
                message_id=str(msg.message_id),
                raw=msg.to_dict(),
            )

        except TelegramError as e:
            logger.error(f"Failed to send media: {e}")
            return SendResult(success=False, error=str(e))

    def _build_keyboard(self, buttons: List[Dict]) -> InlineKeyboardMarkup:
        """Build inline keyboard from button definitions."""
        keyboard = []
        row = []

        for btn in buttons:
            if btn.get("url"):
                row.append(InlineKeyboardButton(
                    text=btn["text"],
                    url=btn["url"],
                ))
            else:
                row.append(InlineKeyboardButton(
                    text=btn["text"],
                    callback_data=btn.get("callback_data", btn["text"]),
                ))

            # New row after each button or when row_break is set
            if btn.get("row_break", False) or len(row) >= 3:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        return InlineKeyboardMarkup(keyboard)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing message."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            keyboard = self._build_keyboard(buttons) if buttons else None

            msg = await self._bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )

            return SendResult(
                success=True,
                message_id=str(msg.message_id),
                raw=msg.to_dict() if hasattr(msg, 'to_dict') else None,
            )

        except TelegramError as e:
            logger.error(f"Failed to edit message: {e}")
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a message."""
        if not self._bot:
            return False

        try:
            await self._bot.delete_message(
                chat_id=int(chat_id),
                message_id=int(message_id),
            )
            return True
        except TelegramError as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if self._bot:
            try:
                await self._bot.send_chat_action(
                    chat_id=int(chat_id),
                    action=ChatAction.TYPING,
                )
            except TelegramError:
                pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a chat."""
        if not self._bot:
            return None

        try:
            chat = await self._bot.get_chat(int(chat_id))
            return {
                "id": chat.id,
                "type": chat.type,
                "title": chat.title,
                "username": chat.username,
                "first_name": chat.first_name,
                "last_name": chat.last_name,
            }
        except TelegramError as e:
            logger.error(f"Failed to get chat info: {e}")
            return None

    async def download_file(self, file_id: str, destination: str) -> bool:
        """Download a file from Telegram."""
        if not self._bot:
            return False

        try:
            file = await self._bot.get_file(file_id)
            await file.download_to_drive(destination)
            return True
        except TelegramError as e:
            logger.error(f"Failed to download file: {e}")
            return False


def create_telegram_adapter(token: str = None, **kwargs) -> TelegramAdapter:
    """
    Factory function to create Telegram adapter.

    Args:
        token: Bot token (or set TELEGRAM_BOT_TOKEN env var)
        **kwargs: Additional config options

    Returns:
        Configured TelegramAdapter
    """
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Telegram bot token required")

    config = ChannelConfig(token=token, **kwargs)
    return TelegramAdapter(config)
