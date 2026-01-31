"""
Response Module - Handles response formatting, streaming, and interactions.

This module provides:
- TypingManager: Typing indicators for chat channels
- AckManager: Acknowledgment reactions for messages
- TemplateEngine: Response formatting with variable substitution
- StreamingResponse: Streaming LLM responses with message editing
"""

from .typing import (
    TypingManager,
    TypingConfig,
    TypingContext,
    TypingState,
)

from .reactions import (
    AckManager,
    AckConfig,
    AckContext,
    AckState,
)

from .templates import (
    TemplateEngine,
    TemplateConfig,
    TemplateContext,
    Identity,
    User,
)

from .streaming import (
    StreamingResponse,
    FallbackStreamingResponse,
    StreamConfig,
    StreamContext,
    StreamState,
    PlatformCapability,
    ProgressIndicator,
    PLATFORM_CAPABILITIES,
    create_streaming_response,
)

__all__ = [
    # Typing
    "TypingManager",
    "TypingConfig",
    "TypingContext",
    "TypingState",
    # Reactions
    "AckManager",
    "AckConfig",
    "AckContext",
    "AckState",
    # Templates
    "TemplateEngine",
    "TemplateConfig",
    "TemplateContext",
    "Identity",
    "User",
    # Streaming
    "StreamingResponse",
    "FallbackStreamingResponse",
    "StreamConfig",
    "StreamContext",
    "StreamState",
    "PlatformCapability",
    "ProgressIndicator",
    "PLATFORM_CAPABILITIES",
    "create_streaming_response",
]
