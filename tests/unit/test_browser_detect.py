"""Browser detection for omni-channel auto-association (#63) — DETECTION ONLY.

Behavioural tests of the real functions: inject platform / base dirs / an
existence probe, assert the computed profile roots and presence flags.  The last
test enforces the security line — it captures EVERY path the module touches and
asserts none is a cookie / session file (the detector must never read profile
contents).  Host-agnostic: expected paths are built with os.path.join so the
asserts hold on any OS the test runs on.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.channels import browser_detect


def _roots(**kw):
    return {b['name']: b['profile_root']
            for b in browser_detect.detect_browsers(probe=lambda p: False, **kw)}


def test_windows_chromium_under_localappdata_firefox_under_appdata():
    r = _roots(platform='win32', localappdata='LA', appdata='AD')
    assert r['chrome'] == os.path.join('LA', 'Google', 'Chrome', 'User Data')
    assert r['edge'] == os.path.join('LA', 'Microsoft', 'Edge', 'User Data')
    assert r['brave'] == os.path.join('LA', 'BraveSoftware', 'Brave-Browser', 'User Data')
    # Firefox uses APPDATA on Windows, not LOCALAPPDATA — the one divergence.
    assert r['firefox'] == os.path.join('AD', 'Mozilla', 'Firefox', 'Profiles')


def test_windows_without_base_dir_yields_no_root(monkeypatch):
    # Explicit empty param falls back to the env var (correct); to exercise the
    # defensive "no base dir at all" branch, clear the env too.
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    monkeypatch.delenv('APPDATA', raising=False)
    r = _roots(platform='win32', localappdata='', appdata='')
    assert r['chrome'] is None and r['firefox'] is None


def test_linux_paths_under_home():
    r = _roots(platform='linux', home='/home/u')
    assert r['chrome'] == os.path.join('/home/u', '.config', 'google-chrome')
    assert r['edge'] == os.path.join('/home/u', '.config', 'microsoft-edge')
    assert r['firefox'] == os.path.join('/home/u', '.mozilla', 'firefox')


def test_darwin_paths_under_home():
    r = _roots(platform='darwin', home='/Users/u')
    assert r['chrome'] == os.path.join('/Users/u', 'Library', 'Application Support',
                                       'Google', 'Chrome')


def test_present_reflects_probe():
    rows = browser_detect.detect_browsers(
        platform='linux', home='/home/u',
        probe=lambda p: p.endswith('google-chrome'))
    present = {b['name']: b['present'] for b in rows}
    assert present['chrome'] is True
    assert present['edge'] is False and present['firefox'] is False


def test_installed_browsers_filters_to_present():
    names = browser_detect.installed_browsers(
        platform='linux', home='/home/u',
        probe=lambda p: 'google-chrome' in p or 'firefox' in p)
    assert sorted(names) == ['chrome', 'firefox']


def test_detection_never_touches_profile_contents():
    # The injected probe is the module's ONLY filesystem touch.  Capture every
    # path it receives and assert each is a profile ROOT, never a cookie /
    # session store — the #63 security line, enforced behaviourally.
    probed = []
    browser_detect.detect_browsers(
        platform='linux', home='/home/u',
        probe=lambda p: (probed.append(p), False)[1])
    assert probed, "expected the detector to probe profile roots"
    for p in probed:
        low = p.lower()
        assert 'cookies' not in low
        assert 'login data' not in low
        assert 'cookies.sqlite' not in low
        assert 'sessionstore' not in low


# ── capability 2: consent-gated channel-usage detection (#63) ──────────────

def test_channel_domain_keys_are_catalog_keys():
    # Single-source-of-truth guard: every channel the allowlist names must be a
    # real CHANNEL_CATALOG key, so the connect step can hand to register_channel.
    from integrations.channels.metadata import CHANNEL_CATALOG
    for ch in browser_detect._CHANNEL_WEB_DOMAINS:
        assert ch in CHANNEL_CATALOG, f"{ch} not a catalog channel"


def test_channel_for_url_maps_known_domains_and_subdomains():
    assert browser_detect.channel_for_url('https://discord.com/channels/1/2') == 'discord'
    assert browser_detect.channel_for_url('https://web.whatsapp.com/') == 'whatsapp'
    assert browser_detect.channel_for_url('https://canary.discord.com/app') == 'discord'  # subdomain
    assert browser_detect.channel_for_url('https://x.com/messages') == 'twitter'           # alias domain
    assert browser_detect.channel_for_url('https://teams.microsoft.com/_#/conv') == 'teams'


def test_channel_for_url_ignores_non_channel_and_empty():
    assert browser_detect.channel_for_url('https://news.ycombinator.com/') is None
    assert browser_detect.channel_for_url('https://mybank.example.com/login') is None
    assert browser_detect.channel_for_url('') is None
    assert browser_detect.channel_for_url(None) is None
    # a domain that merely CONTAINS an allowlisted token must not match
    assert browser_detect.channel_for_url('https://notdiscord.com.evil.test/') is None


def test_detect_channel_usage_off_by_default_never_reads(monkeypatch):
    monkeypatch.delenv('HART_BROWSER_HISTORY_SCAN', raising=False)
    calls = []

    def _spy_reader(root, name):
        calls.append((root, name))
        return ['https://discord.com/']

    res = browser_detect.detect_channel_usage(
        browsers=[{'name': 'chrome', 'profile_root': '/p', 'present': True}],
        history_reader=_spy_reader,
    )
    assert res['enabled'] is False
    assert res['channels'] == []
    assert calls == [], "history must NOT be read when detection is gated off"
    assert 'OFF' in res['notice']


def test_detect_channel_usage_with_consent_maps_history():
    history = {
        'chrome': ['https://discord.com/channels/1', 'https://news.example.com/',
                   'https://web.whatsapp.com/'],
        'firefox': ['https://x.com/home'],
    }
    res = browser_detect.detect_channel_usage(
        consent=True,
        browsers=[
            {'name': 'chrome', 'profile_root': '/c', 'present': True},
            {'name': 'firefox', 'profile_root': '/f', 'present': True},
            {'name': 'edge', 'profile_root': '/e', 'present': False},  # skipped
        ],
        history_reader=lambda root, name: history.get(name, []),
    )
    assert res['enabled'] is True
    assert res['channels'] == ['discord', 'twitter', 'whatsapp']  # sorted, deduped
    assert 'edge' not in res['browsers']                          # absent browser not scanned
    assert sorted(res['browsers']) == ['chrome', 'firefox']
    assert 'No cookies' in res['notice']


def test_detect_channel_usage_env_flag_enables(monkeypatch):
    monkeypatch.setenv('HART_BROWSER_HISTORY_SCAN', 'true')
    res = browser_detect.detect_channel_usage(
        browsers=[{'name': 'chrome', 'profile_root': '/c', 'present': True}],
        history_reader=lambda root, name: ['https://app.slack.com/client/T1'],
    )
    assert res['enabled'] is True
    assert res['channels'] == ['slack']


def test_detect_channel_usage_reader_error_degrades_to_empty():
    def _boom(root, name):
        raise OSError("locked")

    res = browser_detect.detect_channel_usage(
        consent=True,
        browsers=[{'name': 'chrome', 'profile_root': '/c', 'present': True}],
        history_reader=_boom,
    )
    assert res['enabled'] is True
    assert res['channels'] == []        # never raises; degrades to "found nothing"
