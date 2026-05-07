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


# ─── Custom-scheme deep links (UNIF-G4) ─────────────────────────────
#
# Distinct from `is_allowed_install_link` (HTTPS-only) above — these
# helpers cover the OS-registered custom schemes used by the Nunba
# desktop / Android / iOS protocol handlers to receive invite-accept,
# meet-join, and group-join intents from external apps (browser,
# Telegram, Discord, OS file manager, etc.).
#
# Schemes (all three accepted on the receive side; builders default
# to ``hevolveai`` for the canonical cross-platform link):
#   - hevolveai://   Nunba desktop registers this since 2024;
#                    canonical for agent-emitted links.
#   - nunba://       UNIF-G4 brand-canon scheme; iOS + desktop accept.
#   - hevolve://     Legacy mobile scheme; Android + iOS Info.plist
#                    have registered it since the early Hevolve_RN
#                    builds (referrals, share, campaign, channel).
#                    Recognized here so a single ``invite_link()`` URL
#                    can be valid on every surface — the desktop
#                    protocol handler, Android intent filter, and iOS
#                    URL types all accept the same input.
#
# Verbs (path[0]):
#   - invite   → /invite/<invite_code>
#   - meet     → /meet/<platform>/<room_id>
#   - group    → /group/<platform>/<group_id>
#
# Mobile clients use these deep links to route an inbound share-link
# tap straight into the matching agent tool (Invite_Friend.accept or
# Join_External_Room) without going through a web redirect first.

DEEPLINK_SCHEMES: Tuple[str, ...] = ('hevolveai', 'nunba', 'hevolve')
DEEPLINK_VERBS: Tuple[str, ...] = ('invite', 'meet', 'group')


def is_allowed_deeplink_uri(uri: str) -> bool:
    """Validate a custom-scheme deep-link URI.

    True iff scheme ∈ DEEPLINK_SCHEMES and the leading path segment is
    one of DEEPLINK_VERBS with at least one trailing segment.
    """
    if not isinstance(uri, str) or not uri:
        return False
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    scheme = (parsed.scheme or '').lower()
    if scheme not in DEEPLINK_SCHEMES:
        return False
    # urlparse treats `nunba://invite/X` netloc='invite', path='/X'
    # and `nunba:invite/X` path='invite/X'.  Normalize.
    raw = (parsed.netloc + parsed.path) if parsed.netloc else parsed.path
    segments = [s for s in (raw or '').split('/') if s]
    if not segments:
        return False
    verb = segments[0].lower()
    if verb not in DEEPLINK_VERBS:
        return False
    # Per-verb arity:
    #   invite needs 1 trailing segment (code)        → total ≥ 2
    #   meet / group need 2 trailing segments
    #   (platform + room/group_id)                    → total ≥ 3
    required_total = 2 if verb == 'invite' else 3
    if len(segments) < required_total:
        return False
    return True


def invite_link(invite_code: str, scheme: str = 'hevolveai') -> str:
    """Build a custom-scheme deep link for accepting a friend invite."""
    if not invite_code:
        raise ValueError("invite_link requires a non-empty invite_code")
    s = (scheme or 'hevolveai').lower()
    if s not in DEEPLINK_SCHEMES:
        raise ValueError(f"unsupported scheme {scheme!r}")
    return f"{s}://invite/{invite_code}"


def meet_link(platform: str, room_id: str,
              scheme: str = 'hevolveai') -> str:
    """Build a custom-scheme deep link for joining an external meet."""
    if not platform or not room_id:
        raise ValueError("meet_link requires platform + room_id")
    s = (scheme or 'hevolveai').lower()
    if s not in DEEPLINK_SCHEMES:
        raise ValueError(f"unsupported scheme {scheme!r}")
    return f"{s}://meet/{platform.lower()}/{room_id}"


def group_link(platform: str, group_id: str,
               scheme: str = 'hevolveai') -> str:
    """Build a custom-scheme deep link for joining an external group."""
    if not platform or not group_id:
        raise ValueError("group_link requires platform + group_id")
    s = (scheme or 'hevolveai').lower()
    if s not in DEEPLINK_SCHEMES:
        raise ValueError(f"unsupported scheme {scheme!r}")
    return f"{s}://group/{platform.lower()}/{group_id}"


# ─── Public OG / share URL builder ────────────────────────────────
#
# F1 of memory/feedback_unification_reuse_contract.md — the SINGLE
# canonical builder for HTTPS share URLs that crawlers (Discord /
# Slack / WhatsApp Web / Twitter / Telegram / LinkedIn / FB) fetch
# to render unfurl previews.  Every emit site (InviteService,
# DistributionService, adapter share-card builders, OG endpoint)
# must call ``og_url`` — never inline a string.
#
# Distinct from the custom-scheme deep-links above:
#   * ``invite_link / meet_link / group_link`` → ``hevolveai://...`` —
#     consumed by the OS protocol handler, opens Nunba app directly.
#   * ``og_url`` → ``https://...`` — consumed by web bots for OG
#     preview rendering; redirects to SPA / app from the resulting
#     OG endpoint.
#
# These are NOT parallel paths: they serve different consumers
# (protocol handler vs HTTP bot fetcher).  A single emit site
# typically uses BOTH (custom-scheme as the click-through CTA inside
# an HTTPS preview card).

OG_RESOURCE_TYPES: Tuple[str, ...] = ('i', 'c', 'p', 'u')


def og_url(resource_type: str, identifier: str,
           base_url: Optional[str] = None) -> str:
    """Build canonical public share URL for a resource.

    Args:
        resource_type: one of OG_RESOURCE_TYPES — ``i`` (invite),
            ``c`` (community), ``p`` (post), ``u`` (user/agent).
            Future expansion: ``m`` (meet), ``g`` (group), ``ch``
            (channel) — these require canonical Meet / Group /
            ChannelBinding models first (F8 in the contract).
        identifier: the canonical ID for the resource (UUID, slug,
            invite_code, or handle).  Must be non-empty.
        base_url: override.  Defaults to
            ``HEVOLVE_PUBLIC_BASE_URL`` env or
            ``https://hevolve.ai``.

    Returns:
        ``<base>/<resource_type>/<identifier>``.  Example:
        ``https://hevolve.ai/i/A1B2C3``.

    Raises:
        ValueError on unknown ``resource_type`` or empty
        ``identifier``.

    Single-writer contract: every share-URL emit site in HARTOS +
    Nunba calls this function.  No parallel implementations.  See
    ``memory/feedback_unification_reuse_contract.md``.
    """
    if resource_type not in OG_RESOURCE_TYPES:
        raise ValueError(
            f"unsupported resource_type {resource_type!r}; "
            f"known: {OG_RESOURCE_TYPES}"
        )
    if not identifier:
        raise ValueError("og_url requires non-empty identifier")
    import os as _os
    base = base_url or _os.environ.get(
        'HEVOLVE_PUBLIC_BASE_URL', 'https://hevolve.ai'
    )
    return f"{base.rstrip('/')}/{resource_type}/{identifier}"
