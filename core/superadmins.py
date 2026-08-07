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


# ─── Reachable-central resolution ─────────────────────────────────────
#
# Which central can this node actually TALK to right now?  Probed live
# 2026-08-07: central.hevolve.ai has been DNS-dead for months while
# azurekong.hertzai.com serves the full social API — yet flat desktops
# resolved their sync parent from env vars alone, so the entire fleet
# (147 registered nodes) queued public posts that never drained
# anywhere.  This resolver is the ONE place that answers "first alive
# central, in priority order"; superadmin_report keeps its own walk
# because its outbox semantics differ (it must try EVERY central, not
# stop at the first alive one).
#
# Probe target is /api/social/peers/health — the same endpoint
# SyncEngine.is_connected_to gates the drain on, so "resolved" and
# "drainable" can never disagree about what alive means.

_PROBE_TTL_OK = 600.0     # remember an alive central for 10 min
_PROBE_TTL_FAIL = 60.0    # re-probe a dead fleet after 1 min
_PROBE_TIMEOUT = 4.0

_resolve_cache = {'url': '', 'expires': 0.0}


def resolve_reachable_central(force: bool = False) -> str:
    """First central in ``ALL_CENTRAL_URLS`` whose peers/health answers 200.

    Returns '' when none are reachable (offline installs keep exactly
    their current no-parent behaviour).  Cached module-wide so callers
    on the sync/profile cadence never stack probes; ``force=True``
    bypasses the cache (tests, admin diagnostics).
    """
    import time
    now = time.monotonic()
    if not force and now < _resolve_cache['expires']:
        return _resolve_cache['url']
    url_found = ''
    try:
        from core.http_pool import pooled_get
        for candidate in ALL_CENTRAL_URLS:
            try:
                resp = pooled_get(
                    f"{candidate.rstrip('/')}/api/social/peers/health",
                    timeout=_PROBE_TIMEOUT,
                )
                if resp.status_code == 200:
                    url_found = candidate.rstrip('/')
                    break
            except Exception:
                continue
    except Exception:
        # http_pool unavailable (degraded cx_Freeze early boot) — report
        # unreachable rather than guessing; next call re-probes.
        url_found = ''
    _resolve_cache['url'] = url_found
    _resolve_cache['expires'] = now + (
        _PROBE_TTL_OK if url_found else _PROBE_TTL_FAIL)
    return url_found


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
    'resolve_reachable_central',
]
