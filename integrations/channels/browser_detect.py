"""Browser detection for omni-channel auto-association (#63).

Two capabilities, in strictly increasing privacy sensitivity:

  1. ``detect_browsers`` / ``installed_browsers`` — DETECTION ONLY.  Discovers
     which browsers are installed and where their profile dirs live.  Reads
     NOTHING inside a profile (presence + path only).

  2. ``detect_channel_usage`` — CONSENT-GATED history check.  To suggest the
     channels a user already uses, it checks the browser's HISTORY db for a
     fixed ALLOWLIST of messaging-app domains (does ``discord.com`` /
     ``web.whatsapp.com`` appear?).  This is OFF by default and only runs when
     the caller passes ``consent=True`` OR the ``HART_BROWSER_HISTORY_SCAN``
     env flag is set.

THE CREDENTIAL LINE (never crossed, in either capability): this module never
reads cookies, "Login Data", session tokens, or any credential store — that is
account-takeover-grade access and belongs to per-channel OAuth, not a local
scan.  History reading (capability 2) is a categorically lower tier: it is the
user's own browsing record on the user's own device, it is read-only, it is
scoped to the messaging-domain allowlist (no other URL is ever returned or
recorded), and it is gated behind explicit consent.  The actual channel
*connect* still goes through the existing OAuth click-through — detection only
*suggests*.

Cross-platform (Windows / macOS / Linux).  Pure + injectable: profile roots are
computed from HOME / LOCALAPPDATA / APPDATA; ``detect_browsers(probe=...)`` takes
the existence check as a parameter and ``detect_channel_usage(history_reader=...)``
takes the history read as a parameter, so tests never depend on (or touch) the
host's real browser install.
"""
import os
import sys

# browser -> per-platform profile-root path segments (relative to the platform
# base dir).  Standard Chromium "User Data" roots + the Firefox "Profiles" root.
# We only ever test these for existence; we never enumerate or open contents.
_BROWSER_ROOTS = {
    'chrome':  {'win': ('Google', 'Chrome', 'User Data'),
                'darwin': ('Library', 'Application Support', 'Google', 'Chrome'),
                'linux': ('.config', 'google-chrome')},
    'edge':    {'win': ('Microsoft', 'Edge', 'User Data'),
                'darwin': ('Library', 'Application Support', 'Microsoft Edge'),
                'linux': ('.config', 'microsoft-edge')},
    'brave':   {'win': ('BraveSoftware', 'Brave-Browser', 'User Data'),
                'darwin': ('Library', 'Application Support', 'BraveSoftware', 'Brave-Browser'),
                'linux': ('.config', 'BraveSoftware', 'Brave-Browser')},
    'firefox': {'win': ('Mozilla', 'Firefox', 'Profiles'),
                'darwin': ('Library', 'Application Support', 'Firefox', 'Profiles'),
                'linux': ('.mozilla', 'firefox')},
}


def _platform_key(platform=None):
    p = platform or sys.platform
    if p.startswith('win'):
        return 'win'
    if p == 'darwin':
        return 'darwin'
    return 'linux'


def _profile_root(name, platform=None, home=None, localappdata=None, appdata=None):
    """Compute a browser's profile-root path for a platform.  No I/O.

    On Windows, Chromium browsers live under %LOCALAPPDATA% and Firefox under
    %APPDATA%; elsewhere everything hangs off HOME.  Returns None if the browser
    is unknown for the platform or the required base dir is unavailable.
    """
    parts = _BROWSER_ROOTS.get(name, {}).get(_platform_key(platform))
    if not parts:
        return None
    if _platform_key(platform) == 'win':
        if name == 'firefox':
            base = appdata or os.environ.get('APPDATA', '')
        else:
            base = localappdata or os.environ.get('LOCALAPPDATA', '')
        if not base:
            return None
        return os.path.join(base, *parts)
    base = home or os.path.expanduser('~')
    return os.path.join(base, *parts)


def detect_browsers(platform=None, home=None, localappdata=None, appdata=None,
                    probe=None):
    """Return [{name, profile_root, present}] for known browsers — DETECTION ONLY.

    ``probe(path) -> bool`` is the existence check (defaults to os.path.isdir);
    inject it in tests.  This function NEVER opens anything under profile_root —
    it only asks "does this directory exist".
    """
    probe = probe or os.path.isdir
    out = []
    for name in _BROWSER_ROOTS:
        root = _profile_root(name, platform, home, localappdata, appdata)
        out.append({
            'name': name,
            'profile_root': root,
            'present': bool(root) and bool(probe(root)),
        })
    return out


def installed_browsers(**kw):
    """Names of browsers whose profile root exists (convenience over detect_browsers)."""
    return [b['name'] for b in detect_browsers(**kw) if b['present']]


# ─────────────────────────────────────────────────────────────────────────
# Consent-gated channel-usage detection (capability 2 — see module docstring)
# ─────────────────────────────────────────────────────────────────────────
#
# ALLOWLIST: channel_type -> the web-app domains that indicate the user uses
# that channel in a browser.  History is matched ONLY against these domains; no
# other URL is ever read out, recorded, or returned.  The keys are
# CHANNEL_CATALOG keys (drift-guarded by test_browser_detect) so the *connect*
# step can hand straight to the existing register_channel / OAuth flow.  Only
# channels with a canonical public web app appear here (self-hosted ones —
# mattermost, rocketchat, matrix-homeserver — have no fixed domain to match).
_CHANNEL_WEB_DOMAINS = {
    'discord':     ('discord.com',),
    'slack':       ('slack.com',),
    'whatsapp':    ('web.whatsapp.com',),
    'telegram':    ('web.telegram.org',),
    'teams':       ('teams.microsoft.com', 'teams.live.com'),
    'messenger':   ('messenger.com',),
    'instagram':   ('instagram.com',),
    'twitter':     ('twitter.com', 'x.com'),
    'google_chat': ('chat.google.com',),
    'matrix':      ('app.element.io',),
}

# Opt-in env flag (OFF unless explicitly enabled).  Mirrors the gated-capability
# pattern used by send_fcm_push (credential gate) and HEVOLVE_AUTONOMOUS_MARKETING.
_HISTORY_ENV_FLAG = 'HART_BROWSER_HISTORY_SCAN'
_TRUTHY = ('1', 'true', 'yes', 'on')

_SCAN_DISABLED_NOTICE = (
    "Browser-history channel detection is OFF. Enable it explicitly (consent) to "
    "let HART suggest channels you already use. Only messaging-app domains are "
    "ever checked — cookies and logins are never read."
)
_SCAN_NOTICE = (
    "Checked your browser history ({browsers}) for known messaging-app domains "
    "only (e.g. discord.com, web.whatsapp.com). No cookies, logins, or other "
    "sites were read."
)


def channel_for_url(url):
    """Map a URL to a channel_type if its host is in the allowlist, else None.

    Pure.  Host match is exact-or-subdomain (so ``canary.discord.com`` matches
    ``discord.com`` and ``web.whatsapp.com`` matches itself).  Non-allowlisted
    hosts return None — this is the scoping guarantee: nothing outside the
    messaging-domain allowlist is ever surfaced.
    """
    if not url:
        return None
    host = url.lower().split('://', 1)[-1].split('/', 1)[0]
    host = host.split('@')[-1].split(':', 1)[0].split('?', 1)[0].strip()
    for ch, domains in _CHANNEL_WEB_DOMAINS.items():
        for d in domains:
            if host == d or host.endswith('.' + d):
                return ch
    return None


def _read_history_urls(profile_root, name, max_urls=5000):
    """Read URL strings from a browser's history DB — the I/O BOUNDARY.

    Read-only and scoped to the ``url`` column only (never cookies/credentials).
    The live history DB is usually SQLite-locked while the browser runs, so we
    copy it to a temp file first and query the copy.  Returns [] on ANY error
    (missing, locked, schema drift) — detection degrades to "found nothing",
    never raises.  Injected as ``history_reader`` in tests so the unit tests
    never touch a real browser profile.
    """
    if not profile_root or not os.path.isdir(profile_root):
        return []
    import glob
    import shutil
    import sqlite3
    import tempfile
    if name == 'firefox':
        db_paths = glob.glob(os.path.join(profile_root, '*', 'places.sqlite'))
        query = "SELECT url FROM moz_places LIMIT ?"
    else:  # chromium family: Default / "Profile N" dirs each hold a History db
        db_paths = glob.glob(os.path.join(profile_root, '*', 'History'))
        query = "SELECT url FROM urls LIMIT ?"
    urls = []
    for db in db_paths:
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix='.sqlite')
            os.close(fd)
            shutil.copyfile(db, tmp)
            conn = sqlite3.connect(tmp)
            try:
                for row in conn.execute(query, (max_urls,)):
                    if row and row[0]:
                        urls.append(row[0])
            finally:
                conn.close()
        except Exception:
            continue
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return urls


def detect_channel_usage(consent=False, browsers=None, history_reader=None, env=None):
    """Detect which catalog channels the user actively uses, from browser history
    SCOPED TO THE MESSAGING-DOMAIN ALLOWLIST.

    GATED: returns nothing unless ``consent=True`` OR ``HART_BROWSER_HISTORY_SCAN``
    is set.  When gated off, ``history_reader`` is never called (no profile is
    touched at all).  Read-only, opt-in, allowlist-scoped, never credentials.

    Returns ``{'enabled': bool, 'channels': [channel_type...],
    'browsers': [scanned names], 'notice': str}`` — ``notice`` is the
    user-facing transparency line describing exactly what was (or wasn't) read.
    """
    env = env or os.environ.get
    enabled = bool(consent) or str(env(_HISTORY_ENV_FLAG, '') or '').strip().lower() in _TRUTHY
    if not enabled:
        return {'enabled': False, 'channels': [], 'browsers': [],
                'notice': _SCAN_DISABLED_NOTICE}

    browsers = browsers if browsers is not None else detect_browsers()
    history_reader = history_reader or _read_history_urls
    found = set()
    scanned = []
    for b in browsers:
        if not b.get('present'):
            continue
        scanned.append(b['name'])
        try:
            for url in history_reader(b['profile_root'], b['name']) or []:
                ch = channel_for_url(url)
                if ch:
                    found.add(ch)
        except Exception:
            continue
    return {'enabled': True, 'channels': sorted(found), 'browsers': scanned,
            'notice': _SCAN_NOTICE.format(browsers=', '.join(scanned) or 'none')}
