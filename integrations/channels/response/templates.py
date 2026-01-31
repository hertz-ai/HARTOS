"""
TemplateEngine - Response formatting with variable substitution.

Provides template rendering with support for common variables like
{model}, {provider}, {identity.name}, {user.name}, {channel}, {timestamp}.
"""

import re
import time
from datetime import datetime
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


@dataclass
class Identity:
    """Bot identity information."""
    name: str = "Assistant"
    description: str = ""
    avatar_url: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class User:
    """User information."""
    name: str = "User"
    id: str = ""
    display_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TemplateContext:
    """Context for template rendering."""
    model: str = ""
    provider: str = ""
    identity: Identity = field(default_factory=Identity)
    user: User = field(default_factory=User)
    channel: str = ""
    timestamp: Optional[datetime] = None
    custom_vars: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert context to flat dictionary for variable substitution."""
        ts = self.timestamp or datetime.now()

        return {
            "model": self.model,
            "provider": self.provider,
            "identity.name": self.identity.name,
            "identity.description": self.identity.description,
            "identity.avatar_url": self.identity.avatar_url,
            "user.name": self.user.name or self.user.display_name,
            "user.id": self.user.id,
            "user.display_name": self.user.display_name or self.user.name,
            "channel": self.channel,
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp.date": ts.strftime("%Y-%m-%d"),
            "timestamp.time": ts.strftime("%H:%M:%S"),
            "timestamp.iso": ts.isoformat(),
            "timestamp.unix": str(int(ts.timestamp())),
            **self.custom_vars
        }


@dataclass
class TemplateConfig:
    """Configuration for the template engine."""
    prefix: str = ""
    suffix: str = ""
    default_format: str = "{content}"
    escape_html: bool = False
    strict_mode: bool = False  # Raise error on missing variables
    missing_var_placeholder: str = ""  # What to show for missing vars


class TemplateEngine:
    """
    Template engine for response formatting.

    Supports variable substitution using {variable} syntax,
    with support for nested variables like {identity.name}.
    """

    # Pattern to match {variable} or {variable.subvar}
    VAR_PATTERN = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\}')

    def __init__(self, config: Optional[TemplateConfig] = None):
        """
        Initialize the TemplateEngine.

        Args:
            config: Optional configuration for template behavior.
        """
        self._config = config or TemplateConfig()
        self._context = TemplateContext()
        self._custom_filters: Dict[str, Callable[[str], str]] = {}
        self._templates: Dict[str, str] = {}

    @property
    def config(self) -> TemplateConfig:
        """Get the template configuration."""
        return self._config

    @property
    def context(self) -> TemplateContext:
        """Get the current template context."""
        return self._context

    def set_context(self, context: TemplateContext) -> None:
        """Set the template context."""
        self._context = context

    def update_context(self, **kwargs) -> None:
        """Update specific context fields."""
        for key, value in kwargs.items():
            if hasattr(self._context, key):
                setattr(self._context, key, value)
            else:
                self._context.custom_vars[key] = value

    def set_model(self, model: str) -> None:
        """Set the model name."""
        self._context.model = model

    def set_provider(self, provider: str) -> None:
        """Set the provider name."""
        self._context.provider = provider

    def set_identity(self, identity: Identity) -> None:
        """Set the bot identity."""
        self._context.identity = identity

    def set_user(self, user: User) -> None:
        """Set the user information."""
        self._context.user = user

    def set_channel(self, channel: str) -> None:
        """Set the channel name."""
        self._context.channel = channel

    def set_prefix(self, prefix: str) -> None:
        """Set the response prefix."""
        self._config.prefix = prefix

    def set_suffix(self, suffix: str) -> None:
        """Set the response suffix."""
        self._config.suffix = suffix

    def set_variable(self, name: str, value: Any) -> None:
        """Set a custom variable."""
        self._context.custom_vars[name] = value

    def get_variable(self, name: str) -> Optional[Any]:
        """Get a variable value."""
        context_dict = self._context.to_dict()
        return context_dict.get(name, self._context.custom_vars.get(name))

    def register_template(self, name: str, template: str) -> None:
        """Register a named template for later use."""
        self._templates[name] = template

    def get_template(self, name: str) -> Optional[str]:
        """Get a registered template by name."""
        return self._templates.get(name)

    def list_templates(self) -> List[str]:
        """List all registered template names."""
        return list(self._templates.keys())

    def register_filter(self, name: str, func: Callable[[str], str]) -> None:
        """
        Register a custom filter function.

        Filters can be applied using {variable|filter} syntax.
        """
        self._custom_filters[name] = func

    def render(self, template: str, context: Optional[TemplateContext] = None,
               extra_vars: Optional[Dict[str, Any]] = None) -> str:
        """
        Render a template with variable substitution.

        Args:
            template: The template string with {variable} placeholders.
            context: Optional context to use (defaults to stored context).
            extra_vars: Additional variables to include.

        Returns:
            Rendered template string.

        Raises:
            ValueError: If strict_mode is True and a variable is missing.
        """
        ctx = context or self._context
        var_dict = ctx.to_dict()

        if extra_vars:
            var_dict.update(extra_vars)

        def replace_var(match):
            var_name = match.group(1)

            # Check for filter syntax: {var|filter}
            if '|' in var_name:
                var_name, filter_name = var_name.split('|', 1)
                value = var_dict.get(var_name)
                if value is not None and filter_name in self._custom_filters:
                    return self._custom_filters[filter_name](str(value))

            value = var_dict.get(var_name)

            if value is None:
                if self._config.strict_mode:
                    raise ValueError(f"Missing template variable: {var_name}")
                return self._config.missing_var_placeholder or match.group(0)

            result = str(value)
            if self._config.escape_html:
                result = self._escape_html(result)
            return result

        return self.VAR_PATTERN.sub(replace_var, template)

    def render_named(self, template_name: str, context: Optional[TemplateContext] = None,
                     extra_vars: Optional[Dict[str, Any]] = None) -> str:
        """
        Render a registered template by name.

        Args:
            template_name: Name of the registered template.
            context: Optional context to use.
            extra_vars: Additional variables to include.

        Returns:
            Rendered template string.

        Raises:
            KeyError: If template name is not found.
        """
        template = self._templates.get(template_name)
        if template is None:
            raise KeyError(f"Template not found: {template_name}")
        return self.render(template, context, extra_vars)

    def format_response(self, content: str, context: Optional[TemplateContext] = None,
                        extra_vars: Optional[Dict[str, Any]] = None) -> str:
        """
        Format a response with prefix, suffix, and variable substitution.

        Args:
            content: The main response content.
            context: Optional context to use.
            extra_vars: Additional variables to include.

        Returns:
            Formatted response string.
        """
        ctx = context or self._context
        vars_dict = {"content": content}
        if extra_vars:
            vars_dict.update(extra_vars)

        # Render prefix
        prefix = ""
        if self._config.prefix:
            prefix = self.render(self._config.prefix, ctx, vars_dict)

        # Render suffix
        suffix = ""
        if self._config.suffix:
            suffix = self.render(self._config.suffix, ctx, vars_dict)

        # Render content with format template
        formatted = self.render(self._config.default_format, ctx, vars_dict)

        return f"{prefix}{formatted}{suffix}"

    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters."""
        replacements = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;'
        }
        for char, escaped in replacements.items():
            text = text.replace(char, escaped)
        return text

    def get_available_variables(self) -> List[str]:
        """Get list of available variable names."""
        return list(self._context.to_dict().keys())

    def validate_template(self, template: str) -> Dict[str, Any]:
        """
        Validate a template string.

        Returns:
            Dictionary with validation results.
        """
        variables = self.VAR_PATTERN.findall(template)
        available = set(self._context.to_dict().keys())

        missing = [v for v in variables if v not in available and v not in self._context.custom_vars]

        return {
            "valid": len(missing) == 0,
            "variables_used": variables,
            "missing_variables": missing,
            "available_variables": list(available)
        }

    def create_context(
        self,
        model: str = "",
        provider: str = "",
        identity_name: str = "Assistant",
        user_name: str = "User",
        channel: str = "",
        **custom_vars
    ) -> TemplateContext:
        """
        Create a new template context with common values.

        Args:
            model: Model name.
            provider: Provider name.
            identity_name: Bot identity name.
            user_name: User name.
            channel: Channel name.
            **custom_vars: Additional custom variables.

        Returns:
            New TemplateContext instance.
        """
        return TemplateContext(
            model=model,
            provider=provider,
            identity=Identity(name=identity_name),
            user=User(name=user_name),
            channel=channel,
            timestamp=datetime.now(),
            custom_vars=custom_vars
        )

    def clone(self) -> 'TemplateEngine':
        """Create a copy of this template engine."""
        new_engine = TemplateEngine(TemplateConfig(
            prefix=self._config.prefix,
            suffix=self._config.suffix,
            default_format=self._config.default_format,
            escape_html=self._config.escape_html,
            strict_mode=self._config.strict_mode,
            missing_var_placeholder=self._config.missing_var_placeholder
        ))
        new_engine._context = TemplateContext(
            model=self._context.model,
            provider=self._context.provider,
            identity=Identity(
                name=self._context.identity.name,
                description=self._context.identity.description,
                avatar_url=self._context.identity.avatar_url
            ),
            user=User(
                name=self._context.user.name,
                id=self._context.user.id,
                display_name=self._context.user.display_name
            ),
            channel=self._context.channel,
            timestamp=self._context.timestamp,
            custom_vars=dict(self._context.custom_vars)
        )
        new_engine._custom_filters = dict(self._custom_filters)
        new_engine._templates = dict(self._templates)
        return new_engine

    # Built-in filters
    @staticmethod
    def filter_upper(value: str) -> str:
        """Convert to uppercase."""
        return value.upper()

    @staticmethod
    def filter_lower(value: str) -> str:
        """Convert to lowercase."""
        return value.lower()

    @staticmethod
    def filter_title(value: str) -> str:
        """Convert to title case."""
        return value.title()

    @staticmethod
    def filter_strip(value: str) -> str:
        """Strip whitespace."""
        return value.strip()

    def register_default_filters(self) -> None:
        """Register common built-in filters."""
        self.register_filter("upper", self.filter_upper)
        self.register_filter("lower", self.filter_lower)
        self.register_filter("title", self.filter_title)
        self.register_filter("strip", self.filter_strip)

    def get_stats(self) -> dict:
        """Get statistics about the template engine."""
        return {
            "registered_templates": len(self._templates),
            "custom_filters": len(self._custom_filters),
            "custom_variables": len(self._context.custom_vars),
            "prefix_set": bool(self._config.prefix),
            "suffix_set": bool(self._config.suffix)
        }
