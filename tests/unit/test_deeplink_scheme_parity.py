"""Cross-platform invite-deep-link parity drift-guard.

Why this test exists
--------------------
The deep-link contract has SIX surfaces — and historically each was
edited in isolation, so the scheme set drifted:

  | Surface                         | What it owns                |
  |---------------------------------|-----------------------------|
  | HARTOS canon                    | DEEPLINK_SCHEMES tuple      |
  | Nunba desktop (app.py)          | _SCHEMES tuple in           |
  |                                 | windows-protocol handler    |
  | Android AndroidManifest.xml     | <data android:scheme>       |
  | Android deepLinkService.js      | parseCustomScheme regex     |
  | iOS Info.plist                  | CFBundleURLSchemes array    |
  | iOS deepLinkService.js (sync)   | parseCustomScheme regex     |
  |                                 | (vendored from Android)     |

When the canon added ``nunba`` (UNIF-G4) the mobile sides kept
``hevolve`` only, and a ``hevolveai://invite/X`` link emitted by the
agent silently dead-lettered on phones. Captured 2026-05-07 audit:
no scheme worked on all three platforms simultaneously.

This test is the contract: every surface MUST recognize the full
``DEEPLINK_SCHEMES`` set, and every (scheme, verb) pair MUST be
parseable by the mobile JS regex. Run in CI to catch a future
regression the moment it lands.

Surface discovery is done via filesystem traversal — sibling repos
are looked up at predictable sibling paths. If a sibling repo isn't
on the developer's machine, that surface is SKIPPED with a clear
message (so HARTOS-only contributors aren't blocked, but a CI box
that has all three repos checked out exercises the full contract).
"""
from __future__ import annotations

import json
import os
import re
import unittest
from xml.etree import ElementTree as ET


HARTOS_REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
# Sibling-repo lookup. The drift-guard runs in three environments:
#   1. HARTOS-only checkout       → only canon assertion runs
#   2. PycharmProjects/* layout   → Nunba-HART-Companion as sibling
#   3. CI box w/ StudioProjects/* → Hevolve_React_Native + Nunba-Companion-iOS
# The walks are read-only and short-circuit on first hit, so a
# missing sibling never causes a false positive.
def _find_sibling_repo(name: str, search_roots: list[str]) -> str | None:
    for root in search_roots:
        cand = os.path.join(root, name)
        if os.path.isdir(cand):
            return cand
    return None


_SEARCH_ROOTS = [
    os.path.abspath(os.path.join(HARTOS_REPO, os.pardir)),
    # Windows dev box puts mobile repos under StudioProjects/ instead of
    # PycharmProjects/. Cover both.
    os.path.abspath(
        os.path.join(HARTOS_REPO, os.pardir, os.pardir, 'StudioProjects')),
]

NUNBA_REPO = _find_sibling_repo('Nunba-HART-Companion', _SEARCH_ROOTS)
RN_REPO = _find_sibling_repo('Hevolve_React_Native', _SEARCH_ROOTS)
IOS_REPO = _find_sibling_repo('Nunba-Companion-iOS', _SEARCH_ROOTS)


class DeeplinkParityTest(unittest.TestCase):
    """Every surface accepts the full DEEPLINK_SCHEMES × DEEPLINK_VERBS."""

    @classmethod
    def setUpClass(cls):
        from core.install_links import DEEPLINK_SCHEMES, DEEPLINK_VERBS
        cls.canonical_schemes = set(DEEPLINK_SCHEMES)
        cls.canonical_verbs = set(DEEPLINK_VERBS)

    # ──────────────────────────────────────────────────────────────────
    # 1. HARTOS canon — sanity
    # ──────────────────────────────────────────────────────────────────

    def test_canon_includes_hevolveai_nunba_hevolve(self):
        """Canon must include all three schemes — hevolveai (desktop),
        nunba (brand canon), hevolve (legacy mobile). If you remove one
        you break the platform that registered it."""
        self.assertIn('hevolveai', self.canonical_schemes)
        self.assertIn('nunba', self.canonical_schemes)
        self.assertIn('hevolve', self.canonical_schemes)

    def test_canon_verbs_cover_invite_meet_group(self):
        self.assertIn('invite', self.canonical_verbs)
        self.assertIn('meet', self.canonical_verbs)
        self.assertIn('group', self.canonical_verbs)

    def test_canon_validator_accepts_every_scheme_invite(self):
        from core.install_links import is_allowed_deeplink_uri
        for scheme in self.canonical_schemes:
            uri = f'{scheme}://invite/abc123'
            self.assertTrue(
                is_allowed_deeplink_uri(uri),
                f"canon validator rejects {uri!r} — schemes drifted")

    # ──────────────────────────────────────────────────────────────────
    # 2. Nunba desktop — app.py protocol handler _SCHEMES tuple
    # ──────────────────────────────────────────────────────────────────

    def test_nunba_desktop_accepts_all_canonical_schemes(self):
        if NUNBA_REPO is None:
            self.skipTest(
                "Nunba-HART-Companion sibling not on this machine — "
                "skipping desktop surface check")
        app_py = os.path.join(NUNBA_REPO, 'app.py')
        if not os.path.isfile(app_py):
            self.skipTest(f"app.py not at {app_py}")

        with open(app_py, encoding='utf-8') as f:
            src = f.read()

        # Locate `_SCHEMES = (...)` literal in the protocol handler.
        m = re.search(
            r"_SCHEMES\s*=\s*\(([^)]*)\)", src, re.DOTALL)
        self.assertIsNotNone(
            m, "_SCHEMES tuple not found in Nunba-HART-Companion/app.py "
               "— protocol handler may have been refactored; "
               "update this assertion to find its new home")
        body = m.group(1)
        for scheme in self.canonical_schemes:
            literal = f"'{scheme}://'"
            self.assertIn(
                literal, body,
                f"Nunba desktop _SCHEMES missing {literal} — a URL with "
                f"scheme {scheme} won't be recognized by the protocol "
                f"handler even though canon DEEPLINK_SCHEMES includes "
                f"it. Add to app.py:_SCHEMES.")

    # ──────────────────────────────────────────────────────────────────
    # 3. Android — AndroidManifest.xml intent-filter scheme set
    # ──────────────────────────────────────────────────────────────────

    def test_android_manifest_accepts_all_canonical_schemes(self):
        if RN_REPO is None:
            self.skipTest(
                "Hevolve_React_Native sibling not on this machine — "
                "skipping Android Manifest check")
        manifest = os.path.join(
            RN_REPO, 'android', 'app', 'src', 'main', 'AndroidManifest.xml')
        if not os.path.isfile(manifest):
            self.skipTest(f"AndroidManifest.xml not at {manifest}")

        # ElementTree handles the manifest fine; namespaces are needed
        # for attribute reads (android:scheme = {ns}scheme).
        ns = '{http://schemas.android.com/apk/res/android}'
        tree = ET.parse(manifest)
        root = tree.getroot()
        registered = set()
        for data in root.iter('data'):
            sch = data.attrib.get(ns + 'scheme')
            if sch:
                registered.add(sch)

        for scheme in self.canonical_schemes:
            self.assertIn(
                scheme, registered,
                f"AndroidManifest.xml has no <data android:scheme=\""
                f"{scheme}\"/> — Android won't open the app for "
                f"{scheme}://invite/X URLs. Add a <data> line to the "
                f"existing intent-filter block.")

    # ──────────────────────────────────────────────────────────────────
    # 4. iOS — Info.plist CFBundleURLSchemes array
    # ──────────────────────────────────────────────────────────────────

    def test_ios_infoplist_accepts_all_canonical_schemes(self):
        if IOS_REPO is None:
            self.skipTest(
                "Nunba-Companion-iOS sibling not on this machine — "
                "skipping iOS Info.plist check")
        plist = os.path.join(
            IOS_REPO, 'ios', 'NunbaCompanion', 'Info.plist')
        if not os.path.isfile(plist):
            self.skipTest(f"Info.plist not at {plist}")

        # plistlib.load handles both binary + xml plist forms.
        import plistlib
        with open(plist, 'rb') as f:
            data = plistlib.load(f)
        url_types = data.get('CFBundleURLTypes') or []
        registered: set[str] = set()
        for entry in url_types:
            for sch in (entry.get('CFBundleURLSchemes') or []):
                registered.add(str(sch))

        for scheme in self.canonical_schemes:
            self.assertIn(
                scheme, registered,
                f"Info.plist CFBundleURLSchemes missing {scheme!r} — "
                f"iOS won't open the app for {scheme}://invite/X URLs. "
                f"Add <string>{scheme}</string> to the existing "
                f"CFBundleURLTypes/CFBundleURLSchemes array.")

    # ──────────────────────────────────────────────────────────────────
    # 5. Mobile JS parser — Android source-of-truth
    # ──────────────────────────────────────────────────────────────────

    def test_android_jsparser_accepts_all_canonical_schemes(self):
        if RN_REPO is None:
            self.skipTest(
                "Hevolve_React_Native sibling not on this machine — "
                "skipping Android JS parser check")
        js = os.path.join(RN_REPO, 'services', 'deepLinkService.js')
        if not os.path.isfile(js):
            self.skipTest(f"deepLinkService.js not at {js}")

        with open(js, encoding='utf-8') as f:
            src = f.read()

        # Two acceptance checkpoints in the mobile parser:
        #   1. ``isCustomScheme`` predicate (handleDeepLink l.~209)
        #   2. ``parseCustomScheme`` regex set (lines ~125-150)
        # Each must mention every canonical scheme literal.
        for scheme in self.canonical_schemes:
            self.assertIn(
                f'{scheme}://', src,
                f"Android deepLinkService.js never mentions "
                f"{scheme}:// — the parser regex / isCustomScheme "
                f"check will fail to match URLs of that scheme even "
                f"though Manifest registers it. Extend "
                f"parseCustomScheme + isCustomScheme + "
                f"linkingConfig.prefixes to cover {scheme}://.")

    # ──────────────────────────────────────────────────────────────────
    # 6. Mobile JS parser — iOS sync target (must match Android verbatim)
    # ──────────────────────────────────────────────────────────────────

    def test_ios_jsparser_in_sync_with_android(self):
        if RN_REPO is None or IOS_REPO is None:
            self.skipTest("mobile siblings not both present")
        android = os.path.join(RN_REPO, 'services', 'deepLinkService.js')
        ios = os.path.join(
            IOS_REPO, 'js', 'shared', 'services', 'deepLinkService.js')
        if not os.path.isfile(android) or not os.path.isfile(ios):
            self.skipTest("one of the JS files isn't on disk")

        # Compare with line-ending normalised so CRLF↔LF doesn't
        # false-positive — the sync script copies bytes verbatim, but
        # different VCS check-out settings can flip line endings.
        def _norm(p: str) -> str:
            with open(p, encoding='utf-8') as f:
                return f.read().replace('\r\n', '\n').replace('\r', '\n')

        self.assertEqual(
            _norm(android), _norm(ios),
            "iOS deepLinkService.js diverged from Android source-of-"
            "truth. The iOS copy is auto-vendored via "
            "Nunba-Companion-iOS/scripts/sync-from-android.js — run "
            "`node scripts/sync-from-android.js` from the iOS repo to "
            "re-sync. The SHARED_JS_MANIFEST contract says: "
            "destinations should NEVER be hand-edited.")

    # ──────────────────────────────────────────────────────────────────
    # 7. End-to-end semantic — every canon (scheme, verb) parses on
    #    the mobile JS regex
    # ──────────────────────────────────────────────────────────────────

    def test_mobile_regex_matches_every_canon_scheme_invite(self):
        if RN_REPO is None:
            self.skipTest("Hevolve_React_Native sibling not on this machine")
        js = os.path.join(RN_REPO, 'services', 'deepLinkService.js')
        if not os.path.isfile(js):
            self.skipTest(f"deepLinkService.js not at {js}")

        with open(js, encoding='utf-8') as f:
            src = f.read()

        # The invite parser line must live in parseCustomScheme. Locate
        # ANY regex literal that anchors at start, has the scheme
        # alternation, then '://invite/'. We don't assume the exact
        # form — only that for each canon scheme, the substring
        # `<scheme>://invite/` appears in a regex/string literal that
        # the parser uses.
        for scheme in self.canonical_schemes:
            needle = f"{scheme}:\\/\\/invite"
            alt_needle = f"{scheme}://invite"
            self.assertTrue(
                needle in src or alt_needle in src,
                f"No invite-parsing regex covers {scheme}:// in "
                f"deepLinkService.js. A {scheme}://invite/CODE link "
                f"would launch the app but the parser would silently "
                f"drop it (worst kind of failure — no error log, no "
                f"feedback).")


if __name__ == '__main__':
    unittest.main()
