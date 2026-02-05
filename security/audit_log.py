"""
Security Audit Logging
Filters sensitive data from log output and provides secure logging.
Prevents credential leakage via log files.
"""

import re
import logging
from typing import List, Tuple

# Patterns to redact from logs
_REDACTION_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # OpenAI API keys
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), '[REDACTED_OPENAI_KEY]'),
    # JWT tokens
    (re.compile(r'eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}'),
     '[REDACTED_JWT]'),
    # Google API keys
    (re.compile(r'AIzaSy[a-zA-Z0-9_-]{33}'), '[REDACTED_GOOGLE_KEY]'),
    # Groq API keys
    (re.compile(r'gsk_[a-zA-Z0-9]{20,}'), '[REDACTED_GROQ_KEY]'),
    # Generic Bearer tokens in logs
    (re.compile(r'Bearer\s+[a-zA-Z0-9_.-]{20,}'), 'Bearer [REDACTED]'),
    # Password values in key=value format
    (re.compile(r'(password|passwd|pwd|secret|token|api_key|apikey)\s*[=:]\s*\S+', re.I),
     r'\1=[REDACTED]'),
    # AWS keys
    (re.compile(r'AKIA[0-9A-Z]{16}'), '[REDACTED_AWS_KEY]'),
    # Generic hex tokens (40+ chars)
    (re.compile(r'\b[0-9a-f]{40,}\b'), '[REDACTED_HEX_TOKEN]'),
]


class SensitiveFilter(logging.Filter):
    """
    Logging filter that redacts sensitive data patterns.
    Attach to any logger handler to prevent credential leakage.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redact(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern, replacement in _REDACTION_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def get_secure_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Get a logger with the SensitiveFilter already attached.
    Use this instead of logging.getLogger() for security-critical modules.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Add filter if not already present
    if not any(isinstance(f, SensitiveFilter) for f in logger.filters):
        logger.addFilter(SensitiveFilter())

    return logger


def apply_sensitive_filter_to_all():
    """
    Apply SensitiveFilter to the root logger so all log output is redacted.
    Call this once at application startup.
    """
    root_logger = logging.getLogger()
    if not any(isinstance(f, SensitiveFilter) for f in root_logger.filters):
        root_logger.addFilter(SensitiveFilter())

    # Also add to all existing handlers
    for handler in root_logger.handlers:
        if not any(isinstance(f, SensitiveFilter) for f in handler.filters):
            handler.addFilter(SensitiveFilter())
