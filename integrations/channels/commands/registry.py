"""
Command Registry

Provides the command registration and lookup system.
Ported from HevolveBot's commands-registry pattern.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Union,
)
import re
import logging

logger = logging.getLogger(__name__)


class CommandScope(Enum):
    """Scope where command is available."""
    TEXT = "text"       # Only available as text command (e.g., /help)
    NATIVE = "native"   # Only available as native command (platform-specific)
    BOTH = "both"       # Available in both text and native forms


class CommandCategory(Enum):
    """Category of command for grouping in help."""
    SESSION = "session"
    OPTIONS = "options"
    STATUS = "status"
    MANAGEMENT = "management"
    MEDIA = "media"
    TOOLS = "tools"
    CUSTOM = "custom"


@dataclass
class CommandDefinition:
    """
    Definition of a command.

    Attributes:
        key: Unique command identifier (e.g., "help", "model")
        description: Human-readable description
        handler: Async callable that handles the command
        aliases: List of text aliases (e.g., ["/help", "/h", "/?"])
        native_name: Name for native platform commands
        scope: Where the command is available
        category: Category for help grouping
        accepts_args: Whether command accepts arguments
        hidden: Whether to hide from help listings
        enabled: Whether command is currently enabled
        metadata: Additional command metadata
    """
    key: str
    description: str
    handler: Optional[Callable] = None
    aliases: List[str] = field(default_factory=list)
    native_name: Optional[str] = None
    scope: CommandScope = CommandScope.BOTH
    category: CommandCategory = CommandCategory.CUSTOM
    accepts_args: bool = False
    hidden: bool = False
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure aliases are normalized
        normalized = []
        for alias in self.aliases:
            alias = alias.strip().lower()
            if alias and alias not in normalized:
                normalized.append(alias)
        self.aliases = normalized

        # Auto-add /key as alias if no aliases provided
        if not self.aliases and self.scope != CommandScope.NATIVE:
            self.aliases = [f"/{self.key}"]

    @property
    def primary_alias(self) -> str:
        """Get the primary text alias."""
        if self.aliases:
            return self.aliases[0]
        return f"/{self.key}"


class CommandRegistry:
    """
    Registry for managing commands.

    Provides:
    - Command registration and unregistration
    - Alias resolution
    - Command lookup by key or alias
    - Category-based listing
    """

    def __init__(self):
        self._commands: Dict[str, CommandDefinition] = {}
        self._alias_map: Dict[str, str] = {}  # alias -> key
        self._native_map: Dict[str, str] = {}  # native_name -> key

    def register(self, command: CommandDefinition) -> None:
        """
        Register a command.

        Args:
            command: Command definition to register

        Raises:
            ValueError: If command key or alias conflicts with existing
        """
        # Check for key conflict
        if command.key in self._commands:
            raise ValueError(f"Command key already registered: {command.key}")

        # Check for alias conflicts
        for alias in command.aliases:
            normalized = alias.strip().lower()
            if normalized in self._alias_map:
                existing_key = self._alias_map[normalized]
                raise ValueError(
                    f"Alias '{alias}' already registered for command '{existing_key}'"
                )

        # Check native name conflict
        if command.native_name:
            normalized = command.native_name.strip().lower()
            if normalized in self._native_map:
                existing_key = self._native_map[normalized]
                raise ValueError(
                    f"Native name '{command.native_name}' already registered for command '{existing_key}'"
                )

        # Register command
        self._commands[command.key] = command

        # Register aliases
        for alias in command.aliases:
            normalized = alias.strip().lower()
            self._alias_map[normalized] = command.key

        # Register native name
        if command.native_name:
            normalized = command.native_name.strip().lower()
            self._native_map[normalized] = command.key

        logger.debug(f"Registered command: {command.key}")

    def unregister(self, key: str) -> bool:
        """
        Unregister a command.

        Args:
            key: Command key to unregister

        Returns:
            True if command was unregistered, False if not found
        """
        if key not in self._commands:
            return False

        command = self._commands[key]

        # Remove aliases
        for alias in command.aliases:
            normalized = alias.strip().lower()
            self._alias_map.pop(normalized, None)

        # Remove native name
        if command.native_name:
            normalized = command.native_name.strip().lower()
            self._native_map.pop(normalized, None)

        # Remove command
        del self._commands[key]

        logger.debug(f"Unregistered command: {key}")
        return True

    def get(self, key: str) -> Optional[CommandDefinition]:
        """Get command by key."""
        return self._commands.get(key)

    def get_by_alias(self, alias: str) -> Optional[CommandDefinition]:
        """
        Get command by text alias.

        Args:
            alias: Text alias (e.g., "/help" or "help")

        Returns:
            CommandDefinition if found, None otherwise
        """
        normalized = alias.strip().lower()

        # Try direct lookup
        key = self._alias_map.get(normalized)
        if key:
            return self._commands.get(key)

        # Try with leading slash
        if not normalized.startswith("/"):
            key = self._alias_map.get(f"/{normalized}")
            if key:
                return self._commands.get(key)

        return None

    def get_by_native_name(self, name: str) -> Optional[CommandDefinition]:
        """
        Get command by native name.

        Args:
            name: Native command name

        Returns:
            CommandDefinition if found, None otherwise
        """
        normalized = name.strip().lower()
        key = self._native_map.get(normalized)
        if key:
            return self._commands.get(key)
        return None

    def resolve_alias(self, alias: str) -> Optional[str]:
        """
        Resolve an alias to its command key.

        Args:
            alias: Text alias

        Returns:
            Command key if found, None otherwise
        """
        normalized = alias.strip().lower()

        # Try direct lookup
        if normalized in self._alias_map:
            return self._alias_map[normalized]

        # Try with leading slash
        if not normalized.startswith("/"):
            prefixed = f"/{normalized}"
            if prefixed in self._alias_map:
                return self._alias_map[prefixed]

        return None

    def add_alias(self, key: str, alias: str) -> bool:
        """
        Add an alias to an existing command.

        Args:
            key: Command key
            alias: New alias to add

        Returns:
            True if alias added, False if command not found or alias conflicts
        """
        if key not in self._commands:
            return False

        normalized = alias.strip().lower()

        # Check for conflict
        if normalized in self._alias_map:
            return False

        # Add alias
        self._alias_map[normalized] = key
        self._commands[key].aliases.append(normalized)

        return True

    def remove_alias(self, alias: str) -> bool:
        """
        Remove an alias.

        Args:
            alias: Alias to remove

        Returns:
            True if removed, False if not found
        """
        normalized = alias.strip().lower()

        if normalized not in self._alias_map:
            return False

        key = self._alias_map[normalized]

        # Don't remove if it's the only alias
        command = self._commands.get(key)
        if command and len(command.aliases) <= 1:
            return False

        # Remove alias
        del self._alias_map[normalized]
        if command:
            command.aliases = [a for a in command.aliases if a != normalized]

        return True

    def list_commands(
        self,
        category: Optional[CommandCategory] = None,
        scope: Optional[CommandScope] = None,
        include_hidden: bool = False,
        include_disabled: bool = False,
    ) -> List[CommandDefinition]:
        """
        List registered commands.

        Args:
            category: Filter by category
            scope: Filter by scope
            include_hidden: Include hidden commands
            include_disabled: Include disabled commands

        Returns:
            List of matching command definitions
        """
        commands = []

        for command in self._commands.values():
            # Filter by enabled
            if not include_disabled and not command.enabled:
                continue

            # Filter by hidden
            if not include_hidden and command.hidden:
                continue

            # Filter by category
            if category is not None and command.category != category:
                continue

            # Filter by scope
            if scope is not None and command.scope != scope:
                continue

            commands.append(command)

        # Sort by key
        commands.sort(key=lambda c: c.key)

        return commands

    def list_aliases(self) -> Dict[str, str]:
        """Get all aliases mapped to command keys."""
        return dict(self._alias_map)

    def list_native_names(self) -> Dict[str, str]:
        """Get all native names mapped to command keys."""
        return dict(self._native_map)

    def has_command(self, key: str) -> bool:
        """Check if a command is registered."""
        return key in self._commands

    def has_alias(self, alias: str) -> bool:
        """Check if an alias is registered."""
        normalized = alias.strip().lower()
        return normalized in self._alias_map

    def enable_command(self, key: str) -> bool:
        """Enable a command."""
        if key in self._commands:
            self._commands[key].enabled = True
            return True
        return False

    def disable_command(self, key: str) -> bool:
        """Disable a command."""
        if key in self._commands:
            self._commands[key].enabled = False
            return True
        return False

    def clear(self) -> None:
        """Clear all registered commands."""
        self._commands.clear()
        self._alias_map.clear()
        self._native_map.clear()

    def __len__(self) -> int:
        """Get number of registered commands."""
        return len(self._commands)

    def __contains__(self, key: str) -> bool:
        """Check if command key is registered."""
        return key in self._commands

    def __iter__(self):
        """Iterate over command definitions."""
        return iter(self._commands.values())


# Global registry instance
_global_registry: Optional[CommandRegistry] = None


def get_command_registry() -> CommandRegistry:
    """Get the global command registry."""
    global _global_registry
    if _global_registry is None:
        _global_registry = CommandRegistry()
    return _global_registry


def reset_command_registry() -> None:
    """Reset the global command registry."""
    global _global_registry
    _global_registry = None
