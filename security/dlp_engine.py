"""
DLP Engine — Data Loss Prevention for Outbound Calls

Scans text for PII patterns (email, phone, SSN, credit card) and
blocks or redacts before outbound API calls or tool invocations.

Integrated with MCP sandbox (validate_tool_call) to gate outbound data.

Usage:
    from security.dlp_engine import get_dlp_engine

    dlp = get_dlp_engine()
    findings = dlp.scan("Contact john@example.com or 555-123-4567")
    clean_text = dlp.redact("SSN is 123-45-6789")
    allowed, reason = dlp.check_outbound(text)
"""

import re
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger('hevolve_security')

# PII detection patterns
PII_PATTERNS = {
    # Bounded runs (RFC 5321: local part <= 64, domain <= 253): unbounded
    # '[...]+@' tried every start of a long '1.2.1.2...' run to its end --
    # 13 s on 100 KB, on every bridged event (egress review, 2026-09-28).
    'email': re.compile(
        r'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}'
        r'\.[A-Za-z]{2,24}\b'
    ),
    # An email written so '@' is not literal: URL-encoded (%40) or spelled
    # out ((at) / [at]) -- the same address, the same person.
    'email_obfuscated': re.compile(
        r'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._+-]{1,64}'
        r'(?:%40|\s{0,3}[\(\[]\s{0,3}at\s{0,3}[\)\]]\s{0,3})'
        r'[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}\b',
        re.IGNORECASE,
    ),
    # IPv6: a compressed form with '::' and at least three groups, or the
    # full eight groups -- not a clock time (12:30:45) or 'std::vector'.
    'ipv6_address': re.compile(
        r'(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}'
        r'(?::[0-9A-Fa-f]{1,4}){1,5}|(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4})'
        r'(?![\w:])'
    ),
    # Bounded by letters/digits, not \b: an underscore is a word character,
    # so \b let 'phone_4155550199' through whole.
    'phone': re.compile(
        r'(?<![A-Za-z0-9])(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}'
        r'(?![A-Za-z0-9])'
    ),
    'ssn': re.compile(
        r'\b\d{3}-\d{2}-\d{4}\b'
    ),
    'credit_card': re.compile(
        r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'
    ),
    'ip_address': re.compile(
        r'\b(?:\d{1,3}\.){3}\d{1,3}\b'
    ),
}

# Redaction replacements
_REDACT_MAP = {
    'email': '[EMAIL_REDACTED]',
    'email_obfuscated': '[EMAIL_REDACTED]',
    'ipv6_address': '[IP_REDACTED]',
    'phone': '[PHONE_REDACTED]',
    'ssn': '[SSN_REDACTED]',
    'credit_card': '[CC_REDACTED]',
    'ip_address': '[IP_REDACTED]',
}

# Known safe patterns (don't flag these)
_SAFE_PATTERNS = {
    'ip_address': frozenset({
        '127.0.0.1', '0.0.0.0', '255.255.255.255',
        '192.168.0.1', '10.0.0.1', '172.16.0.1',
    }),
}


class DLPEngine:
    """
    Data Loss Prevention engine.
    Scans, redacts, and gates PII in text before outbound transmission.
    """

    def __init__(self, enabled: bool = True,
                 block_on_pii: bool = True,
                 scan_types: Optional[List[str]] = None):
        """
        Args:
            enabled: Master switch for DLP scanning
            block_on_pii: If True, check_outbound blocks on PII. If False, only logs.
            scan_types: Which PII types to scan for (default: all)
        """
        self.enabled = enabled
        self.block_on_pii = block_on_pii
        self.scan_types = scan_types or list(PII_PATTERNS.keys())

    def scan(self, text: str) -> List[Tuple[str, str]]:
        """
        Scan text for PII patterns.

        Returns:
            List of (pii_type, matched_text) tuples
        """
        if not self.enabled or not text:
            return []

        findings = []
        for pii_type in self.scan_types:
            pattern = PII_PATTERNS.get(pii_type)
            if not pattern:
                continue
            for match in pattern.finditer(text):
                value = match.group()
                # Skip known-safe values
                safe_set = _SAFE_PATTERNS.get(pii_type, frozenset())
                if value in safe_set:
                    continue
                findings.append((pii_type, value))

        if findings:
            types_found = set(f[0] for f in findings)
            logger.warning(f"DLP: found {len(findings)} PII items ({types_found})")

        return findings

    def redact(self, text: str) -> str:
        """
        Redact all PII from text.

        Returns:
            Text with PII replaced by type-specific placeholders
        """
        if not self.enabled or not text:
            return text

        result = text
        for pii_type in self.scan_types:
            pattern = PII_PATTERNS.get(pii_type)
            replacement = _REDACT_MAP.get(pii_type, '[REDACTED]')
            if pattern:
                result = pattern.sub(replacement, result)
        return result

    def check_outbound(self, text: str) -> Tuple[bool, str]:
        """
        Gate function for outbound data.

        Returns:
            (allowed, reason)
            - (True, '') if no PII found
            - (False, reason) if PII found and block_on_pii is True
            - (True, warning) if PII found but block_on_pii is False (log-only mode)
        """
        findings = self.scan(text)
        if not findings:
            return True, ''

        types_found = sorted(set(f[0] for f in findings))
        reason = f"PII detected: {', '.join(types_found)} ({len(findings)} items)"

        if self.block_on_pii:
            logger.warning(f"DLP BLOCKED outbound: {reason}")
            return False, reason

        logger.info(f"DLP WARNING (non-blocking): {reason}")
        return True, reason


# Singleton
_dlp_engine = None


def get_dlp_engine() -> DLPEngine:
    global _dlp_engine
    if _dlp_engine is None:
        _dlp_engine = DLPEngine()
    return _dlp_engine
