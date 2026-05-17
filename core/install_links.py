"""
core.install_links — canonical install-link source of truth.

Single home for the (target_device, locale) -> install URL mapping.
Used by:
  - integrations/channels/agent_tools.py::send_install_link  (LLM tool)
  - docs/downloads.md                                        (human docs)
  - landing-page download CTAs (via /api/social/install-links if added)

Per CLAUDE.md Gate 2 (DRY): NEVER inline install URLs anywhere else.
Import from here.  If a URL changes, change ONE line.

History:
  - 2026-04-28: created.  Replaces the legacy
    `HevolveAI_Agent_Companion_Setup.exe` ad-hoc string and the
    scattered `play.google.com/.../com.hertzai.hevolve` references.
    Canonical URLs sourced from HARTOS/docs/downloads.md.

Security:
  - All URLs in CANONICAL_INSTALL_LINKS are checked into the repo
    and reviewed.  The `send_install_link` agent tool accepts an
    `install_link` override ONLY if the host matches `ALLOWED_HOSTS`
    — this prevents prompt-injection from steering users to a
    typosquat / phishing URL.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple
from urllib.parse import urlparse


# ─── Target devices the agent can hand off to ──────────────────────

SUPPORTED_DEVICES: Tuple[str, ...] = (
    'android',
    'ios',
    'windows',
    'macos',
    'linux',
)

# ─── Channels through which install links may be sent ──────────────
#
# These are the channel_type values accepted by the agent tool.
# Must be a subset of the registered channel adapters
# (integrations/channels/registry.py); validated at call time.

SUPPORTED_INSTALL_CHANNELS: Tuple[str, ...] = (
    'telegram',
    'discord',
    'whatsapp',
    'slack',
    'signal',
    'web',          # crossbar/in-app push
    'email',
)


# ─── Canonical install URLs ─────────────────────────────────────────
#
# Keyed by (target_device, locale).  Locale 'default' is the global
# fallback; per-locale entries override only when present (e.g. a
# China-mainland mirror, an India-specific Play Store URL).

CANONICAL_INSTALL_LINKS: Dict[Tuple[str, str], str] = {
    # Windows — GitHub release, Azure-Trusted-Signing-signed
    ('windows', 'default'):
        'https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.exe',
    # macOS — notarized DMG
    ('macos', 'default'):
        'https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.dmg',
    # Linux — AppImage works on every distro
    ('linux', 'default'):
        'https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba-x86_64.AppImage',
    # Android — Google Play (Hevolve Droid is the user-facing brand)
    ('android', 'default'):
        'https://play.google.com/store/apps/details?id=com.hertzai.hevolve',
    # iOS — TestFlight not yet public; provide a coming-soon page
    ('ios', 'default'):
        'https://hevolve.ai/ios-coming-soon',
}


# ─── Allowed hosts for the `install_link` override parameter ───────
#
# When the agent / caller supplies a custom URL, it MUST resolve to
# one of these hosts.  Anything else is rejected as a potential
# prompt-injection payload.

ALLOWED_HOSTS: Tuple[str, ...] = (
    'github.com',                   # release artifacts (raw + browser)
    'objects.githubusercontent.com',  # GitHub's CDN for release assets
    'play.google.com',              # Android
    'apps.apple.com',               # iOS App Store (future)
    'hevolve.ai',                   # marketing landing
    'docs.hevolve.ai',              # docs mirror
    'testflight.apple.com',         # iOS TestFlight (future)
)


# ─── Public API ────────────────────────────────────────────────────

def get_install_link(
    target_device: str,
    locale: str = 'default',
) -> Optional[str]:
    """Return the canonical install URL for a target device + locale.

    Args:
        target_device: one of SUPPORTED_DEVICES
        locale: BCP-47 language tag (e.g. 'en', 'hi', 'zh') or 'default'

    Returns:
        URL string, or None if (device, locale) has no entry AND no
        'default' fallback exists.
    """
    device = (target_device or '').lower().strip()
    if device not in SUPPORTED_DEVICES:
        return None

    # Try locale-specific first, then fall back to 'default'
    url = CANONICAL_INSTALL_LINKS.get((device, locale))
    if url:
        return url
    return CANONICAL_INSTALL_LINKS.get((device, 'default'))


def is_allowed_install_link(url: str) -> bool:
    """Verify a candidate install URL resolves to an allowed host.

    Used to gate the `install_link` override on the agent tool — a
    misbehaving or prompt-injected agent MUST NOT be able to send an
    arbitrary URL to a user's Telegram/Discord/etc.

    Args:
        url: candidate URL string

    Returns:
        True iff the parsed netloc matches one of ALLOWED_HOSTS
        (exact match or proper subdomain).
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.netloc or '').lower()
    # strip port if present
    host = host.split(':', 1)[0]
    if not host:
        return False
    for allowed in ALLOWED_HOSTS:
        if host == allowed or host.endswith('.' + allowed):
            return True
    return False


def is_supported_device(device: str) -> bool:
    """True iff `device` is one of SUPPORTED_DEVICES (case-insensitive)."""
    return (device or '').lower().strip() in SUPPORTED_DEVICES


def is_supported_install_channel(channel_type: str) -> bool:
    """True iff `channel_type` is one of SUPPORTED_INSTALL_CHANNELS."""
    return (channel_type or '').lower().strip() in SUPPORTED_INSTALL_CHANNELS
