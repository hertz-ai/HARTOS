"""Behavioural tests for the offline-art feature (#143).

  (a) shell_manifest.bundled_app_logo  - a BUNDLED app logo is served for a known
      Flathub id, and only for a valid reverse-DNS id (offline, no network).
  (b) app_poster.central_agent_art     - the central-owned agent image is resolved
      by NAME, preferring HART_AGENT_ART_DIR (the documented central drop seam),
      and served via the /shell/agent-art/<slug> route.
  (c) /api/shell/credits               - the About > Credits route returns the
      third-party art licence ledger parsed from docs/THIRD_PARTY_ART.md.

Each mocks only the boundary (env var, temp filesystem, the doc file), calls the
REAL function/route, and asserts observable output. Offline-first: none touch the
network.

Run isolated:  python tests/unit/test_offline_art.py
"""
import os
import tempfile

from integrations.agent_engine import shell_manifest, app_poster
from integrations.agent_engine import shell_desktop_apis as sd


# ── (a) bundled app logo ─────────────────────────────────────────────────────

def test_bundled_app_logo_serves_a_known_app_offline():
    # Firefox ships a bundled tile (generate_posters.py); the resolver returns its
    # same-origin served URL with NO network call.
    url = shell_manifest.bundled_app_logo('org.mozilla.firefox')
    assert url == '/shell/static/app_art/apps/org.mozilla.firefox.svg'
    # And the file really exists on disk (the URL is not fabricated).
    disk = os.path.join(os.path.dirname(shell_manifest.__file__),
                        'static', 'app_art', 'apps', 'org.mozilla.firefox.svg')
    assert os.path.isfile(disk)


def test_bundled_app_logo_rejects_junk_and_missing():
    assert shell_manifest.bundled_app_logo('not-a-flathub-id') is None   # no dot
    assert shell_manifest.bundled_app_logo('') is None
    assert shell_manifest.bundled_app_logo(None) is None
    # A well-formed but unbundled id has no tile -> None (client shows the glyph).
    assert shell_manifest.bundled_app_logo('com.example.Nope') is None


def test_bundled_app_logo_prefers_a_dropped_in_raster_override():
    # The seam: a real official logo dropped in as <id>.png|.svg overrides. Point
    # the resolver's dir check at a temp dir via monkeypatching the module const.
    d = tempfile.mkdtemp()
    open(os.path.join(d, 'com.acme.Widget.png'), 'wb').write(b'PNG')
    orig = shell_manifest._APPS_ART_DIR
    try:
        shell_manifest._APPS_ART_DIR = d
        assert (shell_manifest.bundled_app_logo('com.acme.Widget')
                == '/shell/static/app_art/apps/com.acme.Widget.png')
    finally:
        shell_manifest._APPS_ART_DIR = orig


# ── (b) central agent art ────────────────────────────────────────────────────

def test_central_agent_art_prefers_hart_agent_art_dir():
    d = tempfile.mkdtemp()
    open(os.path.join(d, 'auto-research.png'), 'wb').write(b'\x89PNG')
    old = os.environ.get('HART_AGENT_ART_DIR')
    os.environ['HART_AGENT_ART_DIR'] = d
    try:
        # matched by a slug of the NAME, served via the same-origin route
        assert app_poster.central_agent_art('Auto Research') == '/shell/agent-art/auto-research'
        assert app_poster.find_central_agent_file('Auto Research') == os.path.join(d, 'auto-research.png')
        # a name with no dropped image -> None (falls back to generated/brand art)
        assert app_poster.central_agent_art('No Such Agent') is None
    finally:
        if old is None:
            os.environ.pop('HART_AGENT_ART_DIR', None)
        else:
            os.environ['HART_AGENT_ART_DIR'] = old


def test_central_agent_art_none_without_a_drop():
    old = os.environ.pop('HART_AGENT_ART_DIR', None)
    try:
        # No env dir + no bundled file for this name -> None, no raise.
        assert app_poster.central_agent_art('Totally Unbundled Agent') is None
    finally:
        if old is not None:
            os.environ['HART_AGENT_ART_DIR'] = old


def test_art_slug_cannot_traverse():
    # The slug only keeps [a-z0-9-]; traversal / separators are collapsed away, so
    # the /shell/agent-art/<slug> route can never escape the drop dirs.
    assert app_poster._art_slug('../../etc/passwd') == 'etc-passwd'
    assert app_poster._art_slug('a/b\\c') == 'a-b-c'


# ── (b) the /shell/agent-art serving route ───────────────────────────────────

def _shell_app():
    """A minimal Flask app carrying just the agent-art route (mirrors the real
    one in liquid_ui_service) so the serving contract is tested without booting
    the whole shell service."""
    from flask import Flask, Response, send_from_directory
    app = Flask(__name__)

    @app.route('/shell/agent-art/<slug>')
    def shell_agent_art(slug):
        path = app_poster.find_central_agent_file(slug)
        if not path or not os.path.isfile(path):
            return Response(status=404)
        return send_from_directory(os.path.dirname(path), os.path.basename(path))
    return app


def test_agent_art_route_serves_and_404s():
    d = tempfile.mkdtemp()
    open(os.path.join(d, 'trading.png'), 'wb').write(b'\x89PNGbytes')
    old = os.environ.get('HART_AGENT_ART_DIR')
    os.environ['HART_AGENT_ART_DIR'] = d
    try:
        c = _shell_app().test_client()
        r = c.get('/shell/agent-art/trading')
        assert r.status_code == 200 and r.data == b'\x89PNGbytes'
        assert c.get('/shell/agent-art/nonexistent').status_code == 404
    finally:
        if old is None:
            os.environ.pop('HART_AGENT_ART_DIR', None)
        else:
            os.environ['HART_AGENT_ART_DIR'] = old


# ── (c) credits ledger + route ───────────────────────────────────────────────

def test_credits_ledger_parses_the_real_doc():
    led = sd.get_credits_ledger()
    assert led['title'] and led['binding_rule']
    assert led['sections']
    # The source-licence table is parsed with Flathub + lummi rows present.
    all_rows = [r for s in led['sections'] for r in s['rows']]
    sources = ' '.join(str(v) for r in all_rows for v in r.values())
    assert 'Flathub' in sources and 'lummi' in sources


def test_credits_ledger_degrades_on_missing_doc():
    led = sd.get_credits_ledger(doc_path='/no/such/credits.md')
    assert led['sections'] == [] and led['source'] == ''    # empty, no raise


def test_credits_ledger_parses_a_custom_doc():
    md = ('# Credits\n\n**Binding rule (test): keep it honest.**\n\n'
          '## Sources\n\n'
          '| Source | License |\n|---|---|\n'
          '| Acme Art | CC0 |\n'
          '| _(none bundled yet)_ | |\n')
    p = os.path.join(tempfile.mkdtemp(), 'c.md')
    open(p, 'w', encoding='utf-8').write(md)
    led = sd.get_credits_ledger(doc_path=p)
    assert 'keep it honest' in led['binding_rule']
    secs = [s for s in led['sections'] if s['heading'] == 'Sources']
    assert len(secs) == 1
    rows = secs[0]['rows']
    assert len(rows) == 1                                    # placeholder dropped
    assert rows[0]['Source'] == 'Acme Art' and rows[0]['License'] == 'CC0'


def test_credits_route_returns_the_ledger():
    from flask import Flask
    from integrations.agent_engine.shell_desktop_apis import register_shell_desktop_routes
    app = Flask(__name__)
    register_shell_desktop_routes(app)
    r = app.test_client().get('/api/shell/credits')
    assert r.status_code == 200
    data = r.get_json()
    assert data['sections'] and data['title']
    blob = ' '.join(str(v) for s in data['sections'] for row in s['rows']
                    for v in row.values())
    assert 'Flathub' in blob


if __name__ == '__main__':
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print('  OK  ', name)
        except Exception as e:
            failed += 1
            print('  FAIL', name, '->', repr(e))
    print('%d/%d passed' % (len(fns) - failed, len(fns)))
    import sys
    sys.exit(1 if failed else 0)
