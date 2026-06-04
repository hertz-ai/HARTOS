"""Browser detection for omni-channel auto-association (#63) — DETECTION ONLY.

Discovers which browsers are installed and where their profile directories live,
so the channel-connect flow knows what the user has to work with.

SECURITY LINE (do not cross in this module): this reads NOTHING inside a
profile — no cookies, no "Login Data", no session/token stores.  Reading a
user's logged-in browser sessions is credential access; it belongs behind an
explicit consent gate or, preferably, per-channel OAuth — NOT in a passive
detector that any code might call.  Presence + path only.  If a future
"connect" step needs the sessions, it must ask first; this file stays read-only
metadata about which browsers exist.

Cross-platform (Windows / macOS / Linux).  Pure + injectable: profile roots are
computed from HOME / LOCALAPPDATA / APPDATA, and `detect_browsers(probe=...)`
takes the existence check as a parameter so tests never depend on the host's
real browser install.
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
