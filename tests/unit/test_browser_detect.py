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
