"""
Command System

Phase 3 of the HevolveBot integration plan.
Provides command registration, detection, parsing, and built-in commands.

Modules:
- registry: Command registration and lookup
- detection: Command detection in messages
- arguments: Argument parsing and validation
- mention_gating: Mention-based response gating
- builtin: 20+ built-in commands
"""

from .registry import (
    CommandRegistry,
    CommandDefinition,
    CommandScope,
    CommandCategory,
    get_command_registry,
    reset_command_registry,
)

from .detection import (
    CommandDetector,
    CommandDetectorConfig,
    DetectedCommand,
    get_command_detector,
    reset_command_detector,
)

from .arguments import (
    ArgumentParser,
    ArgumentDefinition,
    ArgumentType,
    ArgumentChoice,
    ParseResult,
    ParsedArgument,
    create_parser,
    validate_range,
    validate_pattern,
    validate_length,
)

from .mention_gating import (
    MentionGate,
    MentionMode,
    MentionGateConfig,
    MentionCheckResult,
    get_mention_gate,
    reset_mention_gate,
)

from .builtin import (
    BuiltinCommands,
    CommandContext,
    CommandResult,
    CommandHandler,
    get_builtin_commands,
    reset_builtin_commands,
    register_builtin_commands,
)

__all__ = [
    # Registry
    "CommandRegistry",
    "CommandDefinition",
    "CommandScope",
    "CommandCategory",
    "get_command_registry",
    "reset_command_registry",
    # Detection
    "CommandDetector",
    "CommandDetectorConfig",
    "DetectedCommand",
    "get_command_detector",
    "reset_command_detector",
    # Arguments
    "ArgumentParser",
    "ArgumentDefinition",
    "ArgumentType",
    "ArgumentChoice",
    "ParseResult",
    "ParsedArgument",
    "create_parser",
    "validate_range",
    "validate_pattern",
    "validate_length",
    # Mention Gating
    "MentionGate",
    "MentionMode",
    "MentionGateConfig",
    "MentionCheckResult",
    "get_mention_gate",
    "reset_mention_gate",
    # Built-in Commands
    "BuiltinCommands",
    "CommandContext",
    "CommandResult",
    "CommandHandler",
    "get_builtin_commands",
    "reset_builtin_commands",
    "register_builtin_commands",
]
