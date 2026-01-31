"""
Argument Parser

Provides argument parsing and validation for commands.
Ported from HevolveBot's command argument handling.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)
import re
import shlex
import logging

logger = logging.getLogger(__name__)


class ArgumentType(Enum):
    """Type of argument."""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    CHOICE = "choice"


@dataclass
class ArgumentChoice:
    """A choice option for an argument."""
    value: str
    label: Optional[str] = None

    def __post_init__(self):
        if self.label is None:
            self.label = self.value


@dataclass
class ArgumentDefinition:
    """
    Definition of a command argument.

    Attributes:
        name: Argument name
        description: Human-readable description
        arg_type: Type of argument
        required: Whether argument is required
        default: Default value if not provided
        choices: Valid choices for CHOICE type
        capture_remaining: Capture all remaining tokens
        validator: Custom validation function
    """
    name: str
    description: str = ""
    arg_type: ArgumentType = ArgumentType.STRING
    required: bool = False
    default: Any = None
    choices: List[ArgumentChoice] = field(default_factory=list)
    capture_remaining: bool = False
    validator: Optional[Callable[[Any], Tuple[bool, Optional[str]]]] = None

    def __post_init__(self):
        # Normalize choices
        normalized = []
        for choice in self.choices:
            if isinstance(choice, str):
                normalized.append(ArgumentChoice(value=choice))
            elif isinstance(choice, dict):
                normalized.append(ArgumentChoice(
                    value=choice.get("value", ""),
                    label=choice.get("label"),
                ))
            elif isinstance(choice, ArgumentChoice):
                normalized.append(choice)
        self.choices = normalized


@dataclass
class ParsedArgument:
    """A parsed argument value."""
    name: str
    raw_value: str
    parsed_value: Any
    arg_type: ArgumentType


@dataclass
class ParseResult:
    """Result of parsing command arguments."""
    success: bool
    args: Dict[str, Any] = field(default_factory=dict)
    raw_args: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    remaining: Optional[str] = None

    def get(self, name: str, default: Any = None) -> Any:
        """Get a parsed argument value."""
        return self.args.get(name, default)

    def has(self, name: str) -> bool:
        """Check if argument was provided."""
        return name in self.args


class ArgumentParser:
    """
    Parses command arguments.

    Features:
    - Positional argument parsing
    - Type conversion and validation
    - Choice validation
    - Required argument checking
    - Capture remaining text
    - Custom validators
    """

    def __init__(self, definitions: Optional[List[ArgumentDefinition]] = None):
        self.definitions = definitions or []

    def add_argument(self, definition: ArgumentDefinition) -> "ArgumentParser":
        """Add an argument definition."""
        self.definitions.append(definition)
        return self

    def add(
        self,
        name: str,
        description: str = "",
        arg_type: ArgumentType = ArgumentType.STRING,
        required: bool = False,
        default: Any = None,
        choices: Optional[List[Union[str, ArgumentChoice]]] = None,
        capture_remaining: bool = False,
        validator: Optional[Callable] = None,
    ) -> "ArgumentParser":
        """Add an argument with parameters."""
        self.add_argument(ArgumentDefinition(
            name=name,
            description=description,
            arg_type=arg_type,
            required=required,
            default=default,
            choices=choices or [],
            capture_remaining=capture_remaining,
            validator=validator,
        ))
        return self

    def parse(self, raw: Optional[str]) -> ParseResult:
        """
        Parse arguments from raw string.

        Args:
            raw: Raw argument string

        Returns:
            ParseResult with parsed values
        """
        result = ParseResult(success=True)

        if not self.definitions:
            # No definitions, just return raw
            if raw:
                result.remaining = raw.strip()
            return result

        # Tokenize input
        tokens = self._tokenize(raw)
        token_index = 0

        for definition in self.definitions:
            if token_index >= len(tokens):
                # No more tokens
                if definition.required:
                    result.success = False
                    result.errors.append(f"Missing required argument: {definition.name}")
                elif definition.default is not None:
                    result.args[definition.name] = definition.default
                continue

            if definition.capture_remaining:
                # Capture all remaining tokens
                remaining_tokens = tokens[token_index:]
                raw_value = " ".join(remaining_tokens)
                parsed = self._parse_value(raw_value, definition)

                if parsed is None:
                    result.success = False
                    result.errors.append(f"Invalid value for {definition.name}")
                else:
                    result.args[definition.name] = parsed
                    result.raw_args[definition.name] = raw_value

                token_index = len(tokens)
                break

            # Parse single token
            raw_value = tokens[token_index]
            parsed = self._parse_value(raw_value, definition)

            if parsed is None:
                if definition.required:
                    result.success = False
                    result.errors.append(f"Invalid value for {definition.name}: {raw_value}")
                elif definition.default is not None:
                    result.args[definition.name] = definition.default
            else:
                result.args[definition.name] = parsed
                result.raw_args[definition.name] = raw_value
                token_index += 1

        # Handle remaining tokens
        if token_index < len(tokens):
            result.remaining = " ".join(tokens[token_index:])

        # Check for missing required arguments
        for definition in self.definitions:
            if definition.required and definition.name not in result.args:
                result.success = False
                if f"Missing required argument: {definition.name}" not in result.errors:
                    result.errors.append(f"Missing required argument: {definition.name}")

        return result

    def _tokenize(self, raw: Optional[str]) -> List[str]:
        """Tokenize raw argument string."""
        if not raw:
            return []

        raw = raw.strip()
        if not raw:
            return []

        # Try shell-style tokenization (handles quotes)
        try:
            return shlex.split(raw)
        except ValueError:
            # Fallback to simple whitespace split
            return raw.split()

    def _parse_value(self, raw: str, definition: ArgumentDefinition) -> Any:
        """Parse a single value according to its definition."""
        if not raw:
            return definition.default

        # Type conversion
        try:
            if definition.arg_type == ArgumentType.STRING:
                parsed = raw

            elif definition.arg_type == ArgumentType.INTEGER:
                parsed = int(raw)

            elif definition.arg_type == ArgumentType.FLOAT:
                parsed = float(raw)

            elif definition.arg_type == ArgumentType.BOOLEAN:
                lower = raw.lower()
                if lower in ("true", "yes", "1", "on", "enable", "enabled"):
                    parsed = True
                elif lower in ("false", "no", "0", "off", "disable", "disabled"):
                    parsed = False
                else:
                    return None

            elif definition.arg_type == ArgumentType.CHOICE:
                # Validate against choices
                lower = raw.lower()
                for choice in definition.choices:
                    if choice.value.lower() == lower:
                        parsed = choice.value
                        break
                else:
                    return None

            else:
                parsed = raw

        except (ValueError, TypeError):
            return None

        # Custom validation
        if definition.validator:
            try:
                valid, error = definition.validator(parsed)
                if not valid:
                    logger.debug(f"Validation failed for {definition.name}: {error}")
                    return None
            except Exception as e:
                logger.debug(f"Validator error for {definition.name}: {e}")
                return None

        return parsed

    def format_usage(self, command_name: str = "command") -> str:
        """Format usage string for the command."""
        parts = [f"/{command_name}"]

        for definition in self.definitions:
            if definition.required:
                parts.append(f"<{definition.name}>")
            else:
                parts.append(f"[{definition.name}]")

        return " ".join(parts)

    def format_help(self) -> str:
        """Format help text for arguments."""
        if not self.definitions:
            return "No arguments."

        lines = []
        for definition in self.definitions:
            req = " (required)" if definition.required else ""
            type_str = definition.arg_type.value
            desc = definition.description or "No description"

            line = f"  {definition.name}: {desc} [{type_str}]{req}"

            if definition.choices:
                choices_str = ", ".join(c.value for c in definition.choices)
                line += f"\n    Choices: {choices_str}"

            if definition.default is not None:
                line += f"\n    Default: {definition.default}"

            lines.append(line)

        return "\n".join(lines)


def create_parser(*definitions: ArgumentDefinition) -> ArgumentParser:
    """Create a parser with the given definitions."""
    return ArgumentParser(list(definitions))


def validate_range(min_val: Optional[float] = None, max_val: Optional[float] = None):
    """Create a range validator."""
    def validator(value: Any) -> Tuple[bool, Optional[str]]:
        try:
            num = float(value)
            if min_val is not None and num < min_val:
                return False, f"Value must be >= {min_val}"
            if max_val is not None and num > max_val:
                return False, f"Value must be <= {max_val}"
            return True, None
        except (ValueError, TypeError):
            return False, "Value must be a number"
    return validator


def validate_pattern(pattern: str):
    """Create a regex pattern validator."""
    regex = re.compile(pattern)
    def validator(value: Any) -> Tuple[bool, Optional[str]]:
        if not isinstance(value, str):
            return False, "Value must be a string"
        if not regex.match(value):
            return False, f"Value must match pattern: {pattern}"
        return True, None
    return validator


def validate_length(min_len: Optional[int] = None, max_len: Optional[int] = None):
    """Create a length validator."""
    def validator(value: Any) -> Tuple[bool, Optional[str]]:
        if not isinstance(value, str):
            return False, "Value must be a string"
        if min_len is not None and len(value) < min_len:
            return False, f"Value must be at least {min_len} characters"
        if max_len is not None and len(value) > max_len:
            return False, f"Value must be at most {max_len} characters"
        return True, None
    return validator
