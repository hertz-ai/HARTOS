"""
Built-in Commands

Implements 20+ built-in commands for the bot as specified in the HevolveBot integration plan.
Provides user commands, group commands, and admin commands.

User Commands:
- /help [command] - Show help for command or list all
- /start - Start interaction (welcome message)
- /stop - Stop receiving messages
- /status - Show bot and session status
- /pair <code> - Pair account with code
- /unpair - Remove account pairing
- /clear - Clear conversation history
- /history [n] - Show last n messages
- /model [name] - Show or set current model
- /language [lang] - Set preferred language
- /timezone [tz] - Set timezone
- /feedback <text> - Send feedback

Group Commands:
- /mention <on|off|reply> - Set mention mode for group
- /quiet - Disable bot in group temporarily
- /resume - Resume bot in group

Admin Commands:
- /broadcast <message> - Send to all users
- /stats - Show usage statistics
- /users - List active users
- /ban <user> - Block user
- /unban <user> - Unblock user
- /config get <key> - Get config value
- /config set <key> <val> - Set config value
- /reload - Reload configuration
- /debug <on|off> - Toggle debug mode
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)
import asyncio
import logging
import os

from .registry import (
    CommandRegistry,
    CommandDefinition,
    CommandScope,
    CommandCategory,
    get_command_registry,
)
from .detection import CommandDetector, DetectedCommand, get_command_detector
from .arguments import (
    ArgumentParser,
    ArgumentDefinition,
    ArgumentType,
    ArgumentChoice,
    ParseResult,
    validate_range,
    validate_length,
)
from .mention_gating import MentionGate, MentionMode, get_mention_gate

logger = logging.getLogger(__name__)


# ==============================================================================
# Command Context and Result
# ==============================================================================

@dataclass
class CommandContext:
    """
    Context passed to command handlers.

    Attributes:
        channel: Channel name (telegram, discord, etc.)
        chat_id: Chat/group ID
        sender_id: Sender's ID
        sender_name: Sender's display name
        user_id: Paired agent user ID (if paired)
        prompt_id: Paired agent prompt ID (if paired)
        is_admin: Whether sender has admin privileges
        is_group: Whether this is a group chat
        raw_args: Raw argument string
        parsed_args: Parsed arguments dict
        session: Session object (if available)
        message_id: Original message ID
        timestamp: Message timestamp
        metadata: Additional context metadata
    """
    channel: str
    chat_id: str
    sender_id: str
    sender_name: Optional[str] = None
    user_id: Optional[int] = None
    prompt_id: Optional[int] = None
    is_admin: bool = False
    is_group: bool = False
    raw_args: Optional[str] = None
    parsed_args: Dict[str, Any] = field(default_factory=dict)
    session: Optional[Any] = None
    message_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_paired(self) -> bool:
        """Check if user is paired."""
        return self.user_id is not None and self.prompt_id is not None


@dataclass
class CommandResult:
    """
    Result from executing a command.

    Attributes:
        success: Whether command succeeded
        message: Response message to send
        data: Additional data from command
        error: Error message if failed
        silent: Whether to suppress response
        metadata: Additional result metadata
    """
    success: bool
    message: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    silent: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, message: str = None, **data) -> "CommandResult":
        """Create a successful result."""
        return cls(success=True, message=message, data=data)

    @classmethod
    def fail(cls, error: str, **data) -> "CommandResult":
        """Create a failed result."""
        return cls(success=False, error=error, data=data)

    @classmethod
    def silent_ok(cls, **data) -> "CommandResult":
        """Create a silent successful result."""
        return cls(success=True, silent=True, data=data)


# ==============================================================================
# Command Handler Type
# ==============================================================================

# Type for command handlers - can be sync or async
CommandHandler = Callable[[CommandContext], Any]  # Returns CommandResult or coroutine


# ==============================================================================
# Built-in Commands Class
# ==============================================================================

class BuiltinCommands:
    """
    Provides all built-in commands for the bot.

    Registers and manages user commands, group commands, and admin commands.

    Usage:
        builtin = BuiltinCommands()
        builtin.register_all()

        # Or with custom registry:
        registry = CommandRegistry()
        builtin = BuiltinCommands(registry=registry)
        builtin.register_all()
    """

    def __init__(
        self,
        registry: Optional[CommandRegistry] = None,
        session_manager: Optional[Any] = None,
        pairing_manager: Optional[Any] = None,
        mention_gate: Optional[MentionGate] = None,
        config_store: Optional[Dict[str, Any]] = None,
        admin_ids: Optional[Set[str]] = None,
        bot_name: str = "Bot",
        bot_version: str = "1.0.0",
    ):
        """
        Initialize built-in commands.

        Args:
            registry: Command registry to use
            session_manager: Session manager for history/state
            pairing_manager: Pairing manager for auth
            mention_gate: Mention gate for group mode
            config_store: Configuration store
            admin_ids: Set of admin user IDs
            bot_name: Bot display name
            bot_version: Bot version string
        """
        self.registry = registry or get_command_registry()
        self.session_manager = session_manager
        self.pairing_manager = pairing_manager
        self.mention_gate = mention_gate or get_mention_gate()
        self.config_store = config_store or {}
        self.admin_ids = admin_ids or set()
        self.bot_name = bot_name
        self.bot_version = bot_version

        # State
        self._debug_mode = False
        self._stopped_users: Set[Tuple[str, str]] = set()  # (channel, sender_id)
        self._banned_users: Set[str] = set()
        self._feedback_handler: Optional[Callable] = None
        self._broadcast_handler: Optional[Callable] = None
        self._model_store: Dict[Tuple[str, str], str] = {}  # (channel, sender_id) -> model
        self._language_store: Dict[Tuple[str, str], str] = {}
        self._timezone_store: Dict[Tuple[str, str], str] = {}

        # Command parsers
        self._parsers: Dict[str, ArgumentParser] = {}
        self._setup_parsers()

    def _setup_parsers(self) -> None:
        """Set up argument parsers for commands."""
        # /help [command]
        self._parsers["help"] = ArgumentParser([
            ArgumentDefinition(
                name="command",
                description="Command to get help for",
                arg_type=ArgumentType.STRING,
                required=False,
            ),
        ])

        # /pair <code>
        self._parsers["pair"] = ArgumentParser([
            ArgumentDefinition(
                name="code",
                description="Pairing code",
                arg_type=ArgumentType.STRING,
                required=True,
            ),
        ])

        # /history [n]
        self._parsers["history"] = ArgumentParser([
            ArgumentDefinition(
                name="count",
                description="Number of messages to show",
                arg_type=ArgumentType.INTEGER,
                required=False,
                default=10,
                validator=validate_range(1, 100),
            ),
        ])

        # /model [name]
        self._parsers["model"] = ArgumentParser([
            ArgumentDefinition(
                name="name",
                description="Model name to set",
                arg_type=ArgumentType.STRING,
                required=False,
            ),
        ])

        # /language [lang]
        self._parsers["language"] = ArgumentParser([
            ArgumentDefinition(
                name="language",
                description="Language code (e.g., en, es, fr)",
                arg_type=ArgumentType.STRING,
                required=False,
            ),
        ])

        # /timezone [tz]
        self._parsers["timezone"] = ArgumentParser([
            ArgumentDefinition(
                name="timezone",
                description="Timezone (e.g., UTC, America/New_York)",
                arg_type=ArgumentType.STRING,
                required=False,
            ),
        ])

        # /feedback <text>
        self._parsers["feedback"] = ArgumentParser([
            ArgumentDefinition(
                name="text",
                description="Feedback text",
                arg_type=ArgumentType.STRING,
                required=True,
                capture_remaining=True,
                validator=validate_length(min_len=5, max_len=1000),
            ),
        ])

        # /mention <mode>
        self._parsers["mention"] = ArgumentParser([
            ArgumentDefinition(
                name="mode",
                description="Mention mode",
                arg_type=ArgumentType.CHOICE,
                required=True,
                choices=[
                    ArgumentChoice("on", "Always respond"),
                    ArgumentChoice("off", "Mention required"),
                    ArgumentChoice("reply", "Reply only"),
                ],
            ),
        ])

        # /broadcast <message>
        self._parsers["broadcast"] = ArgumentParser([
            ArgumentDefinition(
                name="message",
                description="Message to broadcast",
                arg_type=ArgumentType.STRING,
                required=True,
                capture_remaining=True,
            ),
        ])

        # /ban <user>
        self._parsers["ban"] = ArgumentParser([
            ArgumentDefinition(
                name="user",
                description="User ID to ban",
                arg_type=ArgumentType.STRING,
                required=True,
            ),
        ])

        # /unban <user>
        self._parsers["unban"] = ArgumentParser([
            ArgumentDefinition(
                name="user",
                description="User ID to unban",
                arg_type=ArgumentType.STRING,
                required=True,
            ),
        ])

        # /config get <key> or /config set <key> <value>
        self._parsers["config"] = ArgumentParser([
            ArgumentDefinition(
                name="action",
                description="Action (get or set)",
                arg_type=ArgumentType.CHOICE,
                required=True,
                choices=[
                    ArgumentChoice("get", "Get config value"),
                    ArgumentChoice("set", "Set config value"),
                ],
            ),
            ArgumentDefinition(
                name="key",
                description="Config key",
                arg_type=ArgumentType.STRING,
                required=True,
            ),
            ArgumentDefinition(
                name="value",
                description="Value to set (for set action)",
                arg_type=ArgumentType.STRING,
                required=False,
                capture_remaining=True,
            ),
        ])

        # /debug <on|off>
        self._parsers["debug"] = ArgumentParser([
            ArgumentDefinition(
                name="state",
                description="Debug state",
                arg_type=ArgumentType.BOOLEAN,
                required=True,
            ),
        ])

    def register_all(self) -> None:
        """Register all built-in commands."""
        self._register_user_commands()
        self._register_group_commands()
        self._register_admin_commands()
        logger.info(f"Registered {len(self.registry)} built-in commands")

    def _register_user_commands(self) -> None:
        """Register user commands."""
        # /help
        self.registry.register(CommandDefinition(
            key="help",
            description="Show help for a command or list all commands",
            handler=self.cmd_help,
            aliases=["/help", "/h", "/?"],
            category=CommandCategory.STATUS,
            accepts_args=True,
        ))

        # /start
        self.registry.register(CommandDefinition(
            key="start",
            description="Start interaction with the bot",
            handler=self.cmd_start,
            aliases=["/start"],
            category=CommandCategory.SESSION,
        ))

        # /stop
        self.registry.register(CommandDefinition(
            key="stop",
            description="Stop receiving messages from the bot",
            handler=self.cmd_stop,
            aliases=["/stop"],
            category=CommandCategory.SESSION,
        ))

        # /status
        self.registry.register(CommandDefinition(
            key="status",
            description="Show bot and session status",
            handler=self.cmd_status,
            aliases=["/status", "/info"],
            category=CommandCategory.STATUS,
        ))

        # /pair
        self.registry.register(CommandDefinition(
            key="pair",
            description="Pair your account with a pairing code",
            handler=self.cmd_pair,
            aliases=["/pair", "/link"],
            category=CommandCategory.SESSION,
            accepts_args=True,
        ))

        # /unpair
        self.registry.register(CommandDefinition(
            key="unpair",
            description="Remove account pairing",
            handler=self.cmd_unpair,
            aliases=["/unpair", "/unlink"],
            category=CommandCategory.SESSION,
        ))

        # /clear
        self.registry.register(CommandDefinition(
            key="clear",
            description="Clear conversation history",
            handler=self.cmd_clear,
            aliases=["/clear", "/reset"],
            category=CommandCategory.SESSION,
        ))

        # /history
        self.registry.register(CommandDefinition(
            key="history",
            description="Show last n messages from history",
            handler=self.cmd_history,
            aliases=["/history", "/hist"],
            category=CommandCategory.SESSION,
            accepts_args=True,
        ))

        # /model
        self.registry.register(CommandDefinition(
            key="model",
            description="Show or set current AI model",
            handler=self.cmd_model,
            aliases=["/model"],
            category=CommandCategory.OPTIONS,
            accepts_args=True,
        ))

        # /language
        self.registry.register(CommandDefinition(
            key="language",
            description="Set preferred language",
            handler=self.cmd_language,
            aliases=["/language", "/lang"],
            category=CommandCategory.OPTIONS,
            accepts_args=True,
        ))

        # /timezone
        self.registry.register(CommandDefinition(
            key="timezone",
            description="Set your timezone",
            handler=self.cmd_timezone,
            aliases=["/timezone", "/tz"],
            category=CommandCategory.OPTIONS,
            accepts_args=True,
        ))

        # /feedback
        self.registry.register(CommandDefinition(
            key="feedback",
            description="Send feedback to the bot developers",
            handler=self.cmd_feedback,
            aliases=["/feedback", "/fb"],
            category=CommandCategory.STATUS,
            accepts_args=True,
        ))

    def _register_group_commands(self) -> None:
        """Register group commands."""
        # /mention
        self.registry.register(CommandDefinition(
            key="mention",
            description="Set mention mode for group (on/off/reply)",
            handler=self.cmd_mention,
            aliases=["/mention"],
            category=CommandCategory.MANAGEMENT,
            accepts_args=True,
        ))

        # /quiet
        self.registry.register(CommandDefinition(
            key="quiet",
            description="Disable bot in group temporarily",
            handler=self.cmd_quiet,
            aliases=["/quiet", "/silence", "/mute"],
            category=CommandCategory.MANAGEMENT,
        ))

        # /resume
        self.registry.register(CommandDefinition(
            key="resume",
            description="Resume bot in group after quiet",
            handler=self.cmd_resume,
            aliases=["/resume", "/unmute"],
            category=CommandCategory.MANAGEMENT,
        ))

    def _register_admin_commands(self) -> None:
        """Register admin commands."""
        # /broadcast
        self.registry.register(CommandDefinition(
            key="broadcast",
            description="Send message to all users (admin only)",
            handler=self.cmd_broadcast,
            aliases=["/broadcast", "/announce"],
            category=CommandCategory.MANAGEMENT,
            accepts_args=True,
            metadata={"require_admin": True},
        ))

        # /stats
        self.registry.register(CommandDefinition(
            key="stats",
            description="Show usage statistics (admin only)",
            handler=self.cmd_stats,
            aliases=["/stats", "/statistics"],
            category=CommandCategory.STATUS,
            metadata={"require_admin": True},
        ))

        # /users
        self.registry.register(CommandDefinition(
            key="users",
            description="List active users (admin only)",
            handler=self.cmd_users,
            aliases=["/users"],
            category=CommandCategory.STATUS,
            metadata={"require_admin": True},
        ))

        # /ban
        self.registry.register(CommandDefinition(
            key="ban",
            description="Block a user (admin only)",
            handler=self.cmd_ban,
            aliases=["/ban", "/block"],
            category=CommandCategory.MANAGEMENT,
            accepts_args=True,
            metadata={"require_admin": True},
        ))

        # /unban
        self.registry.register(CommandDefinition(
            key="unban",
            description="Unblock a user (admin only)",
            handler=self.cmd_unban,
            aliases=["/unban", "/unblock"],
            category=CommandCategory.MANAGEMENT,
            accepts_args=True,
            metadata={"require_admin": True},
        ))

        # /config
        self.registry.register(CommandDefinition(
            key="config",
            description="Get or set configuration values (admin only)",
            handler=self.cmd_config,
            aliases=["/config", "/cfg"],
            category=CommandCategory.MANAGEMENT,
            accepts_args=True,
            metadata={"require_admin": True},
        ))

        # /reload
        self.registry.register(CommandDefinition(
            key="reload",
            description="Reload configuration (admin only)",
            handler=self.cmd_reload,
            aliases=["/reload"],
            category=CommandCategory.MANAGEMENT,
            metadata={"require_admin": True},
        ))

        # /debug
        self.registry.register(CommandDefinition(
            key="debug",
            description="Toggle debug mode (admin only)",
            handler=self.cmd_debug,
            aliases=["/debug"],
            category=CommandCategory.MANAGEMENT,
            accepts_args=True,
            hidden=True,
            metadata={"require_admin": True},
        ))

    # ==========================================================================
    # Command Execution
    # ==========================================================================

    async def execute(
        self,
        command_name: str,
        context: CommandContext,
    ) -> CommandResult:
        """
        Execute a command.

        Args:
            command_name: Command key or alias
            context: Command context

        Returns:
            CommandResult from the handler
        """
        # Get command
        command = self.registry.get(command_name)
        if not command:
            command = self.registry.get_by_alias(command_name)

        if not command:
            return CommandResult.fail(f"Unknown command: {command_name}")

        if not command.enabled:
            return CommandResult.fail(f"Command is disabled: {command_name}")

        # Check admin requirement
        if command.metadata.get("require_admin", False):
            if not self._is_admin(context):
                return CommandResult.fail("This command requires admin privileges")

        # Check if user is banned
        if self._is_banned(context.sender_id):
            return CommandResult.fail("You are blocked from using this bot")

        # Check if user has stopped
        if self._is_stopped(context.channel, context.sender_id):
            if command.key not in ("start", "resume"):
                return CommandResult.silent_ok()

        # Parse arguments if command accepts them
        if command.accepts_args and command.key in self._parsers:
            parser = self._parsers[command.key]
            parse_result = parser.parse(context.raw_args)
            if not parse_result.success:
                usage = parser.format_usage(command.key)
                return CommandResult.fail(
                    f"Invalid arguments: {', '.join(parse_result.errors)}\n"
                    f"Usage: {usage}"
                )
            context.parsed_args = parse_result.args

        # Execute handler
        try:
            if command.handler is None:
                return CommandResult.fail(f"No handler for command: {command_name}")

            result = command.handler(context)
            if asyncio.iscoroutine(result):
                result = await result

            return result

        except Exception as e:
            logger.exception(f"Error executing command {command_name}: {e}")
            return CommandResult.fail(f"Error: {str(e)}")

    def _is_admin(self, context: CommandContext) -> bool:
        """Check if context user is an admin."""
        if context.is_admin:
            return True
        return context.sender_id in self.admin_ids

    def _is_banned(self, sender_id: str) -> bool:
        """Check if user is banned."""
        return sender_id in self._banned_users

    def _is_stopped(self, channel: str, sender_id: str) -> bool:
        """Check if user has stopped the bot."""
        return (channel, sender_id) in self._stopped_users

    # ==========================================================================
    # User Command Handlers
    # ==========================================================================

    def cmd_help(self, ctx: CommandContext) -> CommandResult:
        """Handle /help command."""
        command_name = ctx.parsed_args.get("command")

        if command_name:
            # Help for specific command
            cmd = self.registry.get(command_name)
            if not cmd:
                cmd = self.registry.get_by_alias(command_name)

            if not cmd:
                return CommandResult.fail(f"Unknown command: {command_name}")

            # Build detailed help
            lines = [
                f"**{cmd.primary_alias}** - {cmd.description}",
                "",
            ]

            if cmd.aliases:
                lines.append(f"Aliases: {', '.join(cmd.aliases)}")

            if cmd.key in self._parsers:
                lines.append("")
                lines.append("Arguments:")
                lines.append(self._parsers[cmd.key].format_help())

            if cmd.metadata.get("require_admin"):
                lines.append("")
                lines.append("(Admin only)")

            return CommandResult.ok("\n".join(lines))

        else:
            # List all commands
            commands = self.registry.list_commands(include_hidden=False)

            # Group by category
            by_category: Dict[CommandCategory, List[CommandDefinition]] = {}
            for cmd in commands:
                if cmd.metadata.get("require_admin") and not self._is_admin(ctx):
                    continue
                if cmd.category not in by_category:
                    by_category[cmd.category] = []
                by_category[cmd.category].append(cmd)

            lines = [f"**{self.bot_name} Commands**", ""]

            for category in CommandCategory:
                if category not in by_category:
                    continue
                cmds = by_category[category]
                lines.append(f"**{category.value.title()}:**")
                for cmd in cmds:
                    lines.append(f"  {cmd.primary_alias} - {cmd.description}")
                lines.append("")

            lines.append("Use /help <command> for detailed help.")

            return CommandResult.ok("\n".join(lines))

    def cmd_start(self, ctx: CommandContext) -> CommandResult:
        """Handle /start command."""
        # Remove from stopped users
        key = (ctx.channel, ctx.sender_id)
        if key in self._stopped_users:
            self._stopped_users.discard(key)

        name = ctx.sender_name or "there"
        message = (
            f"Hello {name}! Welcome to {self.bot_name}.\n\n"
            f"I'm here to help you. Send me a message to get started.\n\n"
            f"Use /help to see available commands."
        )

        return CommandResult.ok(message)

    def cmd_stop(self, ctx: CommandContext) -> CommandResult:
        """Handle /stop command."""
        key = (ctx.channel, ctx.sender_id)
        self._stopped_users.add(key)

        return CommandResult.ok(
            f"You've stopped {self.bot_name}. "
            f"Use /start to receive messages again."
        )

    def cmd_status(self, ctx: CommandContext) -> CommandResult:
        """Handle /status command."""
        lines = [
            f"**{self.bot_name} Status**",
            "",
            f"Version: {self.bot_version}",
            f"Channel: {ctx.channel}",
            f"Debug Mode: {'On' if self._debug_mode else 'Off'}",
        ]

        if ctx.is_paired:
            lines.append(f"Paired: Yes (User ID: {ctx.user_id})")
        else:
            lines.append("Paired: No")

        if ctx.session and hasattr(ctx.session, "message_count"):
            lines.append(f"Messages in session: {ctx.session.message_count}")

        # Show user preferences
        user_key = (ctx.channel, ctx.sender_id)
        if user_key in self._model_store:
            lines.append(f"Model: {self._model_store[user_key]}")
        if user_key in self._language_store:
            lines.append(f"Language: {self._language_store[user_key]}")
        if user_key in self._timezone_store:
            lines.append(f"Timezone: {self._timezone_store[user_key]}")

        return CommandResult.ok("\n".join(lines))

    def cmd_pair(self, ctx: CommandContext) -> CommandResult:
        """Handle /pair command."""
        code = ctx.parsed_args.get("code", "")

        if not code:
            return CommandResult.fail(
                "Please provide a pairing code.\n"
                "Usage: /pair <code>"
            )

        if not self.pairing_manager:
            return CommandResult.fail(
                "Pairing is not configured. Please contact an administrator."
            )

        session = self.pairing_manager.verify_pairing(
            channel=ctx.channel,
            sender_id=ctx.sender_id,
            code=code,
        )

        if session:
            return CommandResult.ok(
                f"Pairing successful! Your account is now linked.\n"
                f"User ID: {session.user_id}",
                user_id=session.user_id,
                prompt_id=session.prompt_id,
            )
        else:
            return CommandResult.fail(
                "Invalid or expired pairing code. Please get a new code and try again."
            )

    def cmd_unpair(self, ctx: CommandContext) -> CommandResult:
        """Handle /unpair command."""
        if not self.pairing_manager:
            return CommandResult.fail("Pairing is not configured.")

        if not ctx.is_paired:
            return CommandResult.fail("You are not paired.")

        success = self.pairing_manager.unpair(ctx.channel, ctx.sender_id)

        if success:
            return CommandResult.ok(
                "Your account has been unpaired. "
                "You'll need to pair again to continue using the bot."
            )
        else:
            return CommandResult.fail("Failed to unpair account.")

    def cmd_clear(self, ctx: CommandContext) -> CommandResult:
        """Handle /clear command."""
        if ctx.session and hasattr(ctx.session, "clear_history"):
            ctx.session.clear_history()
            return CommandResult.ok("Conversation history cleared.")

        if self.session_manager:
            session = self.session_manager.get_session(
                ctx.channel, ctx.sender_id, create=False
            )
            if session:
                session.clear_history()
                return CommandResult.ok("Conversation history cleared.")

        return CommandResult.ok("No history to clear.")

    def cmd_history(self, ctx: CommandContext) -> CommandResult:
        """Handle /history command."""
        count = ctx.parsed_args.get("count", 10)

        session = ctx.session
        if not session and self.session_manager:
            session = self.session_manager.get_session(
                ctx.channel, ctx.sender_id, create=False
            )

        if not session or not hasattr(session, "messages"):
            return CommandResult.ok("No conversation history.")

        messages = session.messages[-count:]
        if not messages:
            return CommandResult.ok("No messages in history.")

        lines = [f"**Last {len(messages)} messages:**", ""]
        for msg in messages:
            role = "You" if msg.role == "user" else self.bot_name
            content = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
            lines.append(f"**{role}:** {content}")

        return CommandResult.ok("\n".join(lines))

    def cmd_model(self, ctx: CommandContext) -> CommandResult:
        """Handle /model command."""
        name = ctx.parsed_args.get("name")
        user_key = (ctx.channel, ctx.sender_id)

        if not name:
            # Show current model
            current = self._model_store.get(user_key, "default")
            return CommandResult.ok(f"Current model: {current}")

        # Set model
        self._model_store[user_key] = name
        return CommandResult.ok(f"Model set to: {name}")

    def cmd_language(self, ctx: CommandContext) -> CommandResult:
        """Handle /language command."""
        lang = ctx.parsed_args.get("language")
        user_key = (ctx.channel, ctx.sender_id)

        if not lang:
            current = self._language_store.get(user_key, "en")
            return CommandResult.ok(f"Current language: {current}")

        self._language_store[user_key] = lang
        return CommandResult.ok(f"Language set to: {lang}")

    def cmd_timezone(self, ctx: CommandContext) -> CommandResult:
        """Handle /timezone command."""
        tz = ctx.parsed_args.get("timezone")
        user_key = (ctx.channel, ctx.sender_id)

        if not tz:
            current = self._timezone_store.get(user_key, "UTC")
            return CommandResult.ok(f"Current timezone: {current}")

        self._timezone_store[user_key] = tz
        return CommandResult.ok(f"Timezone set to: {tz}")

    def cmd_feedback(self, ctx: CommandContext) -> CommandResult:
        """Handle /feedback command."""
        text = ctx.parsed_args.get("text")

        if not text:
            return CommandResult.fail(
                "Please provide feedback text.\n"
                "Usage: /feedback <your feedback>"
            )

        # Call feedback handler if set
        if self._feedback_handler:
            try:
                self._feedback_handler(ctx.sender_id, ctx.channel, text)
            except Exception as e:
                logger.error(f"Feedback handler error: {e}")

        logger.info(f"Feedback from {ctx.sender_id}: {text}")

        return CommandResult.ok(
            "Thank you for your feedback! We appreciate you taking the time to help us improve."
        )

    # ==========================================================================
    # Group Command Handlers
    # ==========================================================================

    def cmd_mention(self, ctx: CommandContext) -> CommandResult:
        """Handle /mention command."""
        if not ctx.is_group:
            return CommandResult.fail("This command only works in groups.")

        mode_str = ctx.parsed_args.get("mode", "").lower()

        mode_map = {
            "on": MentionMode.ALWAYS,
            "off": MentionMode.MENTION,
            "reply": MentionMode.COMMANDS_ONLY,
        }

        if mode_str not in mode_map:
            return CommandResult.fail(
                "Invalid mode. Use: on, off, or reply\n"
                "- on: Always respond\n"
                "- off: Only respond when mentioned\n"
                "- reply: Only respond to commands"
            )

        mode = mode_map[mode_str]
        self.mention_gate.set_mode(ctx.chat_id, mode)

        return CommandResult.ok(f"Mention mode set to: {mode_str}")

    def cmd_quiet(self, ctx: CommandContext) -> CommandResult:
        """Handle /quiet command."""
        if not ctx.is_group:
            return CommandResult.fail("This command only works in groups.")

        self.mention_gate.quiet(ctx.chat_id)

        return CommandResult.ok(
            f"{self.bot_name} is now quiet in this group. "
            f"Use /resume to re-enable."
        )

    def cmd_resume(self, ctx: CommandContext) -> CommandResult:
        """Handle /resume command."""
        if not ctx.is_group:
            return CommandResult.fail("This command only works in groups.")

        self.mention_gate.resume(ctx.chat_id)

        return CommandResult.ok(f"{self.bot_name} has resumed in this group.")

    # ==========================================================================
    # Admin Command Handlers
    # ==========================================================================

    async def cmd_broadcast(self, ctx: CommandContext) -> CommandResult:
        """Handle /broadcast command."""
        message = ctx.parsed_args.get("message")

        if not message:
            return CommandResult.fail(
                "Please provide a message to broadcast.\n"
                "Usage: /broadcast <message>"
            )

        if self._broadcast_handler:
            try:
                count = await self._broadcast_handler(message)
                return CommandResult.ok(f"Broadcast sent to {count} users.")
            except Exception as e:
                return CommandResult.fail(f"Broadcast failed: {e}")

        return CommandResult.ok(
            "Broadcast queued. (No broadcast handler configured)"
        )

    def cmd_stats(self, ctx: CommandContext) -> CommandResult:
        """Handle /stats command."""
        lines = [
            f"**{self.bot_name} Statistics**",
            "",
        ]

        # Session stats
        if self.session_manager:
            total = self.session_manager.get_session_count()
            lines.append(f"Active sessions: {total}")

        # Command stats from registry
        lines.append(f"Registered commands: {len(self.registry)}")

        # Other stats
        lines.append(f"Banned users: {len(self._banned_users)}")
        lines.append(f"Stopped users: {len(self._stopped_users)}")
        lines.append(f"Debug mode: {'On' if self._debug_mode else 'Off'}")

        return CommandResult.ok("\n".join(lines))

    def cmd_users(self, ctx: CommandContext) -> CommandResult:
        """Handle /users command."""
        if not self.session_manager:
            return CommandResult.ok("No session manager configured.")

        sessions = self.session_manager.list_sessions()[:20]  # Limit to 20

        if not sessions:
            return CommandResult.ok("No active users.")

        lines = ["**Active Users:**", ""]
        for session in sessions:
            paired = "Paired" if session.user_id else "Not paired"
            lines.append(f"- {session.channel}:{session.sender_id} ({paired})")

        if len(sessions) == 20:
            lines.append("... (showing first 20)")

        return CommandResult.ok("\n".join(lines))

    def cmd_ban(self, ctx: CommandContext) -> CommandResult:
        """Handle /ban command."""
        user = ctx.parsed_args.get("user")

        if not user:
            return CommandResult.fail(
                "Please specify a user to ban.\n"
                "Usage: /ban <user_id>"
            )

        if user in self._banned_users:
            return CommandResult.fail(f"User {user} is already banned.")

        self._banned_users.add(user)
        logger.info(f"Admin {ctx.sender_id} banned user {user}")

        return CommandResult.ok(f"User {user} has been banned.")

    def cmd_unban(self, ctx: CommandContext) -> CommandResult:
        """Handle /unban command."""
        user = ctx.parsed_args.get("user")

        if not user:
            return CommandResult.fail(
                "Please specify a user to unban.\n"
                "Usage: /unban <user_id>"
            )

        if user not in self._banned_users:
            return CommandResult.fail(f"User {user} is not banned.")

        self._banned_users.discard(user)
        logger.info(f"Admin {ctx.sender_id} unbanned user {user}")

        return CommandResult.ok(f"User {user} has been unbanned.")

    def cmd_config(self, ctx: CommandContext) -> CommandResult:
        """Handle /config command."""
        action = ctx.parsed_args.get("action")
        key = ctx.parsed_args.get("key")
        value = ctx.parsed_args.get("value")

        if action == "get":
            if key not in self.config_store:
                return CommandResult.fail(f"Config key not found: {key}")
            return CommandResult.ok(f"{key} = {self.config_store[key]}")

        elif action == "set":
            if not value:
                return CommandResult.fail(
                    "Please provide a value.\n"
                    "Usage: /config set <key> <value>"
                )
            self.config_store[key] = value
            logger.info(f"Admin {ctx.sender_id} set config {key}={value}")
            return CommandResult.ok(f"Set {key} = {value}")

        return CommandResult.fail("Invalid action. Use 'get' or 'set'.")

    def cmd_reload(self, ctx: CommandContext) -> CommandResult:
        """Handle /reload command."""
        # Placeholder for configuration reload
        logger.info(f"Admin {ctx.sender_id} triggered reload")

        return CommandResult.ok(
            "Configuration reloaded.\n"
            "(Note: Full reload requires restart for some settings)"
        )

    def cmd_debug(self, ctx: CommandContext) -> CommandResult:
        """Handle /debug command."""
        state = ctx.parsed_args.get("state")

        if state is None:
            return CommandResult.ok(f"Debug mode: {'On' if self._debug_mode else 'Off'}")

        self._debug_mode = state
        logger.info(f"Admin {ctx.sender_id} set debug mode to {state}")

        return CommandResult.ok(f"Debug mode: {'On' if state else 'Off'}")

    # ==========================================================================
    # Configuration Methods
    # ==========================================================================

    def set_feedback_handler(self, handler: Callable[[str, str, str], None]) -> None:
        """
        Set handler for feedback submissions.

        Args:
            handler: Function(sender_id, channel, text)
        """
        self._feedback_handler = handler

    def set_broadcast_handler(self, handler: Callable[[str], int]) -> None:
        """
        Set handler for broadcast messages.

        Args:
            handler: Async function(message) -> count
        """
        self._broadcast_handler = handler

    def add_admin(self, user_id: str) -> None:
        """Add an admin user."""
        self.admin_ids.add(user_id)

    def remove_admin(self, user_id: str) -> None:
        """Remove an admin user."""
        self.admin_ids.discard(user_id)

    def get_user_model(self, channel: str, sender_id: str) -> Optional[str]:
        """Get user's preferred model."""
        return self._model_store.get((channel, sender_id))

    def get_user_language(self, channel: str, sender_id: str) -> Optional[str]:
        """Get user's preferred language."""
        return self._language_store.get((channel, sender_id))

    def get_user_timezone(self, channel: str, sender_id: str) -> Optional[str]:
        """Get user's timezone."""
        return self._timezone_store.get((channel, sender_id))


# ==============================================================================
# Global Instance
# ==============================================================================

_builtin_commands: Optional[BuiltinCommands] = None


def get_builtin_commands() -> BuiltinCommands:
    """Get or create the global built-in commands instance."""
    global _builtin_commands
    if _builtin_commands is None:
        _builtin_commands = BuiltinCommands()
    return _builtin_commands


def reset_builtin_commands() -> None:
    """Reset the global built-in commands instance."""
    global _builtin_commands
    _builtin_commands = None


def register_builtin_commands(
    registry: Optional[CommandRegistry] = None,
    **kwargs,
) -> BuiltinCommands:
    """
    Register all built-in commands.

    Args:
        registry: Optional custom registry
        **kwargs: Additional arguments for BuiltinCommands

    Returns:
        BuiltinCommands instance
    """
    global _builtin_commands
    _builtin_commands = BuiltinCommands(registry=registry, **kwargs)
    _builtin_commands.register_all()
    return _builtin_commands
