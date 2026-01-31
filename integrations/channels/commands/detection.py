"""
Command Detection

Provides command detection in text messages.
Ported from HevolveBot's command detection pattern.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple, Pattern
import re
import logging

from .registry import CommandRegistry, CommandDefinition, get_command_registry

logger = logging.getLogger(__name__)


@dataclass
class DetectedCommand:
    """
    Result of detecting a command in text.

    Attributes:
        command: The detected command definition
        raw_command: Raw command text as typed
        args: Arguments after the command
        prefix: The prefix used (e.g., "/" or "!")
        full_match: Full matched text
        start_pos: Start position in original text
        end_pos: End position in original text
    """
    command: CommandDefinition
    raw_command: str
    args: Optional[str] = None
    prefix: str = "/"
    full_match: str = ""
    start_pos: int = 0
    end_pos: int = 0

    @property
    def has_args(self) -> bool:
        """Check if command has arguments."""
        return bool(self.args and self.args.strip())


@dataclass
class CommandDetectorConfig:
    """
    Configuration for command detection.

    Attributes:
        prefixes: Valid command prefixes (default: ["/", "!"])
        allow_inline: Allow commands mid-message (default: False)
        case_sensitive: Case-sensitive matching (default: False)
        normalize_colons: Treat "/cmd:" like "/cmd " (default: True)
        strip_bot_mention: Remove @botname from commands (default: True)
        bot_username: Bot username to strip from commands
    """
    prefixes: List[str] = field(default_factory=lambda: ["/", "!"])
    allow_inline: bool = False
    case_sensitive: bool = False
    normalize_colons: bool = True
    strip_bot_mention: bool = True
    bot_username: Optional[str] = None


class CommandDetector:
    """
    Detects commands in text messages.

    Features:
    - Multiple prefix support (/, !)
    - Alias resolution
    - Argument extraction
    - Bot mention handling
    - Colon normalization (/cmd: args)
    """

    def __init__(
        self,
        registry: Optional[CommandRegistry] = None,
        config: Optional[CommandDetectorConfig] = None,
    ):
        self.registry = registry or get_command_registry()
        self.config = config or CommandDetectorConfig()
        self._pattern: Optional[Pattern] = None
        self._pattern_valid = False

    def _build_pattern(self) -> Pattern:
        """Build regex pattern for command detection."""
        # Escape prefixes for regex
        escaped_prefixes = [re.escape(p) for p in self.config.prefixes]
        prefix_pattern = f"[{''.join(escaped_prefixes)}]"

        # Build pattern components
        # Command: prefix + word characters (letters, numbers, underscore, hyphen)
        # Args: optional whitespace or colon followed by remaining text
        pattern = rf"^({prefix_pattern})([a-zA-Z][a-zA-Z0-9_-]*)(?:[@]([^\s]+))?(?:[\s:]+(.*))?$"

        flags = 0 if self.config.case_sensitive else re.IGNORECASE
        return re.compile(pattern, flags | re.DOTALL)

    def _get_pattern(self) -> Pattern:
        """Get or build the detection pattern."""
        if self._pattern is None or not self._pattern_valid:
            self._pattern = self._build_pattern()
            self._pattern_valid = True
        return self._pattern

    def invalidate_pattern(self) -> None:
        """Invalidate cached pattern (call when config changes)."""
        self._pattern_valid = False

    def detect(self, text: str) -> Optional[DetectedCommand]:
        """
        Detect a command in text.

        Args:
            text: Text to check for commands

        Returns:
            DetectedCommand if found, None otherwise
        """
        if not text:
            return None

        text = text.strip()
        if not text:
            return None

        # Check if text starts with a valid prefix
        if not any(text.startswith(p) for p in self.config.prefixes):
            return None

        # Handle multi-line: only look at first line
        first_line = text.split("\n")[0].strip()

        # Match against pattern
        pattern = self._get_pattern()
        match = pattern.match(first_line)

        if not match:
            return None

        prefix = match.group(1)
        command_name = match.group(2)
        bot_mention = match.group(3)
        args = match.group(4)

        # Handle bot mention
        if bot_mention and self.config.strip_bot_mention:
            if self.config.bot_username:
                normalized_mention = bot_mention.lower()
                normalized_bot = self.config.bot_username.lower()
                if normalized_mention != normalized_bot:
                    # Not our bot, ignore command
                    return None

        # Normalize command name for lookup
        lookup_name = command_name if self.config.case_sensitive else command_name.lower()

        # Try to find command in registry
        command = self.registry.get_by_alias(f"{prefix}{lookup_name}")

        if not command:
            # Try without prefix (some aliases might not include it)
            command = self.registry.get_by_alias(lookup_name)

        if not command:
            return None

        # Check if command is enabled
        if not command.enabled:
            return None

        # Clean up args
        if args:
            args = args.strip()
            if not args:
                args = None

        # Check if command accepts args
        if args and not command.accepts_args:
            # Command doesn't accept args, ignore
            return None

        return DetectedCommand(
            command=command,
            raw_command=f"{prefix}{command_name}",
            args=args,
            prefix=prefix,
            full_match=match.group(0),
            start_pos=0,
            end_pos=len(match.group(0)),
        )

    def is_command(self, text: str) -> bool:
        """
        Check if text is a command.

        Args:
            text: Text to check

        Returns:
            True if text is a valid command
        """
        return self.detect(text) is not None

    def extract_command_name(self, text: str) -> Optional[str]:
        """
        Extract command name from text without full detection.

        Args:
            text: Text to extract from

        Returns:
            Command name if found, None otherwise
        """
        if not text:
            return None

        text = text.strip()

        # Check prefix
        if not any(text.startswith(p) for p in self.config.prefixes):
            return None

        # Extract first word after prefix
        first_line = text.split("\n")[0].strip()

        # Match command pattern
        for prefix in self.config.prefixes:
            if first_line.startswith(prefix):
                remaining = first_line[len(prefix):]
                # Get command name (up to space, colon, or @)
                match = re.match(r"([a-zA-Z][a-zA-Z0-9_-]*)", remaining)
                if match:
                    return match.group(1).lower()

        return None

    def normalize_command_text(self, text: str) -> str:
        """
        Normalize command text.

        - Strips leading/trailing whitespace
        - Normalizes colons to spaces
        - Strips bot mentions if configured

        Args:
            text: Text to normalize

        Returns:
            Normalized text
        """
        if not text:
            return text

        text = text.strip()

        # Handle colon normalization
        if self.config.normalize_colons:
            # Replace /cmd: with /cmd
            for prefix in self.config.prefixes:
                # Match /command: or /command:args
                colon_pattern = rf"^({re.escape(prefix)}[a-zA-Z][a-zA-Z0-9_-]*)\s*:\s*"
                text = re.sub(colon_pattern, r"\1 ", text, flags=re.IGNORECASE)

        # Handle bot mention stripping
        if self.config.strip_bot_mention and self.config.bot_username:
            # Remove @botname from /cmd@botname
            for prefix in self.config.prefixes:
                mention_pattern = rf"^({re.escape(prefix)}[a-zA-Z][a-zA-Z0-9_-]*)@{re.escape(self.config.bot_username)}"
                text = re.sub(mention_pattern, r"\1", text, flags=re.IGNORECASE)

        return text.strip()

    def list_prefixes(self) -> List[str]:
        """Get configured prefixes."""
        return list(self.config.prefixes)

    def add_prefix(self, prefix: str) -> None:
        """Add a command prefix."""
        if prefix not in self.config.prefixes:
            self.config.prefixes.append(prefix)
            self.invalidate_pattern()

    def remove_prefix(self, prefix: str) -> bool:
        """Remove a command prefix."""
        if prefix in self.config.prefixes and len(self.config.prefixes) > 1:
            self.config.prefixes.remove(prefix)
            self.invalidate_pattern()
            return True
        return False


# Global detector instance
_global_detector: Optional[CommandDetector] = None


def get_command_detector() -> CommandDetector:
    """Get the global command detector."""
    global _global_detector
    if _global_detector is None:
        _global_detector = CommandDetector()
    return _global_detector


def reset_command_detector() -> None:
    """Reset the global command detector."""
    global _global_detector
    _global_detector = None
