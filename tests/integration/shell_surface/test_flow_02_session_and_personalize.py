"""CHAPTER 02 -- SESSION & PERSONALIZE (the shell-surface story, part 2 of N).

Chapter 01 booted the node and painted the aura shell.  The user is now AT the
desktop.  This chapter follows what a live session does next, in flow order:
restore the session's panel layout, gate the power menu, open Personalize,
walk the theme gallery, apply a preset, overlay a palette, pick a wallpaper,
and read the Personalize-adjacent state -- closing the loop by proving a
personalize change reaches the NEXT first paint.

TWO ORIGINS, ONE ENGINE (the load-bearing topology of this chapter): the
shell page is served by LiquidUIService (this suite's `client`), but the
Personalize hub's theme fetches go to `const BACKEND =
'http://localhost:6777'` -- the HARTOS backend, where integrations/social/
__init__.py registers theme_bp (integrations/social/api_theme.py).  The
/api/appearance/* namespace does NOT exist on the shell app.  Both origins
converge on the ONE ThemeService engine (integrations/agent_engine/
theme_service.py) and its two disk artifacts, active_theme.json +
theme_custom.json -- so a theme applied through :6777 retints the page the
shell origin serves.  Tests below drive the shell leg through the recording
`client` and the backend leg through a theme_bp app mounted EXACTLY the way
integrations/social/__init__.py mounts it.

Chapter 02 beats, in flow order:
  1. session state    -> GET/POST /api/shell/session-state (panel layout)
  2. power menu gate  -> GET /api/shell/session/firmware-capable
  3. theme gallery    -> GET /api/appearance/presets (backend origin; the
                         shell origin 404s -- narrated)
  4. apply preset     -> POST /api/appearance/apply + the shell's own
                         POST /api/theme -> disk + gsettings + theme.changed
  5. refusal branches -> unknown preset (404), empty body (400)
  6. palette overlay  -> customized overrides -> /api/appearance/css ->
                         the NEXT GET / carries the new secondary accent
  7. wallpaper        -> GET state + POST set (X11/feh branch)
  8. wallpaper        -> POST set (Wayland/swaymsg branch)
  9. wallpaper        -> GET collection (offline NixOS-dir scan)
 10. adjacent reads   -> GET /api/shell/accessibility + /api/shell/sounds/themes
 11. loop closure     -> PUT a11y font_scale -> the NEXT GET / is rescaled
"""
import json
import os

import pytest

from .test_flow_01_boot_and_first_paint import pin_aura_active_theme


@pytest.fixture()
def backend_client():
    """The BACKEND (:6777) leg of the story: a Flask app carrying theme_bp,
    mounted exactly as integrations/social/__init__.py lines 303-304 do it
    (`from .api_theme import theme_bp; app.register_blueprint(theme_bp)`).
    Same blueprint object, same handlers, same ThemeService engine."""
    from flask import Flask
    from integrations.social.api_theme import theme_bp
    backend = Flask('hartos_backend_theme_leg')
    backend.register_blueprint(theme_bp)
    backend.testing = True
    return backend.test_client()


@pytest.fixture()
def theme_event_spy(monkeypatch):
    """Observe the ONE notification path ThemeService fires: core.platform.
    events.emit_event -> EventBus -> WAMP -> every visual subsystem.  In this
    process no EventBus is bootstrapped, so emit_event would no-op; the spy
    records (topic, payload) at the exact boundary instead."""
    events = []
    monkeypatch.setattr(
        'core.platform.events.emit_event',
        lambda topic, data=None, async_=True: events.append((topic, data)))
    return events


def test_01_session_state_roundtrip(client):
    # ENTRY  the shell restores the last session's panel layout on login:
    #        GET /api/shell/session-state -> get_session_state()
    #        [liquid_ui_service.py] -> reads <self._data_dir>/shell_session.json
    #        (self._data_dir = HEVOLVE_DATA_DIR | HART_DATA_DIR |
    #        <repo>/agent_data, fixed at construction) -> file content, or {}
    #        when absent/unreadable (logged, never a 500)
    # THEN   the session evolves (user moves panels, switches workspace) and
    #        the shell persists: POST /api/shell/session-state ->
    #        save_session_state() -> os.makedirs + json.dump of the EXACT
    #        request body (no validation, no merge -- last write wins)
    # DATA   {'panels': [{'id':'wallpaper_manager','x':120,'y':80}],
    #         'workspace': 2}  ==> written verbatim ==> read back verbatim
    # SINK   POST: 200 {'status':'saved'}; GET: 200 <the same dict> -- this
    #        is what makes panel positions survive logout/login.
    #        (The test restores the pre-test state afterwards: the data dir is
    #        real, and this suite must leave the box as it found it.)
    original = client.get('/api/shell/session-state')
    assert original.status_code == 200
    original_state = original.get_json()
    assert isinstance(original_state, dict)

    layout = {'panels': [{'id': 'wallpaper_manager', 'x': 120, 'y': 80}],
              'workspace': 2}
    try:
        resp = client.post('/api/shell/session-state', json=layout)
        assert resp.status_code == 200
        assert resp.get_json() == {'status': 'saved'}

        back = client.get('/api/shell/session-state')
        assert back.status_code == 200
        assert back.get_json() == layout
    finally:
        restore = client.post('/api/shell/session-state', json=original_state)
        assert restore.status_code == 200


def test_02_power_menu_firmware_capability_gate(client):
    # ENTRY  the power menu decides whether to SHOW 'Restart into Firmware
    #        (UEFI)': GET /api/shell/session/firmware-capable ->
    #        shell_session_firmware_capable() [liquid_ui_service.py]
    #        -> firmware_setup_supported() [shell_os_apis.py] -- the ONE
    #           canonical probe (DRY: the POST /api/shell/session/firmware
    #           action gates on the SAME function, so menu and action can
    #           never drift):
    #           1. os.path.isdir('/sys/firmware/efi')  (UEFI-booted at all?)
    #           2. read efivar OsIndicationsSupported-8be4df61-... and test
    #              bit 0 (EFI_OS_INDICATIONS_BOOT_TO_FW_UI); 4-byte attr
    #              prefix + 8-byte LE value; unreadable => conservative False
    # BRANCH on this dev box /sys/firmware/efi does not exist -> step 1
    #        short-circuits False -> the button stays HIDDEN (a user on
    #        legacy BIOS must never get a plain reboot when they asked for
    #        firmware setup)
    # DATA   probe verdict ==> {'supported': <verdict>} -- asserted against
    #        the canonical probe itself so the route can never fork from it
    # SINK   200 application/json to the power-menu JS
    from integrations.agent_engine.shell_os_apis import firmware_setup_supported
    resp = client.get('/api/shell/session/firmware-capable')
    assert resp.status_code == 200
    assert resp.get_json() == {'supported': firmware_setup_supported()}


def test_03_presets_gallery_lives_on_backend_origin(client, backend_client):
    # ENTRY  the user opens Personalize (hartPersonalize.js).  The theme
    #        gallery fetch goes to BACKEND+'/api/appearance/presets'.
    # LEG 1  the SHELL origin (this suite's `client`): /api/appearance/* is
    #        NOT registered on LiquidUIService's app.  Unclaimed paths fall to
    #        the Nunba reverse-proxy catch-all ONLY when HART_NUNBA_SOCKET or
    #        NUNBA_STATIC_DIR is set; on this hermetic box neither is -> no
    #        catch-all rule exists -> Flask 404.  CONTROLLED: the shell JS
    #        never fetches appearance from its own origin, so this 404 is the
    #        proof the namespace lives elsewhere, not a defect.
    # LEG 2  the BACKEND origin (:6777, theme_bp): GET /api/appearance/presets
    #        -> list_presets() [integrations/social/api_theme.py]
    #        -> ThemeService.list_presets() [theme_service.py]: scans the REAL
    #           preset dir nixos/assets/conky-themes/*.json (sorted), pulling
    #           id/name/description/category + the 4-colour swatch
    #           (accent/secondary/background/surface) per preset
    # DATA   aura.json ==> {'id':'aura','name':'Aura','category':'dark',
    #        'accent':'00E6C3','secondary':'9B5CFF',...} among the shipped
    #        gallery (arctic, aura, cyberpunk, forest, hart-default,
    #        high-contrast, midnight, minimal, potato, sunset)
    # SINK   200 {'presets': [...]} -> the gallery the user scrolls
    assert client.get('/api/appearance/presets').status_code == 404

    resp = backend_client.get('/api/appearance/presets')
    assert resp.status_code == 200
    presets = resp.get_json()['presets']
    by_id = {p['id']: p for p in presets}
    assert {'aura', 'hart-default', 'high-contrast', 'potato'} <= set(by_id)
    aura = by_id['aura']
    assert aura['name'] == 'Aura'
    assert aura['category'] == 'dark'
    assert aura['accent'] == '00E6C3'
    assert aura['secondary'] == '9B5CFF'


def test_04_apply_preset_persists_broadcasts_and_tints_gtk(
        client, backend_client, fake_os, theme_event_spy,
        monkeypatch, tmp_path):
    # ENTRY  the user clicks the Aura card; hartPersonalize.js applyPreset
    #        POSTs BACKEND+'/api/appearance/apply' {'theme_id':'aura'}
    #        -> apply_theme() [integrations/social/api_theme.py]
    #        -> ThemeService.apply_theme('aura') [theme_service.py]:
    #           1. get_preset('aura') -- nixos/assets/conky-themes/aura.json
    #           2. json.dump(preset) -> _ACTIVE_THEME_PATH (active_theme.json,
    #              the file Conky's Lua re-reads every 5s and the file the
    #              NEXT render_desktop_shell paints from)
    #           3. delete _CUSTOM_OVERRIDES_PATH (new preset = fresh start)
    #           4. _apply_gtk: subprocess.Popen(['gsettings', 'set',
    #              'org.gnome.desktop.interface', 'color-scheme',
    #              'prefer-dark'])  (aura.gtk_prefer_dark True; intercepted
    #              by fake_os -- argv recorded, nothing execs)
    #           5. emit_event('theme.changed', {'theme_id','preset'})
    #              [core/platform/events.py] -- EventBus -> WAMP fan-out; the
    #              spy records it at the boundary
    # DATA   aura preset dict ==> active_theme.json on disk ==>
    #        {'status':'applied','theme_id':'aura','theme':{...},'custom':None}
    # SINK   200 to the Personalize hub, which then live-swaps CSS via
    #        /api/appearance/css (beat 6 drives that surface)
    #
    # SAME ENGINE ON THE SHELL ORIGIN: POST /api/theme (update_theme()
    # [liquid_ui_service.py]) drives the SAME ThemeService.apply_theme.
    # (FIXED 2026-07-17: this reply used to read result.get('id') -- a key
    # apply_theme never returns -- so 'theme' was permanently None. It now
    # echoes result['theme_id'], so the caller can confirm WHICH theme the
    # shell just applied.)
    pin_aura_active_theme(monkeypatch, tmp_path)
    from integrations.agent_engine import theme_service as ts

    resp = backend_client.post('/api/appearance/apply',
                               json={'theme_id': 'aura'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'applied'
    assert body['theme_id'] == 'aura'
    assert body['theme']['colors']['accent'] == '00E6C3'
    assert body['custom'] is None

    # 2. the persisted artifact the next paint reads
    on_disk = json.loads(open(ts._ACTIVE_THEME_PATH, encoding='utf-8').read())
    assert on_disk['id'] == 'aura'
    # 4. the GTK tint left the process boundary with exactly this argv
    assert ['gsettings', 'set', 'org.gnome.desktop.interface',
            'color-scheme', 'prefer-dark'] in fake_os.calls
    # 5. the one broadcast every visual subsystem subscribes to
    assert ('theme.changed' in [t for t, _ in theme_event_spy])
    topic, payload = [e for e in theme_event_spy if e[0] == 'theme.changed'][0]
    assert payload['theme_id'] == 'aura'
    assert payload['preset']['id'] == 'aura'

    # the shell origin's own hot-reload verb, same engine, reply names the theme
    resp2 = client.post('/api/theme', json={'theme_id': 'aura'})
    assert resp2.status_code == 200
    assert resp2.get_json() == {'status': 'updated', 'theme': 'aura'}


def test_05_apply_unknown_preset_and_empty_body_refusals(
        client, backend_client, monkeypatch, tmp_path):
    # BRANCH (sequential continuation of beat 4): the unknown-id path.
    #        ThemeService.apply_theme('solarpunk') -> get_preset misses
    #        (no nixos/assets/conky-themes/solarpunk.json) -> returns
    #        {'error': 'Unknown theme: solarpunk'} BEFORE touching disk,
    #        gsettings, or the EventBus -- nothing is half-applied.
    #        -> theme_bp maps the error dict to 404
    #        -> the shell's /api/theme maps the same error dict to 404
    # BRANCH the empty-body path never reaches the engine at all:
    #        theme_bp: no theme_id AND no palette -> 400
    #          {'error': 'theme_id or palette (secondary_accent/custom) required'}
    #        shell /api/theme: no theme_id -> 400 {'error': 'theme_id required'}
    # SINK   controlled 4xx refusals; the active theme on disk is untouched
    pin_aura_active_theme(monkeypatch, tmp_path)
    from integrations.agent_engine import theme_service as ts
    before = open(ts._ACTIVE_THEME_PATH, encoding='utf-8').read()

    resp = backend_client.post('/api/appearance/apply',
                               json={'theme_id': 'solarpunk'})
    assert resp.status_code == 404
    assert resp.get_json() == {'error': 'Unknown theme: solarpunk'}

    resp = client.post('/api/theme', json={'theme_id': 'solarpunk'})
    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'Unknown theme: solarpunk'

    assert backend_client.post('/api/appearance/apply',
                               json={}).status_code == 400
    resp = client.post('/api/theme', json={})
    assert resp.status_code == 400
    assert resp.get_json() == {'error': 'theme_id required'}

    assert open(ts._ACTIVE_THEME_PATH, encoding='utf-8').read() == before, (
        'a refused apply must not touch the persisted active theme')


def test_06_palette_overlay_hot_swap_reaches_next_paint(
        client, backend_client, theme_event_spy, monkeypatch, tmp_path):
    # ENTRY  the Personalize palette picker (#161) posts a palette WITHOUT a
    #        preset switch: POST /api/appearance/apply
    #        {'secondary_accent': 'FF2E9A'}
    #        -> ThemeService.apply_theme(theme_id='', secondary_accent=...)
    #           -> theme_id empty -> the preset block is SKIPPED entirely
    #           -> _palette_overrides normalizes 'FF2E9A' -> '#FF2E9A' under
    #              colors.secondary
    #           -> update_custom({'colors': {'secondary': '#FF2E9A'}}):
    #              deep-merge into _CUSTOM_OVERRIDES_PATH (theme_custom.json)
    #              + re-write active_theme.json with overrides applied
    #              + emit_event('theme.custom_updated', {'overrides': ...})
    # DATA   reply {'status':'customized',
    #               'overrides':{'colors':{'secondary':'#FF2E9A'}}}
    # THEN   applyPreset's success path live-swaps: GET /api/appearance/css
    #        -> get_css() [api_theme.py] -> ThemeService.get_css_variables()
    #        -> aura merged with the overlay ==> '--hart-a2: #FF2E9A;' +
    #           '--hart-a2-rgb: 255,46,154;' as text/css (the page injects it
    #           into the managed <style id=hart-theme-live> -- no reload, G3)
    # SINK   loop closure across origins: the SHELL origin's NEXT GET /
    #        (render_desktop_shell -> the same get_css_variables) now paints
    #        the pink secondary -- the personalize choice survives a reload
    #        because both origins read the same two files on disk.
    pin_aura_active_theme(monkeypatch, tmp_path)
    from integrations.agent_engine import theme_service as ts

    resp = backend_client.post('/api/appearance/apply',
                               json={'secondary_accent': 'FF2E9A'})
    assert resp.status_code == 200
    assert resp.get_json() == {
        'status': 'customized',
        'overrides': {'colors': {'secondary': '#FF2E9A'}}}

    overrides = json.loads(
        open(ts._CUSTOM_OVERRIDES_PATH, encoding='utf-8').read())
    assert overrides == {'colors': {'secondary': '#FF2E9A'}}
    assert ('theme.custom_updated', {'overrides': overrides}) in theme_event_spy

    css = backend_client.get('/api/appearance/css')
    assert css.status_code == 200
    assert css.content_type.startswith('text/css')
    css_text = css.get_data(as_text=True)
    assert '--hart-a2: #FF2E9A;' in css_text
    assert '--hart-a2-rgb: 255,46,154;' in css_text
    assert '--hart-accent: #00E6C3;' in css_text   # aura base survives

    html = client.get('/').get_data(as_text=True)
    assert '--hart-a2: #FF2E9A;' in html, (
        'the palette overlay did not reach the shell origin\'s next paint')


def _wallpaper_cfg_snapshot():
    """The wallpaper config artifact both wallpaper tests mutate:
    <HART_CONFIG_DIR|~/.config/hart>/wallpaper.json (shell_desktop_apis.
    _HART_CONFIG is the module's own resolved constant -- read it, don't
    re-derive it).  Returns (path, original_bytes_or_None) for restore."""
    from integrations.agent_engine import shell_desktop_apis as sda
    path = os.path.join(sda._HART_CONFIG, 'wallpaper.json')
    original = None
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            original = f.read()
    return path, original


def _wallpaper_cfg_restore(path, original):
    if original is None:
        if os.path.isfile(path):
            os.remove(path)
    else:
        with open(path, 'wb') as f:
            f.write(original)


def test_07_wallpaper_state_and_set_x11_branch(client, fake_os, monkeypatch):
    # ENTRY  the Wallpaper panel opens: GET /api/shell/wallpaper ->
    #        shell_wallpaper() [shell_desktop_apis.py] -> _load_json of
    #        ~/.config/hart/wallpaper.json, defaulting to
    #        {'current':'','lock_screen':'','mode':'fill','slideshow':{...}}
    #        NOTE what the code ACTUALLY does: the default is WHOLE-FILE, not
    #        per-key -- when the file exists, its content is returned
    #        verbatim, so a partial cfg (e.g. only {'slideshow': ...}, which
    #        the slideshow route writes when no cfg existed before) yields a
    #        partial GET with no 'current'/'mode' keys.  The panel JS must
    #        tolerate missing keys; this test asserts only dict-shape here
    #        and asserts the full keys after the write below.
    # THEN   the user picks an image: POST /api/shell/wallpaper/set
    #        {'path': '/usr/share/backgrounds/hart/aura.png', 'mode': 'fill'}
    #        -> shell_wallpaper_set():
    #        BRANCH _is_wayland()? -- WAYLAND_DISPLAY / XDG_SESSION_TYPE are
    #        cleared here (and pgrep only runs on sys.platform=='linux'), so
    #        the X11 branch runs:
    #           mode 'fill' -> _run(['feh', '--bg-fill', <path>])
    #           (fake_os records the argv; nothing paints on this box)
    #        -> cfg['current']=path, cfg['mode']=mode -> _save_json
    # DATA   {'path':..., 'mode':'fill'} ==> feh argv + persisted cfg ==>
    #        reply {'set': True, 'path':..., 'mode': 'fill'}
    # SINK   the follow-up GET reads the persisted choice back -- the
    #        wallpaper survives a session restart.  (Config restored after.)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.delenv('XDG_SESSION_TYPE', raising=False)
    path, original = _wallpaper_cfg_snapshot()
    try:
        state = client.get('/api/shell/wallpaper')
        assert state.status_code == 200
        cfg = state.get_json()
        assert isinstance(cfg, dict)   # file-verbatim or whole-file default

        wp = '/usr/share/backgrounds/hart/aura.png'
        resp = client.post('/api/shell/wallpaper/set',
                           json={'path': wp, 'mode': 'fill'})
        assert resp.status_code == 200
        assert resp.get_json() == {'set': True, 'path': wp, 'mode': 'fill'}
        assert ['feh', '--bg-fill', wp] in fake_os.calls

        after = client.get('/api/shell/wallpaper').get_json()
        assert after['current'] == wp
        assert after['mode'] == 'fill'
    finally:
        _wallpaper_cfg_restore(path, original)


def test_08_wallpaper_set_wayland_branch(client, fake_os, monkeypatch):
    # BRANCH (the deployed node's real path, sequential continuation of
    #        beat 7): on the NixOS node the shell runs under a Wayland
    #        compositor, so WAYLAND_DISPLAY is set -> _is_wayland() True on
    #        its FIRST check (no pgrep needed) -> shell_wallpaper_set issues
    #        swaymsg instead of feh:
    #           _run(['swaymsg', 'output', '*', 'bg', <path>, <mode>])
    #        -- one call regardless of mode (sway takes the mode inline).
    # DATA   same persistence as beat 7: cfg.current/mode saved to
    #        wallpaper.json; reply {'set': True, ...}
    # SINK   every output ('*') repaints; the choice persists.  (Restored.)
    monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-1')
    path, original = _wallpaper_cfg_snapshot()
    try:
        wp = '/usr/share/backgrounds/hart/nebula.png'
        resp = client.post('/api/shell/wallpaper/set',
                           json={'path': wp, 'mode': 'fit'})
        assert resp.status_code == 200
        assert resp.get_json() == {'set': True, 'path': wp, 'mode': 'fit'}
        assert ['swaymsg', 'output', '*', 'bg', wp, 'fit'] in fake_os.calls
        assert not any(c and c[0] == 'feh' for c in fake_os.calls
                       if isinstance(c, list)), (
            'wayland branch must not also fire the X11 feh path')
    finally:
        _wallpaper_cfg_restore(path, original)


def test_09_wallpaper_collection_offline_scan(client):
    # ENTRY  the Wallpaper panel's picker grid: GET
    #        /api/shell/wallpaper/collection (no ?directory=) ->
    #        shell_wallpaper_collection() [shell_desktop_apis.py]
    #        -> _WALLPAPER_DIRS(): HART_WALLPAPER_DIR override first, then the
    #           NixOS-valid /run/current-system/sw/share/backgrounds, then
    #           ~/.local/share/backgrounds, then FHS /usr/share/backgrounds
    #           (the OFFLINE contract: bundled assets only, no network)
    #        -> _collect_wallpapers: each dir + ONE subdir level (gnome ships
    #           under .../gnome), image extensions only, capped at 240
    # BRANCH on this dev box none of those dirs exist -> os.listdir OSError
    #        is swallowed per-dir -> the CONTROLLED empty gallery
    # DATA   {'images': [...], 'count': len(images),
    #         'directories': [only dirs that exist]}
    # SINK   200 to the picker; an empty gallery renders the picker's own
    #        empty state, never an error toast
    resp = client.get('/api/shell/wallpaper/collection')
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body['images'], list)
    assert body['count'] == len(body['images'])
    assert body['count'] <= 240
    for d in body['directories']:
        assert os.path.isdir(d)


def test_10_personalize_adjacent_state_reads(client):
    # ENTRY  Personalize-adjacent panels read their state on open.
    # READ 1 GET /api/shell/accessibility -> shell_accessibility_get()
    #        [shell_os_apis.py] -> the module-level _A11Y_SETTINGS dict --
    #        the SAME live dict render_desktop_shell consumes (one state, two
    #        readers).  Seeded at import from /etc/hart/accessibility.json;
    #        absent on this box -> the shipped defaults.
    # READ 2 GET /api/shell/sounds/themes -> shell_sounds_themes()
    #        [shell_desktop_apis.py] -> scans /usr/share/sounds +
    #        /run/current-system/sw/share/sounds for theme dirs;
    # BRANCH neither dir exists here -> the fallback single entry
    #        {'id':'freedesktop','name':'FreeDesktop (default)','path':''} --
    #        the panel is never empty; active/enabled come from
    #        ~/.config/hart/sound-theme.json (defaults when absent)
    # SINK   two 200s; both feed the Personalize hub's settings sections
    a11y = client.get('/api/shell/accessibility')
    assert a11y.status_code == 200
    state = a11y.get_json()
    assert set(state) == {'font_scale', 'high_contrast', 'reduced_motion',
                          'large_cursor', 'screen_reader', 'sticky_keys'}

    sounds = client.get('/api/shell/sounds/themes')
    assert sounds.status_code == 200
    body = sounds.get_json()
    assert body['themes'], 'sound themes must never be empty (fallback entry)'
    for t in body['themes']:
        assert t['id'] and t['name']
    assert 'active' in body and 'enabled' in body


def test_11_a11y_font_scale_reaches_next_paint(client, monkeypatch, tmp_path):
    # ENTRY  the user drags the Personalize font-size slider;
    #        hartPersonalize.js PUTs /api/shell/accessibility
    #        {'font_scale': 1.5} -> shell_accessibility_set()
    #        [shell_os_apis.py] -> mutates the module-level _A11Y_SETTINGS
    #        dict in place (runtime override of the NixOS-declared seed)
    # THEN   loop closure -- the NEXT first paint: GET / ->
    #        render_desktop_shell() reads get_a11y_settings() (the SAME dict)
    #        -> _fs=1.5 differs from 1.0 -> emits the a11y override AFTER
    #        {css_vars} (later source wins):
    # DATA   aura font.size 14, heading_size 26, shell.icon_size 20, all
    #        scaled by 1.5 and rounded ==>
    #        ':root{--hart-font-size:21px;--hart-heading-size:39px;
    #         --hart-icon-size:30px}'  (exact concatenation, no spaces)
    #        -- chapter 01 beat 2 asserted this block is ABSENT at scale 1.0;
    #        together the two beats pin both sides of the branch
    # SINK   the rescaled page; the PUT is restored afterwards so the shared
    #        module state leaves the suite as it entered
    pin_aura_active_theme(monkeypatch, tmp_path)
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '0')
    original = client.get('/api/shell/accessibility').get_json()

    try:
        resp = client.put('/api/shell/accessibility',
                          json={'font_scale': 1.5})
        assert resp.status_code == 200
        assert resp.get_json()['font_scale'] == 1.5

        html = client.get('/').get_data(as_text=True)
        assert (':root{--hart-font-size:21px;'
                '--hart-heading-size:39px;'
                '--hart-icon-size:30px}') in html, (
            'the a11y font_scale override did not reach the next paint')
    finally:
        restore = client.put('/api/shell/accessibility',
                             json={'font_scale': original['font_scale']})
        assert restore.status_code == 200
