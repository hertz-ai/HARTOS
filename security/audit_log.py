"""
Security Audit Logging
Filters sensitive data from log output and provides secure logging.
Prevents credential leakage via log files.
"""

import re
import sys
import logging
from typing import List, Tuple, Optional

# Vendor API-key / token / password / PEM redaction is delegated to the ONE
# canonical pattern set in security/secret_redactor.py (see _redact below).
# This module used to carry its OWN copy of those regexes and they DRIFTED: its
# Google pattern required an 'AIzaSy' prefix (AIzaSy…{33}) and its OpenAI pattern
# was sk-[alnum]{20,}, so Google keys not starting 'Sy' and every sk-proj-/
# sk-ant- key LEAKED into the audit log. Delegating to the canonical fixes that
# and inherits its far broader vendor coverage.
#
# The ONE audit-log-specific supplemental below stays local on purpose: audit
# logs deliberately over-redact bare long-hex tokens (SHA/HMAC/raw tokens with no
# keyword prefix), whereas the canonical gates hex on a keyword to avoid mangling
# hashes in the hive-privacy path.
_AUDIT_EXTRA_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'\b[0-9a-f]{40,}\b'), '[REDACTED_HEX_TOKEN]'),
]

# ── Canonical-redactor health, surfaced instead of swallowed ────────────────
# If the canonical redactor ever fails, the ONLY remaining coverage is the bare
# long-hex pattern above — every vendor API key (sk-, sk-proj-, sk-ant-, AIza,
# gsk_, AKIA, Bearer, PEM) would flow into the audit log verbatim. That is a
# security-relevant degradation, so it must never be silent.
#
# WHY THIS DOES NOT CALL logger.warning(): this code runs INSIDE a logging.Filter.
# Emitting a log record from a filter re-enters the logging machinery, which runs
# the filters again — unbounded recursion on the very path that is already broken.
# So the signal goes straight to stderr ONCE, and the state is left readable via
# `canonical_redactor_status()` for health surfaces that poll rather than listen.
_redactor_failure: Optional[str] = None
_redactor_failure_count: int = 0


def canonical_redactor_status() -> Tuple[bool, Optional[str], int]:
    """Return ``(healthy, first_error_repr, failure_count)``.

    ``healthy is False`` means audit-log redaction has DEGRADED to the local
    long-hex pattern only and vendor keys are no longer being scrubbed.
    """
    return (_redactor_failure is None, _redactor_failure, _redactor_failure_count)


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
        # Canonical vendor-secret redaction — ONE source of truth
        # (security/secret_redactor.py). Still best-effort, because a redaction
        # filter must never raise and drop a log line — but the failure is now
        # RECORDED and announced once, never swallowed. See the module header for
        # why this cannot use the logging system to report a logging fault.
        global _redactor_failure, _redactor_failure_count
        try:
            from security.secret_redactor import redact_secrets
            text = redact_secrets(text)[0]
        except Exception as exc:            # noqa: BLE001 - see above; must not raise
            _redactor_failure_count += 1
            if _redactor_failure is None:
                _redactor_failure = repr(exc)
                try:
                    sys.stderr.write(
                        "[audit_log] CANONICAL REDACTOR FAILED (%r) — audit-log "
                        "redaction has DEGRADED to the long-hex pattern only; "
                        "vendor API keys are NO LONGER being scrubbed.\n" % (exc,))
                except Exception:
                    # stderr itself is gone (closed/detached). The flag above is
                    # still set, so canonical_redactor_status() remains truthful.
                    pass
        for pattern, replacement in _AUDIT_EXTRA_PATTERNS:
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
