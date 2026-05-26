"""Canonical superadmin allowlist + central report-in URLs.

Single source of truth for the universal-peer-join + central-report
spec (see Nunba memory:
``project_universal_peer_join_central_report.md``).  Every install —
Nunba bundled, Docker, OS-via-ISO, embedded — ships this constant
identically.  No parallel copies allowed in main / admin_bp /
federation modules.

Two open user-confirmation items:
- ``sathish@hertai.com`` is likely a typo for ``sathish@hertzai.com``.
  Preserved verbatim until the user confirms.
- ``cortext@hertzai.com`` is likely a typo for ``cortex@hertzai.com``.
  Preserved verbatim until the user confirms.

Constitutional rule: this allowlist must NEVER be silently mutated by
self-evolution / autoresearch agents.  Edits flow through the signed
admin-config gate or a release bump, nothing else.
"""
from __future__ import annotations
from typing import FrozenSet, Tuple


# ─── Allowlist (verbatim, see module docstring for typo notes) ────────

SUPERADMIN_EMAILS: FrozenSet[str] = frozenset({
    'sathish@hertai.com',
    'giri@hertzai.com',
    'central@hevolve.ai',
    'sathish@hevolve.ai',
    'cortext@hertzai.com',
})


# ─── Central report-in URLs (where a fresh install posts identity) ───

# Primary genesis centrals.  An install reports its identity here on
# first successful peer-join, then opportunistically thereafter (every
# REPORT_INTERVAL_SEC).
SUPERADMIN_CENTRAL_URLS: Tuple[str, ...] = (
    'https://central.hevolve.ai',
)

# Fallback genesis centrals.  Tried only if every primary URL fails
# the report-in attempt — provides Azure-side redundancy when the
# `central.hevolve.ai` DNS or origin is unreachable.  azurekong is the
# same Kong gateway already used elsewhere in the stack (e.g. MiniCPM
# inference at azurekong.hertzai.com:8000), so an install reaching
# this URL is hitting our existing production infrastructure.
SUPERADMIN_FALLBACK_CENTRAL_URLS: Tuple[str, ...] = (
    'https://azurekong.hertzai.com',
)

# Combined view for consumers that want to know about ALL centrals
# regardless of priority (e.g. peer_discovery genesis seed list, where
# we want gossip to find either).  Order: primary first, then fallback.
ALL_CENTRAL_URLS: Tuple[str, ...] = (
    SUPERADMIN_CENTRAL_URLS + SUPERADMIN_FALLBACK_CENTRAL_URLS
)


# ─── Tunables (operator-overridable via env) ──────────────────────────

# How often a node re-reports identity to centrals after the first
# successful join.  Long-cadence — this is presence/inventory, not the
# realtime gossip path.
REPORT_INTERVAL_SEC = 3600  # 1 hour

# Per-attempt HTTP timeout (connect, read).  Centrals offline must not
# slow the boot path.
REPORT_TIMEOUT_CONNECT = 3.0
REPORT_TIMEOUT_READ = 8.0

# Outbox drain cadence — when a central was unreachable, the report
# lands in the outbox and the next loop tick retries.
REPORT_OUTBOX_RETRY_SEC = 300  # 5 minutes


def is_superadmin_email(email: str) -> bool:
    """Case-insensitive membership check."""
    if not email:
        return False
    return email.strip().lower() in {e.lower() for e in SUPERADMIN_EMAILS}


__all__ = [
    'SUPERADMIN_EMAILS',
    'SUPERADMIN_CENTRAL_URLS',
    'SUPERADMIN_FALLBACK_CENTRAL_URLS',
    'ALL_CENTRAL_URLS',
    'REPORT_INTERVAL_SEC',
    'REPORT_TIMEOUT_CONNECT',
    'REPORT_TIMEOUT_READ',
    'REPORT_OUTBOX_RETRY_SEC',
    'is_superadmin_email',
]
