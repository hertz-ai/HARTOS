"""
Mention Gating

Provides mention-based command gating for group contexts.
Ported from HevolveBot's activation mode pattern.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Set
import re
import logging

logger = logging.getLogger(__name__)


class MentionMode(Enum):
    """Mode for mention gating."""
    ALWAYS = "always"          # Always respond, no mention required
    MENTION = "mention"        # Require @mention or reply to bot
    COMMANDS_ONLY = "commands_only"  # Only respond to commands
    QUIET = "quiet"           # Silenced, don't respond unless resumed


@dataclass
class MentionGateConfig:
    """
    Configuration for mention gating.

    Attributes:
        default_mode: Default mode for groups
        dm_mode: Mode for direct messages (usually ALWAYS)
        bot_username: Bot's username for @mention detection
        bot_user_id: Bot's user ID for reply detection
        command_prefixes: Prefixes that count as commands
        trigger_words: Additional trigger words (optional)
    """
    default_mode: MentionMode = MentionMode.MENTION
    dm_mode: MentionMode = MentionMode.ALWAYS
    bot_username: Optional[str] = None
    bot_user_id: Optional[str] = None
    command_prefixes: List[str] = None
    trigger_words: List[str] = None

    def __post_init__(self):
        if self.command_prefixes is None:
            self.command_prefixes = ["/", "!"]
        if self.trigger_words is None:
            self.trigger_words = []


@dataclass
class MentionCheckResult:
    """Result of checking mention gating."""
    should_respond: bool
    reason: str
    mode: MentionMode
    was_mentioned: bool = False
    was_replied_to: bool = False
    is_command: bool = False
    is_dm: bool = False


class MentionGate:
    """
    Controls when the bot should respond based on mention gating.

    Features:
    - Different modes for groups vs DMs
    - @mention detection
    - Reply-to-bot detection
    - Command prefix detection
    - Trigger word detection
    - Per-chat mode overrides
    """

    def __init__(self, config: Optional[MentionGateConfig] = None):
        self.config = config or MentionGateConfig()
        self._chat_modes: dict[str, MentionMode] = {}

    def check(
        self,
        text: str,
        chat_id: str,
        is_group: bool = False,
        is_bot_mentioned: bool = False,
        reply_to_bot: bool = False,
        sender_id: Optional[str] = None,
    ) -> MentionCheckResult:
        """
        Check if the bot should respond to a message.

        Args:
            text: Message text
            chat_id: Chat/channel ID
            is_group: Whether this is a group chat
            is_bot_mentioned: Whether bot was @mentioned
            reply_to_bot: Whether this is a reply to the bot
            sender_id: Sender's user ID

        Returns:
            MentionCheckResult with decision
        """
        # Determine active mode
        mode = self._get_mode(chat_id, is_group)

        # DMs always respond (unless quiet)
        if not is_group:
            if mode == MentionMode.QUIET:
                return MentionCheckResult(
                    should_respond=False,
                    reason="Quiet mode active",
                    mode=mode,
                    is_dm=True,
                )
            return MentionCheckResult(
                should_respond=True,
                reason="Direct message",
                mode=mode,
                is_dm=True,
            )

        # Check if this is a command
        is_command = self._is_command(text)

        # Check for mention
        was_mentioned = is_bot_mentioned or self._check_mention(text)

        # Check for reply to bot
        was_replied_to = reply_to_bot

        # Check for trigger words
        has_trigger = self._check_trigger_words(text)

        # Apply mode logic
        if mode == MentionMode.ALWAYS:
            return MentionCheckResult(
                should_respond=True,
                reason="Always mode active",
                mode=mode,
                was_mentioned=was_mentioned,
                was_replied_to=was_replied_to,
                is_command=is_command,
            )

        elif mode == MentionMode.QUIET:
            # Only respond to /resume command
            if is_command and text.strip().lower().startswith(("/resume", "!resume")):
                return MentionCheckResult(
                    should_respond=True,
                    reason="Resume command in quiet mode",
                    mode=mode,
                    is_command=True,
                )
            return MentionCheckResult(
                should_respond=False,
                reason="Quiet mode active",
                mode=mode,
                was_mentioned=was_mentioned,
                was_replied_to=was_replied_to,
                is_command=is_command,
            )

        elif mode == MentionMode.COMMANDS_ONLY:
            if is_command:
                return MentionCheckResult(
                    should_respond=True,
                    reason="Command detected",
                    mode=mode,
                    is_command=True,
                )
            return MentionCheckResult(
                should_respond=False,
                reason="Commands only mode - not a command",
                mode=mode,
                was_mentioned=was_mentioned,
                was_replied_to=was_replied_to,
            )

        elif mode == MentionMode.MENTION:
            # Respond if mentioned, replied to, command, or trigger word
            if was_mentioned:
                return MentionCheckResult(
                    should_respond=True,
                    reason="Bot was mentioned",
                    mode=mode,
                    was_mentioned=True,
                    is_command=is_command,
                )
            if was_replied_to:
                return MentionCheckResult(
                    should_respond=True,
                    reason="Reply to bot",
                    mode=mode,
                    was_replied_to=True,
                    is_command=is_command,
                )
            if is_command:
                return MentionCheckResult(
                    should_respond=True,
                    reason="Command detected",
                    mode=mode,
                    is_command=True,
                )
            if has_trigger:
                return MentionCheckResult(
                    should_respond=True,
                    reason="Trigger word detected",
                    mode=mode,
                    was_mentioned=was_mentioned,
                )
            return MentionCheckResult(
                should_respond=False,
                reason="Mention required - not mentioned",
                mode=mode,
            )

        # Default: don't respond
        return MentionCheckResult(
            should_respond=False,
            reason="Unknown mode",
            mode=mode,
        )

    def _get_mode(self, chat_id: str, is_group: bool) -> MentionMode:
        """Get the active mode for a chat."""
        # Check for chat-specific override
        if chat_id in self._chat_modes:
            return self._chat_modes[chat_id]

        # Use default based on chat type
        if is_group:
            return self.config.default_mode
        return self.config.dm_mode

    def _is_command(self, text: str) -> bool:
        """Check if text starts with a command prefix."""
        text = text.strip()
        return any(text.startswith(p) for p in self.config.command_prefixes)

    def _check_mention(self, text: str) -> bool:
        """Check if bot is @mentioned in text."""
        if not self.config.bot_username:
            return False

        # Case-insensitive mention check
        pattern = rf"@{re.escape(self.config.bot_username)}\b"
        return bool(re.search(pattern, text, re.IGNORECASE))

    def _check_trigger_words(self, text: str) -> bool:
        """Check for trigger words in text."""
        if not self.config.trigger_words:
            return False

        text_lower = text.lower()
        return any(word.lower() in text_lower for word in self.config.trigger_words)

    def set_mode(self, chat_id: str, mode: MentionMode) -> None:
        """Set mode for a specific chat."""
        self._chat_modes[chat_id] = mode
        logger.info(f"Set mention mode for {chat_id}: {mode.value}")

    def get_mode(self, chat_id: str, is_group: bool = True) -> MentionMode:
        """Get mode for a specific chat."""
        return self._get_mode(chat_id, is_group)

    def clear_mode(self, chat_id: str) -> bool:
        """Clear mode override for a chat."""
        if chat_id in self._chat_modes:
            del self._chat_modes[chat_id]
            return True
        return False

    def quiet(self, chat_id: str) -> None:
        """Set chat to quiet mode."""
        self.set_mode(chat_id, MentionMode.QUIET)

    def resume(self, chat_id: str) -> None:
        """Resume from quiet mode (restore default)."""
        self.clear_mode(chat_id)

    def set_always(self, chat_id: str) -> None:
        """Set chat to always respond mode."""
        self.set_mode(chat_id, MentionMode.ALWAYS)

    def set_mention_only(self, chat_id: str) -> None:
        """Set chat to mention-only mode."""
        self.set_mode(chat_id, MentionMode.MENTION)

    def set_commands_only(self, chat_id: str) -> None:
        """Set chat to commands-only mode."""
        self.set_mode(chat_id, MentionMode.COMMANDS_ONLY)

    def list_overrides(self) -> dict[str, MentionMode]:
        """List all chat mode overrides."""
        return dict(self._chat_modes)

    def update_config(
        self,
        bot_username: Optional[str] = None,
        bot_user_id: Optional[str] = None,
        default_mode: Optional[MentionMode] = None,
        dm_mode: Optional[MentionMode] = None,
        trigger_words: Optional[List[str]] = None,
    ) -> None:
        """Update configuration."""
        if bot_username is not None:
            self.config.bot_username = bot_username
        if bot_user_id is not None:
            self.config.bot_user_id = bot_user_id
        if default_mode is not None:
            self.config.default_mode = default_mode
        if dm_mode is not None:
            self.config.dm_mode = dm_mode
        if trigger_words is not None:
            self.config.trigger_words = trigger_words


# Global instance
_global_gate: Optional[MentionGate] = None


def get_mention_gate() -> MentionGate:
    """Get the global mention gate."""
    global _global_gate
    if _global_gate is None:
        _global_gate = MentionGate()
    return _global_gate


def reset_mention_gate() -> None:
    """Reset the global mention gate."""
    global _global_gate
    _global_gate = None
