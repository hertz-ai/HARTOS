"""CHAPTER 01 -- BOOT & FIRST PAINT (the shell-surface story, part 1 of N).

This suite narrates the deployed HART OS shell as ONE continuous story, from
entry to exit, source to sink.  Every test below is a chapter beat, ordered
TOPOLOGICALLY along the real boot flow, and every test's comment block names
the exact functions/files each request flows through and the data at each hop.
The narration is the semantic spec: when a future change contradicts a
narrated hop, the failing test names the hop, so the regression is found by
reading, not re-derived from the code.

THE SCENE.  On a real node, greetd starts the cage kiosk, cage starts the
GTK/WebKit shell host, and the host's very first HTTP request is GET / against
LiquidUIService's Flask app (integrations/agent_engine/liquid_ui_service.py,
served by serve_forever over waitress on the liquid-ui port).  Here the SAME
app factory (_create_flask_app) is driven through the recording test_client
from conftest.py, with the subprocess boundary faked (fake_os) and the peer
HTTP boundary refusing instantly (no_network) -- so every degrade branch we
narrate is the same one CI and an offline node exercise.

Chapter 01 beats, in flow order:
  1. service construction (surface_app fixture == the node's boot)
  2. GET /            -> render_desktop_shell -> themed HTML first paint
  3. asset closure    -> every /shell/static ref the HTML makes must serve 200
  4. boot data        -> the inlined MANIFEST + GET /api/shell/apps
  5. boot data        -> GET /api/context (offline degrade narrated)
  6. boot data        -> GET /api/shell/events (journalctl tail, faked)
  7. console sink     -> POST /api/shell/clientlog (error branch)
  8. console sink     -> warn + oversize branches (sequential branch beats)
"""
import json
import logging
import re

# ---------------------------------------------------------------------------
# Shared story prop: pin the node's persisted boot theme to the shipped
# default (aura) so the first paint is deterministic on ANY box.
#
# On a REAL first boot, ThemeService.auto_select_theme() (theme_service.py)
# runs detect_performance_tier() and writes agent_data/active_theme.json;
# on standard+ hardware that file ends up holding the aura preset (the
# shipped OS boot theme, dev_loop 2026-07-15).  Here we place that exact
# on-disk artifact ourselves (aura.json loaded from the REAL preset dir,
# nixos/assets/conky-themes/) into a tmp dir and repoint the module-level
# path constants theme_service._ACTIVE_THEME_PATH/_CUSTOM_OVERRIDES_PATH,
# which every ThemeService static method reads AT CALL TIME -- so the test
# drives the same read path as a booted node, with the node's own data.
# (Repointing also keeps the suite from writing into the repo's agent_data/.)
# ---------------------------------------------------------------------------


def pin_aura_active_theme(monkeypatch, tmp_path):
    """Seed <tmp>/active_theme.json with the REAL aura preset and repoint the
    ThemeService disk paths there.  Returns the aura preset dict."""
    from integrations.agent_engine import theme_service as ts
    aura = ts.ThemeService.get_preset('aura')   # reads nixos/assets/conky-themes/aura.json
    assert aura is not None and aura['id'] == 'aura', (
        'aura.json missing from nixos/assets/conky-themes/ -- the shipped '
        'default theme is gone')
    active = tmp_path / 'active_theme.json'
    active.write_text(json.dumps(aura), encoding='utf-8')
    monkeypatch.setattr(ts, '_ACTIVE_THEME_PATH', str(active))
    monkeypatch.setattr(ts, '_CUSTOM_OVERRIDES_PATH',
                        str(tmp_path / 'theme_custom.json'))
    return aura


def test_01_boot_service_construction_and_health(client):
    # ENTRY  the node boots the shell service (conftest.surface_app runs the
    #        SAME factory the node does):
    #        LiquidUIService.__init__()  [liquid_ui_service.py]
    #          -> stores ports (backend_port=6777, model_bus_port=6790),
    #             builds ContextEngine, computes self._data_dir from
    #             HEVOLVE_DATA_DIR / HART_DATA_DIR / <repo>/agent_data
    #        -> _create_flask_app()
    #          -> _register_self(): registers this instance in
    #             core.platform.registry so in-process A2UI emitters reach the
    #             LIVE shell via get_registry().get_or_none('LiquidUIService')
    #          -> Flask(__name__, static_url_path='/shell/static') -- the
    #             dead-husk fix: shell assets serve from /shell/static, NOT
    #             Flask's default /static
    #          -> registers ~310 routes (own routes + shell_os_apis +
    #             shell_desktop_apis + shell_system_apis + app_installer +
    #             os_bridge + flash + openclaw + onboarding + media), each
    #             module in its OWN try/except (#18 route-drop hardening)
    # DRIVE  GET /health -- the liveness probe systemd/monitoring hits first
    #        -> health()  [liquid_ui_service.py]
    # DATA   {'status':'ok', 'service':'liquid-ui-shell',
    #         'model_available': False (no model-ready signal arrived yet;
    #         serve_forever's model wait never ran in-test), 'renderer': ...}
    # SINK   200 application/json to the prober
    resp = client.get('/health')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'ok'
    assert body['service'] == 'liquid-ui-shell'
    assert body['model_available'] is False
    assert 'renderer' in body

    # And the construction side-effect is observable: the instance is in the
    # platform registry (the in-process half of the A2UI push channel).
    from core.platform.registry import get_registry
    assert get_registry().has('LiquidUIService'), (
        '_register_self did not land the instance in the platform registry; '
        'in-process A2UI emitters would push into the void')


def test_02_first_paint_serves_theme_tokens_and_gpu_floor(
        client, monkeypatch, tmp_path):
    # ENTRY  GET /  (the cage kiosk's WebKit host, first request after boot)
    #        -> index()  [liquid_ui_service.py]
    #        -> render_desktop_shell()
    #           -> ThemeService.get_css_variables() + get_active_theme()
    #              [theme_service.py]: reads _ACTIVE_THEME_PATH (pinned here
    #              to the aura preset, the artifact auto_select_theme wrote on
    #              the node's first boot), merges _CUSTOM_OVERRIDES_PATH (none)
    #           -> gpu verdict: read_gpu_render_mode() opens
    #              /run/hart/gpu-render (hart-gpu-probe's boot verdict file);
    #              on this box the file is ABSENT -> fail-SOFTWARE floor ->
    #              gpu_mode='software'.
    #           -> webkit compositing: LIQUID_UI_PREFER_HW_GL (set by
    #              hart-liquid-ui.nix from ui.preferHardwareGL) pinned '0'
    #              -> webkit-flat (backdrop-filter will NOT paint -> the CSS
    #              solidifies the glass, #151 transparent-windows fix)
    #           -> body class = 'gpu-software' + ' webkit-flat'
    #           -> a11y block: get_a11y_settings() [shell_os_apis.py] is the
    #              import-seeded defaults (font_scale 1.0) -> NO font-scale
    #              override emitted (contrast with chapter 02 beat 11)
    #           -> shell_manifest.PANEL_MANIFEST -> with_icon_colors -> inlined
    #              as const MANIFEST (there is NO /api/shell/manifest route)
    # DATA   aura colors.accent '00E6C3' ==> '--hart-accent: #00E6C3;'
    #        aura colors.secondary '9B5CFF' ==> '--hart-a2: #9B5CFF;'
    #        accent parsed to a comma triple ==> ':root{--hart-accent-rgb:0,230,195}'
    #        aura shell.glass_rgb '255,255,255' ==> '--hart-glass-rgb: 255,255,255;'
    #        aura font.display 'Space Grotesk' ==> '--hart-font-display: "Space Grotesk";'
    #        aura wallpaper.value ==> '.wallpaper{...background:linear-gradient(160deg,#05060D...'
    #        self.backend_port 6777 ==> "const BACKEND = 'http://localhost:6777';"
    # SINK   200 text/html to the WebKit host; the page then loads its
    #        /shell/static assets (next beat) and its console errors forward
    #        via POST /api/shell/clientlog (beats 7-8)
    pin_aura_active_theme(monkeypatch, tmp_path)
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '0')

    resp = client.get('/')
    assert resp.status_code == 200
    assert resp.mimetype == 'text/html'
    html = resp.get_data(as_text=True)

    # theme tokens reached the paint
    assert '--hart-accent: #00E6C3;' in html
    assert '--hart-a2: #9B5CFF;' in html
    assert ':root{--hart-accent-rgb:0,230,195}' in html
    assert '--hart-glass-rgb: 255,255,255;' in html
    assert '--hart-font-display: "Space Grotesk";' in html
    assert 'background:linear-gradient(160deg,#05060D' in html

    # the render floor on this box: software render + flat webkit
    assert '<body class="gpu-software webkit-flat">' in html

    # boot data inlined into the page (no separate manifest route exists)
    assert 'const MANIFEST = {' in html
    assert '"wallpaper_manager"' in html          # a real PANEL_MANIFEST id
    assert "const BACKEND = 'http://localhost:6777';" in html

    # a11y at defaults: the font-scale override block is NOT emitted
    assert ':root{--hart-font-size:' not in html


def test_03_first_paint_asset_closure_no_dead_husk(client, monkeypatch,
                                                   tmp_path):
    # ENTRY  the WebKit host parses the HTML from GET / and fetches EVERY
    #        referenced asset back from the SAME origin:
    #          <script defer src="/shell/static/*.js">   (orb, hero, desktop,
    #             onboarding, personalize, dock, ...)
    #          <link rel="stylesheet" href="/shell/static/*.css">
    #          <img src="/shell/static/hevolve-logo.png">
    #          @font-face url('/shell/static/MaterialSymbolsRounded.woff2')
    #        -> Flask's built-in static handler (the app was constructed with
    #           static_url_path='/shell/static', static_folder='static' next
    #           to liquid_ui_service.py)
    # DATA   ref list extracted from the SERVED page (never a hardcoded list,
    #        so this beat can never drift from what the HTML actually asks)
    # SINK   200 + non-empty body per asset.  THE DEAD-HUSK CONTRACT
    #        (2026-06-15 f294f52): when these 404'd on the first real USB
    #        boot, the orb never animated, hero input never wired, onboarding
    #        never fired, the logo broke.  Also: the OLD default /static
    #        prefix must NOT serve shell assets (repointed, not duplicated).
    pin_aura_active_theme(monkeypatch, tmp_path)
    html = client.get('/').get_data(as_text=True)

    refs = sorted(set(
        re.findall(r'(?:src|href)="(/shell/static/[^"]+)"', html)
        + re.findall(r"url\('(/shell/static/[^']+)'\)", html)))
    assert refs, 'shell HTML references no /shell/static assets -- render changed?'
    assert '/shell/static/voiceOrbViz.js' in refs      # the breathing orb
    assert '/shell/static/hartPersonalize.js' in refs  # chapter 02's surface
    assert '/shell/static/MaterialSymbolsRounded.woff2' in refs  # every glyph

    broken = []
    for url in refs:
        r = client.get(url)
        if r.status_code != 200 or not r.data:
            broken.append((url, r.status_code))
    assert not broken, (
        'dead-husk: the served page references assets the app does not '
        'serve: %r' % broken)

    # single static source of truth: the old /static prefix stays dead
    assert client.get('/static/voiceOrbViz.js').status_code == 404


def test_04_boot_data_apps_listing(client):
    # ENTRY  the booted page's start menu asks which OS apps exist:
    #        GET /api/shell/apps -> shell_apps()  [liquid_ui_service.py]
    #        -> os.listdir over /usr/share/applications and
    #           ~/.local/share/applications, collecting *.desktop entries as
    #           {'id': fname-minus-.desktop, 'name': titlecased, 'subsystem':
    #           'linux'}, capped at 100
    # BRANCH on this dev box neither .desktop dir exists -> the loop skips
    #        both -> the CONTROLLED empty listing (the start menu falls back
    #        to the inlined MANIFEST panels, which is why the desktop is
    #        never empty even with zero native apps)
    # DATA   {'apps': [...]} -- every entry, when present, is a linux
    #        .desktop id; never more than 100
    # SINK   200 application/json to the start-menu JS
    resp = client.get('/api/shell/apps')
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body['apps'], list)
    assert len(body['apps']) <= 100
    for entry in body['apps']:
        assert entry['subsystem'] == 'linux'
        assert entry['id'] and entry['name']


def test_05_boot_data_context_snapshot_degrades_offline(client):
    # ENTRY  GET /api/context -> api_context() [liquid_ui_service.py]
    #        -> ContextEngine.get_context()
    #           PARALLEL fan-in of four signal sources, aggregated into one dict:
    #           -> _get_device_context(): /etc/hart/variant + capability_tier
    #              files (absent here -> 'unknown'), local clock -> hour /
    #              time_of_day / day_of_week
    #           -> _get_model_context(): pooled_get http://localhost:6790/v1/models
    #              -- the peer HTTP boundary REFUSES instantly (conftest
    #              no_network, same as an offline node) -> except-swallow ->
    #              {'available': False, 'models': [], 'count': 0}
    #           -> _get_agent_context(): pooled_get :6777/api/social/dashboard/agents
    #              -> same refusal -> {'running': 0, 'total': 0, 'agents': []}
    #           -> _get_system_context(): reads /proc/loadavg, /proc/meminfo,
    #              /proc/uptime -- absent on this box -> {} (each read has its
    #              own logged except; no key is fabricated)
    # DATA   context = {timestamp, device{...}, models{available:False},
    #        agents{running:0}, system{}} -- the OFFLINE-FIRST floor: the
    #        shell renders context-aware UI even with every peer service down
    # SINK   200 application/json; the same dict is cached on self._cache
    resp = client.get('/api/context')
    assert resp.status_code == 200
    ctx = resp.get_json()
    assert {'timestamp', 'device', 'models', 'agents', 'system'} <= set(ctx)
    assert ctx['models'] == {'available': False, 'models': [], 'count': 0}
    assert ctx['agents'] == {'running': 0, 'total': 0, 'agents': []}
    assert ctx['device']['time_of_day'] in (
        'morning', 'afternoon', 'evening', 'night')
    assert isinstance(ctx['system'], dict)


def test_06_boot_data_event_feed_tails_journal(client, fake_os):
    # ENTRY  GET /api/shell/events -> shell_events() [liquid_ui_service.py]
    #        -> subprocess.run(['journalctl', '--since', '1 hour ago',
    #           '-p', '0..5', '--no-pager', '-o', 'short', '-n', '50'])
    #           (intercepted by fake_os -- nothing execs on this machine)
    #        -> LOOP over stdout lines: line.split(None, 3) -> time = first
    #           two whitespace chunks joined, message = the 4th chunk
    # DATA   canned journal:
    #        'Jul 17 09:00:01 hartnode systemd[1]: Started HART Liquid UI shell.'
    #        ==> {'time': 'Jul 17',
    #             'message': 'hartnode systemd[1]: Started HART Liquid UI shell.'}
    #        (note what the code ACTUALLY does: the seconds field lands in
    #        the message-side chunk count, so 'time' is only 'Mon DD' -- the
    #        split(None, 3) keeps 09:00:01 out of 'time'; narrated as-is)
    # SINK   200 {'events': [...]} to the notifications panel
    fake_os.stdout_for['journalctl'] = (
        'Jul 17 09:00:01 hartnode systemd[1]: Started HART Liquid UI shell.\n'
        'Jul 17 09:00:02 hartnode hart-comp[812]: DRM master acquired')
    resp = client.get('/api/shell/events')
    assert resp.status_code == 200
    events = resp.get_json()['events']
    assert events[0] == {
        'time': 'Jul 17',
        'message': 'hartnode systemd[1]: Started HART Liquid UI shell.'}
    assert events[1]['message'] == 'hartnode hart-comp[812]: DRM master acquired'

    # the handler issued EXACTLY the bounded journalctl tail, nothing else
    assert ['journalctl', '--since', '1 hour ago', '-p', '0..5',
            '--no-pager', '-o', 'short', '-n', '50'] in fake_os.calls


def test_07_console_errors_sink_to_journal(client, caplog):
    # ENTRY  the page's inline head script wraps window.onerror /
    #        unhandledrejection / console.error and POSTs each record here --
    #        the FIX-B blind-spot closer: WebView JS consoles never reach
    #        journald on their own.
    #        POST /api/shell/clientlog -> shell_clientlog() [liquid_ui_service.py]
    #        -> request.get_data(cache=True) (<= 8192 bytes -> accepted)
    #        -> get_json re-parses the SAME cached buffer
    #        -> fields clamped: message[:2000], stack[:4000], url[:500]
    #        -> level 'error' -> logger.error('[shell-client] (url:line:col)
    #           message\nstack') on logger 'hevolve.liquid_ui'
    # DATA   {'level':'error','message':'hartHome.compose is not a function',
    #         'url':'/shell/static/hartHome.js','line':12,'col':5,
    #         'stack':'TypeError: ...'}
    #        ==> log text '[shell-client] (/shell/static/hartHome.js:12:5)
    #            hartHome.compose is not a function' + stack on the next line
    # SINK   the module logger -> on the node, the service's stdout/journald
    #        (systemd-cat wrap, the 2026-07-11 journald blind-spot fix); the
    #        HTTP reply is ALWAYS 200 {'ok': True} -- a logging failure may
    #        never break the shell
    caplog.set_level(logging.WARNING, logger='hevolve.liquid_ui')
    resp = client.post('/api/shell/clientlog', json={
        'level': 'error',
        'message': 'hartHome.compose is not a function',
        'url': '/shell/static/hartHome.js', 'line': 12, 'col': 5,
        'stack': 'TypeError: hartHome.compose is not a function',
    })
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True}

    records = [r for r in caplog.records if '[shell-client]' in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    msg = records[0].getMessage()
    assert ('[shell-client] (/shell/static/hartHome.js:12:5) '
            'hartHome.compose is not a function') in msg
    assert 'TypeError: hartHome.compose is not a function' in msg


def test_08_console_sink_warn_and_oversize_branches(client, caplog):
    # BRANCH A (sequential continuation of beat 7): level 'warn'
    #        -> shell_clientlog routes the record to logger.warning instead of
    #           logger.error (the only difference; same clamps, same sink)
    # BRANCH B: an oversize body (> 8192 bytes raw) -> the bounded sink DROPS
    #        the record BEFORE parsing (no log line at all) and still replies
    #        200 {'ok': True} -- a hostile/looping client cannot flood
    #        journald through this ingress, and the shell never sees an error
    # SINK   both branches: 200 {'ok': True}; only branch A reaches the logger
    caplog.set_level(logging.WARNING, logger='hevolve.liquid_ui')

    resp = client.post('/api/shell/clientlog', json={
        'level': 'warn', 'message': 'slow frame: compose took 180ms'})
    assert resp.status_code == 200 and resp.get_json() == {'ok': True}
    warns = [r for r in caplog.records if '[shell-client]' in r.getMessage()]
    assert len(warns) == 1 and warns[0].levelno == logging.WARNING
    assert 'slow frame: compose took 180ms' in warns[0].getMessage()

    caplog.clear()
    resp = client.post('/api/shell/clientlog', json={
        'level': 'error', 'message': 'x' * 9000})   # raw body > 8192
    assert resp.status_code == 200 and resp.get_json() == {'ok': True}
    assert not [r for r in caplog.records if '[shell-client]' in r.getMessage()], (
        'oversize record must be dropped unlogged (bounded sink)')
