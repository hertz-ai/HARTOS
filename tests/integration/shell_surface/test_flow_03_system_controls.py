"""CHAPTER 03 -- SYSTEM CONTROLS: the OS-mutating half of the deployed shell.

Previously in this suite: the shell surface answered questions. In this chapter
it starts giving ORDERS -- power the box down, relight the screen, turn the
volume knob, format a disk. Every one of those orders must cross exactly one
door: the subprocess/D-Bus boundary. This chapter walks each request in
TOPOLOGICAL order (entry -> validation -> boundary argv -> parse -> response
sink) against the REAL served app (`client` over
LiquidUIService._create_flask_app()) with the hermetic FakeOS standing in for
the machine, so a poweroff test can never power anything off.

One hop is shared by every power verb in this chapter, so it is narrated once
here and referenced below:

    shell_os_apis._logind_call(method, *args)     [shell_os_apis.py:257]
      -> os_bridge.logind.logind_call             [os_bridge/logind.py:183]
         1) NATIVE jeepney D-Bus first            [logind.py:198-221] -- on this
            dev box (and any box without a reachable SYSTEM bus) the native
            attempt returns None ("transport unavailable"), it is NEVER a
            subprocess, so FakeOS does not see it;
         2) bounded busctl fallback               [logind.py:161-180] -- THE
            observable boundary:
            subprocess.run(['busctl','call','--system',
                            'org.freedesktop.login1','/org/freedesktop/login1',
                            'org.freedesktop.login1.Manager', <Method>, *args],
                           capture_output=True, text=True, timeout=10)
            result-checked: rc!=0 -> (False, 'logind <M> denied or failed: ...')
            so a polkit denial can never masquerade as success (#133).

Rules of the chapter: behavioural asserts only, every test drives `client`
(feeding the 100% HITS registry), forced failures come from fake_os.rc_for,
invalid input must leave the boundary log EMPTY, and a raw 500 on a plain
request is a REAL FINDING (xfail'd as DEFECT, story continues).
"""
import os

import pytest

# The one canonical login1 busctl prefix (mirrors os_bridge/logind.py:167-168).
LOGIN1 = ['busctl', 'call', '--system',
          'org.freedesktop.login1', '/org/freedesktop/login1',
          'org.freedesktop.login1.Manager']


def _login1(method, *sig_args):
    """The exact argv logind_call's busctl fallback issues for `method`."""
    return LOGIN1 + [method, *sig_args]


# ═════════════════════════════════════════════════════════════════════════════
# ACT 1 -- POWER VERBS (/api/shell/power/action)
# ═════════════════════════════════════════════════════════════════════════════

def test_power_shutdown_reaches_login1_poweroff(client, fake_os):
    # ENTRY  POST /api/shell/power/action {'action': 'shutdown'}
    #   -> @_require_shell_auth passes (test client is 127.0.0.1)  [shell_os_apis.py:111]
    #   -> shell_os_apis.shell_power_action()  [shell_os_apis.py:974]
    #      -> validates action against valid_actions = list(_POWER_METHOD) +
    #         ['lock','firmware','uefi'] = ['reboot','shutdown','suspend',
    #         'hibernate','lock','firmware','uefi']  [shell_os_apis.py:1010]
    #      -> _audit_shell_op('power_action') best-effort  [shell_os_apis.py:1021]
    #      -> _logind_call(_POWER_METHOD['shutdown'], 'b', 'true')
    #         where _POWER_METHOD['shutdown'] == 'PowerOff'  [os_bridge/power.py:22]
    #      -> busctl fallback argv  [BOUNDARY - asserted via fake_os.calls]
    # DATA   action 'shutdown' ==> argv [...,'PowerOff','b','true']; the 'b true'
    #        is the login1 interactive boolean (logind may consult polkit);
    #        rc 0 ==> (True, None) ==> {'action':'shutdown','initiated':True}
    resp = client.post('/api/shell/power/action', json={'action': 'shutdown'})
    assert resp.status_code == 200
    assert resp.get_json() == {'action': 'shutdown', 'initiated': True}
    assert _login1('PowerOff', 'b', 'true') in fake_os.calls


@pytest.mark.parametrize('verb,login1_method', [
    ('reboot', 'Reboot'),
    ('suspend', 'Suspend'),
    ('hibernate', 'Hibernate'),
])
def test_power_verbs_map_to_login1_methods(client, fake_os, verb, login1_method):
    # ENTRY  POST /api/shell/power/action {'action': <verb>}
    #   -> shell_os_apis.shell_power_action()  [shell_os_apis.py:974]
    #      -> allowlist admits the verb  [shell_os_apis.py:1010-1012]
    #      -> _logind_call(_POWER_METHOD[verb], 'b', 'true')  [shell_os_apis.py:1032]
    # DATA   the verb->method map is the ONE canonical _POWER_METHOD
    #        [os_bridge/power.py:22]: reboot->Reboot, suspend->Suspend,
    #        hibernate->Hibernate; rc 0 ==> {'action':verb,'initiated':True}
    resp = client.post('/api/shell/power/action', json={'action': verb})
    assert resp.status_code == 200
    assert resp.get_json() == {'action': verb, 'initiated': True}
    assert _login1(login1_method, 'b', 'true') in fake_os.calls


def test_power_lock_uses_no_arg_locksessions(client, fake_os):
    # ENTRY  POST /api/shell/power/action {'action': 'lock'}
    #   -> shell_os_apis.shell_power_action()  [shell_os_apis.py:974]
    #      -> 'lock' is in the allowlist but NOT in _POWER_METHOD, so it takes
    #         its own branch: _logind_call('LockSessions')  [shell_os_apis.py:1029-1030]
    # DATA   LockSessions takes NO signature args, so the busctl argv ends at
    #        the method name (no 'b'/'true' suffix); rc 0 ==> initiated True
    resp = client.post('/api/shell/power/action', json={'action': 'lock'})
    assert resp.status_code == 200
    assert resp.get_json() == {'action': 'lock', 'initiated': True}
    assert _login1('LockSessions') in fake_os.calls


def test_power_invalid_verb_never_touches_the_boundary(client, fake_os):
    # ENTRY  POST /api/shell/power/action {'action': 'poweroff'}
    #   -> shell_os_apis.shell_power_action()  [shell_os_apis.py:974]
    #      -> 'poweroff' is NOT in the allowlist -- the surface's shutdown verb
    #         is 'shutdown' (mapping to login1 'PowerOff'); the systemctl-style
    #         'poweroff' spelling is rejected at the gate  [shell_os_apis.py:1011-1012]
    # DATA   invalid verb ==> 400 {'error': 'Invalid action. Valid: [...]'}
    # BRANCH validation runs BEFORE _audit and BEFORE _logind_call, so the OS
    #        boundary log stays EMPTY -- not one argv was even built. This is
    #        the constraint that makes the allowlist real: an unknown power
    #        verb cannot execute ANYTHING.
    resp = client.post('/api/shell/power/action', json={'action': 'poweroff'})
    assert resp.status_code == 400
    assert 'Invalid action' in resp.get_json()['error']
    assert fake_os.calls == []


def test_power_denied_by_polkit_surfaces_result_checked_error(client, fake_os):
    # ENTRY  POST /api/shell/power/action {'action': 'shutdown'}
    #   -> shell_power_action -> _logind_call('PowerOff','b','true')
    #      -> busctl exits NON-ZERO (forced via fake_os.rc_for -- the shape of a
    #         polkit denial)  [BOUNDARY]
    #      -> _busctl_logind_call checks rc: stderr/stdout empty ==> detail
    #         'exit code 1' ==> (False, 'logind PowerOff denied or failed:
    #         exit code 1')  [os_bridge/logind.py:177-179]
    # DATA   rc 1 ==> DELIBERATE 500 {'action':'shutdown','initiated':False,
    #        'error': 'logind PowerOff denied or failed: exit code 1'}
    #        [shell_os_apis.py:1034-1037]
    # BRANCH this is the #133 contract: the old code fire-and-forgot
    #        subprocess.Popen and masked denial as {'initiated': True}; the fix
    #        surfaces the REAL verdict as a structured error payload (a
    #        controlled, intentional 500 -- not an unhandled crash).
    fake_os.rc_for['busctl'] = 1
    resp = client.post('/api/shell/power/action', json={'action': 'shutdown'})
    assert resp.status_code == 500
    body = resp.get_json()
    assert body['initiated'] is False
    assert 'denied or failed' in body['error']
    assert _login1('PowerOff', 'b', 'true') in fake_os.calls


def test_power_firmware_verb_gated_on_uefi_capability(client, fake_os):
    # ENTRY  POST /api/shell/power/action {'action': 'firmware'}
    #   -> shell_power_action: 'firmware' IS in the allowlist, but before any
    #      logind call it must pass firmware_setup_supported()
    #      [shell_os_apis.py:1016-1019 -> 213]:
    #        1) os.path.isdir('/sys/firmware/efi') -- absent on this dev box
    #           (and on any legacy-BIOS box) ==> False immediately;
    #        2) (on UEFI) read the OsIndicationsSupported efivar and test the
    #           boot-to-fw-UI bit -- a pure FILE read, never a subprocess.
    # DATA   unsupported ==> 400 {'error': 'Reboot to firmware setup is not
    #        supported...'} -- the user asked for firmware setup, so a plain
    #        reboot would be the WRONG action; refusing is the design.
    # BRANCH boundary log stays EMPTY: neither the two-step arm
    #        (SetRebootToFirmwareSetup) nor the Reboot was issued.
    resp = client.post('/api/shell/power/action', json={'action': 'firmware'})
    assert resp.status_code == 400
    assert 'not supported' in resp.get_json()['error']
    assert fake_os.calls == []


# ═════════════════════════════════════════════════════════════════════════════
# ACT 2 -- POWER PROFILES (/api/shell/power/set)
# ═════════════════════════════════════════════════════════════════════════════

def test_power_profile_set_maps_to_powerprofilesctl(client, fake_os):
    # ENTRY  POST /api/shell/power/set {'profile': 'performance'}
    #   -> shell_os_apis.shell_power_set()  [shell_os_apis.py:955]
    #      -> validates profile in ('performance','balanced','powersave')
    #         [shell_os_apis.py:959]
    #      -> subprocess.run(['powerprofilesctl','set','performance'],
    #         timeout=5)  [BOUNDARY]
    # DATA   profile 'performance' ==> argv ['powerprofilesctl','set',
    #        'performance']; rc 0 ==> {'set':'performance','success':True}
    resp = client.post('/api/shell/power/set', json={'profile': 'performance'})
    assert resp.status_code == 200
    assert resp.get_json() == {'set': 'performance', 'success': True}
    assert ['powerprofilesctl', 'set', 'performance'] in fake_os.calls


def test_power_profile_invalid_blocked_before_boundary(client, fake_os):
    # ENTRY  POST /api/shell/power/set {'profile': 'ludicrous'}
    #   -> shell_power_set: 'ludicrous' fails the three-value allowlist
    #      [shell_os_apis.py:959-960] ==> 400 {'error': 'Invalid profile'}
    # BRANCH boundary log stays EMPTY -- powerprofilesctl is never spawned for
    #        a profile the OS does not define.
    resp = client.post('/api/shell/power/set', json={'profile': 'ludicrous'})
    assert resp.status_code == 400
    assert fake_os.calls == []


def test_power_profile_tool_failure_reports_honest_success_false(client, fake_os):
    # ENTRY  POST /api/shell/power/set {'profile': 'powersave'}
    #   -> shell_power_set -> powerprofilesctl exits 1 (forced via rc_for)
    # DATA   rc 1 ==> 200 {'set':'powersave','success':False}  [shell_os_apis.py:965-968]
    # BRANCH the handler reports the tool's REAL verdict in the success flag
    #        rather than erroring -- the profile stayed unchanged and the
    #        response says so; degrade-not-die, nothing gulped.
    fake_os.rc_for['powerprofilesctl'] = 1
    resp = client.post('/api/shell/power/set', json={'profile': 'powersave'})
    assert resp.status_code == 200
    assert resp.get_json() == {'set': 'powersave', 'success': False}
    assert ['powerprofilesctl', 'set', 'powersave'] in fake_os.calls


# ═════════════════════════════════════════════════════════════════════════════
# ACT 3 -- THE SESSION SURFACE (/api/shell/session/<action>)
# The power menu's own route: SAME logind funnel, DIFFERENT verb vocabulary.
# ═════════════════════════════════════════════════════════════════════════════

def test_session_restart_verb_translates_to_login1_reboot(client, fake_os):
    # ENTRY  POST /api/shell/session/restart (verb travels in the URL, no body)
    #   -> liquid_ui_service.shell_session('restart')  [liquid_ui_service.py:6820]
    #      -> allowlist ('lock','logout','suspend','shutdown','restart',
    #         'firmware')  [liquid_ui_service.py:6832-6834]
    #      -> DRY hop: 'restart' is THIS surface's public word for a reboot; it
    #         is translated via the ONE canonical map _POWER_METHOD['reboot']
    #         == 'Reboot'  [liquid_ui_service.py:6870-6872]
    #      -> _logind_call('Reboot','b','true') -> busctl  [BOUNDARY]
    # DATA   'restart' ==> argv [...,'Reboot','b','true']; rc 0 ==>
    #        {'status': 'restart'}
    resp = client.post('/api/shell/session/restart')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'restart'}
    assert _login1('Reboot', 'b', 'true') in fake_os.calls


def test_session_lock_matches_power_lock_path(client, fake_os):
    # ENTRY  POST /api/shell/session/lock
    #   -> shell_session('lock') -> _logind_call('LockSessions')
    #      [liquid_ui_service.py:6848-6849]
    # DATA   same no-arg LockSessions argv as /api/shell/power/action 'lock' --
    #        two surfaces, ONE logind implementation (no parallel busctl path).
    resp = client.post('/api/shell/session/lock')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'lock'}
    assert _login1('LockSessions') in fake_os.calls


def test_session_rejects_the_other_surfaces_vocabulary(client, fake_os):
    # ENTRY  POST /api/shell/session/reboot
    #   -> shell_session('reboot'): the session allowlist says 'restart', NOT
    #      'reboot' (while /api/shell/power/action says 'reboot' and not
    #      'restart') -- two verb vocabularies over one method map. 'reboot'
    #      here ==> 400 {'error': 'Invalid action'}  [liquid_ui_service.py:6832-6834]
    # BRANCH boundary log stays EMPTY: the URL verb is rejected before any
    #        logind call is composed. (Semantic note for the reader: the
    #        vocabulary split is intentional-but-subtle; this test pins it so a
    #        future 'harmonization' shows up as a diff here.)
    resp = client.post('/api/shell/session/reboot')
    assert resp.status_code == 400
    assert resp.get_json() == {'error': 'Invalid action'}
    assert fake_os.calls == []


def test_session_logout_without_session_id_refuses_honestly(client, fake_os, monkeypatch):
    # ENTRY  POST /api/shell/session/logout
    #   -> shell_session('logout')  [liquid_ui_service.py:6850-6859]
    #      -> logout terminates THIS seat session via login1 TerminateSession,
    #         which needs the session id from XDG_SESSION_ID; with it unset
    #         (guaranteed here via monkeypatch; it never exists on this dev
    #         box) the handler REFUSES rather than mask a no-op as success
    # DATA   no XDG_SESSION_ID ==> deliberate 500 {'action':'logout','error':
    #        'No active session id to terminate (XDG_SESSION_ID unset)'}
    # BRANCH boundary log stays EMPTY -- TerminateSession was never issued for
    #        a session we could not name. Honest refusal, not silence.
    monkeypatch.delenv('XDG_SESSION_ID', raising=False)
    resp = client.post('/api/shell/session/logout')
    assert resp.status_code == 500
    assert 'No active session id' in resp.get_json()['error']
    assert fake_os.calls == []


def test_session_firmware_capability_probe_is_pure_read(client, fake_os):
    # ENTRY  GET /api/shell/session/firmware-capable
    #   -> liquid_ui_service.shell_session_firmware_capable()  [liquid_ui_service.py:6880]
    #      -> firmware_setup_supported()  [shell_os_apis.py:213] -- the SAME
    #         single-source probe the power routes gate on: isdir
    #         ('/sys/firmware/efi') then an efivar file read. No subprocess.
    # DATA   this box has no /sys/firmware/efi ==> {'supported': False}, so the
    #        power menu HIDES the firmware button instead of offering a verb
    #        that would degrade into a plain reboot.
    # BRANCH boundary log stays EMPTY: capability is answered from sysfs alone.
    resp = client.get('/api/shell/session/firmware-capable')
    assert resp.status_code == 200
    assert resp.get_json() == {'supported': False}
    assert fake_os.calls == []


# ═════════════════════════════════════════════════════════════════════════════
# ACT 4 -- DISPLAY (xrandr read + write, swaymsg-first rotation)
# ═════════════════════════════════════════════════════════════════════════════

def test_display_get_parses_xrandr_modes(client, fake_os):
    # ENTRY  GET /api/shell/display
    #   -> liquid_ui_service.shell_display()  [liquid_ui_service.py:7294]
    #      -> subprocess.run(['xrandr','--current'], timeout=5)  [BOUNDARY]
    #      -> line parser (a LOOP over stdout):
    #         * ' connected' opens a display block (note: ' connected' with the
    #           leading space does NOT match 'disconnected' -- the classic
    #           xrandr parsing trap, pinned by the HDMI-1 line below);
    #         * first token in parts[2:] that starts with a digit and contains
    #           'x' is the live resolution, '+offset' stripped;
    #         * '   '-indented lines are mode rows; '*' marks the active rate;
    #         * a non-indented line CLOSES the block.
    # DATA   canned xrandr text ==> displays[0] eDP-1 @ 1920x1080 with two
    #        modes, rates parsed as floats, active flag on the '*' row;
    #        disconnected HDMI-1 contributes NOTHING.
    fake_os.stdout_for['xrandr'] = (
        'Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 16384 x 16384\n'
        'eDP-1 connected primary 1920x1080+0+0 (normal left inverted right) 344mm x 194mm\n'
        '   1920x1080     60.00*+  48.00\n'
        '   1280x720      60.00\n'
        'HDMI-1 disconnected (normal left inverted right)\n')
    resp = client.get('/api/shell/display')
    assert resp.status_code == 200
    assert resp.get_json() == {'displays': [{
        'name': 'eDP-1',
        'resolution': '1920x1080',
        'modes': [
            {'resolution': '1920x1080', 'rates': [60.0, 48.0], 'active': True},
            {'resolution': '1280x720', 'rates': [60.0], 'active': False},
        ],
    }]}
    assert ['xrandr', '--current'] in fake_os.calls


def test_display_brightness_maps_to_xrandr(client, fake_os):
    # ENTRY  POST /api/shell/display/brightness {'output':'eDP-1','brightness':0.5}
    #   -> liquid_ui_service.shell_display_brightness()  [liquid_ui_service.py:7362]
    #      -> validates output + brightness present  [liquid_ui_service.py:7367-7368]
    #      -> clamps to [0.1, 1.0]  [liquid_ui_service.py:7369]
    #      -> subprocess.run(['xrandr','--output','eDP-1','--brightness','0.5'],
    #         timeout=5)  [BOUNDARY]
    # DATA   0.5 survives the clamp ==> argv carries '0.5'; rc 0 ==>
    #        {'success': True, 'brightness': 0.5}
    resp = client.post('/api/shell/display/brightness',
                       json={'output': 'eDP-1', 'brightness': 0.5})
    assert resp.status_code == 200
    assert resp.get_json() == {'success': True, 'brightness': 0.5}
    assert ['xrandr', '--output', 'eDP-1', '--brightness', '0.5'] in fake_os.calls


def test_display_brightness_clamps_to_sane_range(client, fake_os):
    # ENTRY  POST /api/shell/display/brightness {'output':'eDP-1','brightness':5}
    #   -> shell_display_brightness: max(0.1, min(1.0, 5.0)) == 1.0
    #      [liquid_ui_service.py:7369]
    # DATA   caller's 5 ==> boundary sees '1.0', response echoes the CLAMPED
    #        value (1.0), never the raw request -- the OS can't be asked to
    #        overdrive the panel, and the floor 0.1 means brightness can never
    #        be set fully black (an unrecoverable-screen guard).
    resp = client.post('/api/shell/display/brightness',
                       json={'output': 'eDP-1', 'brightness': 5})
    assert resp.status_code == 200
    assert resp.get_json() == {'success': True, 'brightness': 1.0}
    assert ['xrandr', '--output', 'eDP-1', '--brightness', '1.0'] in fake_os.calls


def test_display_brightness_missing_output_blocked(client, fake_os):
    # ENTRY  POST /api/shell/display/brightness {'brightness': 0.5} (no output)
    #   -> shell_display_brightness: output falsy ==> 400 'output and
    #      brightness required'  [liquid_ui_service.py:7367-7368]
    # BRANCH boundary log stays EMPTY -- xrandr is never spawned without a
    #        target output to aim at.
    resp = client.post('/api/shell/display/brightness', json={'brightness': 0.5})
    assert resp.status_code == 400
    assert fake_os.calls == []


def test_display_brightness_tool_failure_degrades_controlled(client, fake_os):
    # ENTRY  POST /api/shell/display/brightness (valid body)
    #   -> xrandr exits 1 (forced) ==> handler returns the tool's stderr as a
    #      CONTROLLED 400 {'success': False, 'error': ...}  [liquid_ui_service.py:7376]
    # BRANCH note the sink: tool failure maps to 400 (caller-visible, logged in
    #        the payload), not a raw 500 -- the degrade contract of this route.
    fake_os.rc_for['xrandr'] = 1
    resp = client.post('/api/shell/display/brightness',
                       json={'output': 'eDP-1', 'brightness': 0.5})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_display_brightness_non_numeric_is_controlled_400(client, fake_os):
    # ENTRY  POST /api/shell/display/brightness {'output':'eDP-1','brightness':'max'}
    #   -> shell_display_brightness: float('max') is coerced INSIDE the guard
    #      (FIXED 2026-07-17: it used to sit outside the try and raised a raw
    #      500; the suite caught it, the coercion now mirrors the sink-volume
    #      twin's controlled 400)
    # DATA   brightness 'max' ==> controlled 400 'brightness must be a number';
    #        the boundary stays untouched (no argv is ever built).
    resp = client.post('/api/shell/display/brightness',
                       json={'output': 'eDP-1', 'brightness': 'max'})
    assert resp.status_code == 400
    assert fake_os.calls == []


def test_display_resolution_maps_to_xrandr_mode(client, fake_os):
    # ENTRY  POST /api/shell/display/resolution
    #        {'output':'HDMI-1','resolution':'1920x1080','rate':60}
    #   -> liquid_ui_service.shell_display_resolution()  [liquid_ui_service.py:7343]
    #      -> validates output + resolution present  [liquid_ui_service.py:7349-7350]
    #      -> builds ['xrandr','--output','HDMI-1','--mode','1920x1080'] and,
    #         because rate is truthy, appends ['--rate','60']  [liquid_ui_service.py:7352-7354]
    #      -> subprocess.run(cmd, timeout=10)  [BOUNDARY]
    # DATA   rc 0 ==> {'success': True, 'output': 'HDMI-1',
    #        'resolution': '1920x1080'} (rate accepted silently)
    resp = client.post('/api/shell/display/resolution',
                       json={'output': 'HDMI-1', 'resolution': '1920x1080',
                             'rate': 60})
    assert resp.status_code == 200
    assert resp.get_json() == {'success': True, 'output': 'HDMI-1',
                               'resolution': '1920x1080'}
    assert ['xrandr', '--output', 'HDMI-1', '--mode', '1920x1080',
            '--rate', '60'] in fake_os.calls


def test_display_resolution_missing_fields_blocked(client, fake_os):
    # ENTRY  POST /api/shell/display/resolution {'output': 'HDMI-1'}
    #   -> shell_display_resolution: resolution missing ==> 400
    #      [liquid_ui_service.py:7349-7350]
    # BRANCH boundary log stays EMPTY.
    resp = client.post('/api/shell/display/resolution', json={'output': 'HDMI-1'})
    assert resp.status_code == 400
    assert fake_os.calls == []


def test_display_rotation_wayland_first_then_x11_fallback(client, fake_os):
    # ENTRY  POST /api/shell/display/rotation {'output':'eDP-1','transform':'90'}
    #   -> shell_system_apis.shell_display_set_rotation()  [shell_system_apis.py:2284]
    #      -> validates transform against {'normal','90','180','270','flipped',
    #         'flipped-90','flipped-180','flipped-270'}  [shell_system_apis.py:2292-2295]
    #      -> TRY 1 (Wayland): _run(['swaymsg','output','eDP-1','transform',
    #         '90'])  [BOUNDARY] -- forced rc 1 here, so the compositor path
    #         reports failure
    #      -> TRY 2 (X11 fallback LOOP leg): translate via xrandr_map
    #         {'90': 'left'} and _run(['xrandr','--output','eDP-1','--rotate',
    #         'left'])  [BOUNDARY] -- rc 0
    # DATA   transform '90' ==> swaymsg keeps the literal '90', xrandr speaks
    #        'left'; fallback success ==> 200 {'rotated': True, ...}
    # BRANCH the two-transport cascade is the whole point: one verb, two
    #        display servers, tried in order until one answers.
    fake_os.rc_for['swaymsg'] = 1
    resp = client.post('/api/shell/display/rotation',
                       json={'output': 'eDP-1', 'transform': '90'})
    assert resp.status_code == 200
    assert resp.get_json() == {'rotated': True, 'output': 'eDP-1',
                               'transform': '90'}
    assert ['swaymsg', 'output', 'eDP-1', 'transform', '90'] in fake_os.calls
    assert ['xrandr', '--output', 'eDP-1', '--rotate', 'left'] in fake_os.calls


def test_display_rotation_invalid_transform_blocked(client, fake_os):
    # ENTRY  POST /api/shell/display/rotation {'output':'eDP-1','transform':'diagonal'}
    #   -> shell_display_set_rotation: 'diagonal' fails the transform set
    #      [shell_system_apis.py:2292-2295] ==> 400
    # BRANCH boundary log stays EMPTY -- neither swaymsg nor xrandr is spawned
    #        for a geometry the OS cannot express.
    resp = client.post('/api/shell/display/rotation',
                       json={'output': 'eDP-1', 'transform': 'diagonal'})
    assert resp.status_code == 400
    assert fake_os.calls == []


# ═════════════════════════════════════════════════════════════════════════════
# ACT 5 -- AUDIO (per-sink pactl routes + the wpctl-first default-sink routes)
# ═════════════════════════════════════════════════════════════════════════════

def test_audio_sink_volume_maps_to_pactl(client, fake_os):
    # ENTRY  POST /api/shell/audio/volume
    #        {'sink_id': 'alsa_output.pci-0000_00_1f.3.analog-stereo', 'volume': 42}
    #   -> liquid_ui_service.shell_audio_volume()  [liquid_ui_service.py:7100]
    #      -> validates sink_id + volume present  [liquid_ui_service.py:7105-7106]
    #      -> int-coerces and clamps to [0, 150] INSIDE a try  [liquid_ui_service.py:7107-7110]
    #      -> subprocess.run(['pactl','set-sink-volume',<sink>,'42%'],
    #         timeout=5)  [BOUNDARY]
    # DATA   volume 42 ==> argv suffix '42%'; rc 0 ==> {'success': True,
    #        'volume': 42}
    sink = 'alsa_output.pci-0000_00_1f.3.analog-stereo'
    resp = client.post('/api/shell/audio/volume',
                       json={'sink_id': sink, 'volume': 42})
    assert resp.status_code == 200
    assert resp.get_json() == {'success': True, 'volume': 42}
    assert ['pactl', 'set-sink-volume', sink, '42%'] in fake_os.calls


def test_audio_sink_volume_clamps_at_150(client, fake_os):
    # ENTRY  POST /api/shell/audio/volume {'sink_id': 'sink0', 'volume': 999}
    #   -> shell_audio_volume: max(0, min(150, 999)) == 150  [liquid_ui_service.py:7108]
    # DATA   999 ==> boundary sees '150%' (PipeWire's overdrive ceiling), and
    #        the response reports the CLAMPED 150 -- the caller learns what was
    #        actually applied, not what they asked for.
    resp = client.post('/api/shell/audio/volume',
                       json={'sink_id': 'sink0', 'volume': 999})
    assert resp.status_code == 200
    assert resp.get_json() == {'success': True, 'volume': 150}
    assert ['pactl', 'set-sink-volume', 'sink0', '150%'] in fake_os.calls


def test_audio_sink_volume_missing_sink_blocked(client, fake_os):
    # ENTRY  POST /api/shell/audio/volume {'volume': 42} (no sink_id)
    #   -> shell_audio_volume ==> 400 'sink_id and volume required'
    #      [liquid_ui_service.py:7105-7106]
    # BRANCH boundary log stays EMPTY -- pactl is never aimed at a nameless sink.
    resp = client.post('/api/shell/audio/volume', json={'volume': 42})
    assert resp.status_code == 400
    assert fake_os.calls == []


def test_audio_sink_volume_tool_failure_degrades_controlled(client, fake_os):
    # ENTRY  POST /api/shell/audio/volume (valid body), pactl exits 1 (forced)
    #   -> shell_audio_volume: rc != 0 ==> CONTROLLED 400 {'success': False,
    #      'error': <stderr>}  [liquid_ui_service.py:7117]
    fake_os.rc_for['pactl'] = 1
    resp = client.post('/api/shell/audio/volume',
                       json={'sink_id': 'sink0', 'volume': 42})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_audio_source_volume_non_integer_is_controlled_400(client, fake_os):
    # ENTRY  POST /api/shell/audio/source/volume {'source_id':'mic0','volume':'eleven'}
    #   -> liquid_ui_service.shell_audio_source_volume()
    #      -> int('eleven') is coerced INSIDE the guard (FIXED 2026-07-17: it
    #         used to sit outside any try and raised a raw 500; now the
    #         controlled 400 matches the SINK-volume twin exactly)
    # DATA   volume 'eleven' ==> 400 'volume must be a number'; boundary untouched.
    resp = client.post('/api/shell/audio/source/volume',
                       json={'source_id': 'mic0', 'volume': 'eleven'})
    assert resp.status_code == 400
    assert fake_os.calls == []


def test_audio_mute_maps_to_pactl(client, fake_os):
    # ENTRY  POST /api/shell/audio/mute {'sink_id': 'sink0', 'muted': True}
    #   -> liquid_ui_service.shell_audio_mute()  [liquid_ui_service.py:7121]
    #      -> muted True ==> pactl's '1'  [liquid_ui_service.py:7129]
    #      -> subprocess.run(['pactl','set-sink-mute','sink0','1'], timeout=5)
    #         [BOUNDARY]
    # DATA   rc 0 ==> {'success': True, 'muted': True}
    resp = client.post('/api/shell/audio/mute',
                       json={'sink_id': 'sink0', 'muted': True})
    assert resp.status_code == 200
    assert resp.get_json() == {'success': True, 'muted': True}
    assert ['pactl', 'set-sink-mute', 'sink0', '1'] in fake_os.calls


def test_default_sink_volume_wpctl_first_pactl_fallback(client, fake_os):
    # ENTRY  POST /api/shell/volume {'volume': 30}
    #   -> liquid_ui_service.shell_volume_set()  [liquid_ui_service.py:7229]
    #      -> clamps inside a try  [liquid_ui_service.py:7235-7239]
    #      -> TRY 1: _vol_run(['wpctl','set-volume','@DEFAULT_AUDIO_SINK@',
    #         '0.3'])  [BOUNDARY] -- 30/100.0 == 0.3, wpctl speaks fractions;
    #         forced rc 1 here
    #      -> TRY 2 (fallback leg): _vol_run(['pactl','set-sink-volume',
    #         '@DEFAULT_SINK@','30%'])  [BOUNDARY] -- pactl speaks percent; rc 0
    # DATA   ONE user volume (30) is spoken in TWO tool dialects (0.3 vs 30%);
    #        the response names which tool actually served it:
    #        {'available': True, 'volume': 30, 'tool': 'pactl'}
    fake_os.rc_for['wpctl'] = 1
    resp = client.post('/api/shell/volume', json={'volume': 30})
    assert resp.status_code == 200
    assert resp.get_json() == {'available': True, 'volume': 30, 'tool': 'pactl'}
    assert ['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', '0.3'] in fake_os.calls
    assert ['pactl', 'set-sink-volume', '@DEFAULT_SINK@', '30%'] in fake_os.calls


def test_default_sink_volume_no_tool_honest_unavailable(client, fake_os):
    # ENTRY  POST /api/shell/volume {'volume': 30}, BOTH wpctl and pactl exit 1
    #   -> shell_volume_set walks both legs; both fail
    # DATA   ==> 200 {'available': False, 'error': 'no volume tool
    #        (wpctl/pactl)'}  [liquid_ui_service.py:7250-7251]
    # BRANCH the live-USB reality: neither tool may exist. The handler answers
    #        200-with-available-False (an HONEST capability report the UI can
    #        grey out on), not an error the popover would toast at the user.
    fake_os.rc_for['wpctl'] = 1
    fake_os.rc_for['pactl'] = 1
    resp = client.post('/api/shell/volume', json={'volume': 30})
    assert resp.status_code == 200
    assert resp.get_json() == {'available': False,
                               'error': 'no volume tool (wpctl/pactl)'}


def test_default_sink_mute_toggle_and_state_readback(client, fake_os):
    # ENTRY  POST /api/shell/volume/mute {} (no 'muted' key)
    #   -> liquid_ui_service.shell_volume_mute()  [liquid_ui_service.py:7255]
    #      -> muted None ==> wpctl's 'toggle'  [liquid_ui_service.py:7259]
    #      -> _vol_run(['wpctl','set-mute','@DEFAULT_AUDIO_SINK@','toggle'])
    #         [BOUNDARY] rc 0
    #      -> response sink is a READBACK, not an echo: _volume_get()
    #         [liquid_ui_service.py:110] runs ['wpctl','get-volume',
    #         '@DEFAULT_AUDIO_SINK@']  [BOUNDARY] and parses
    #         'Volume: 0.55 [MUTED]' -> fraction 0.55 * 100 -> 55, 'MUTED'
    #         substring -> muted True
    # DATA   canned 'Volume: 0.55 [MUTED]' ==> {'available': True,
    #        'tool': 'wpctl', 'volume': 55, 'muted': True} -- the caller gets
    #        the POST-toggle truth from the mixer itself.
    fake_os.stdout_for['get-volume'] = 'Volume: 0.55 [MUTED]'
    resp = client.post('/api/shell/volume/mute', json={})
    assert resp.status_code == 200
    assert resp.get_json() == {'available': True, 'tool': 'wpctl',
                               'volume': 55, 'muted': True}
    assert ['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', 'toggle'] in fake_os.calls
    assert ['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'] in fake_os.calls


# ═════════════════════════════════════════════════════════════════════════════
# ACT 6 -- STORAGE / DISK (the destructive end of the chapter)
# ═════════════════════════════════════════════════════════════════════════════

def test_storage_trim_maps_to_fstrim(client, fake_os):
    # ENTRY  POST /api/shell/storage/trim {'mount': <cwd>}
    #   -> @_require_system_auth passes (127.0.0.1)  [shell_system_apis.py:413]
    #   -> shell_system_apis.shell_storage_trim()  [shell_system_apis.py:838]
    #      -> validates os.path.isdir(mount) (the suite cwd is the sandboxed
    #         scratch dir the conftest chdir'd into)  [shell_system_apis.py:842-843]
    #      -> _run(['fstrim','-v',<mount>], timeout=60)  [BOUNDARY]
    #      -> _audit_system_op('storage_trim') best-effort
    # DATA   rc 0 + canned stdout ==> {'ok': True, 'mount': <cwd>,
    #        'output': '<fstrim report>'} -- trim is the SAFE disk op, no
    #        confirm gate needed.
    mount = os.getcwd()
    fake_os.stdout_for['fstrim'] = '/: 12.5 GiB (13421772800 bytes) trimmed'
    resp = client.post('/api/shell/storage/trim', json={'mount': mount})
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'mount': mount,
                               'output': '/: 12.5 GiB (13421772800 bytes) trimmed'}
    assert ['fstrim', '-v', mount] in fake_os.calls


def test_storage_trim_failure_reports_ok_false(client, fake_os):
    # ENTRY  POST /api/shell/storage/trim (valid mount), fstrim exits 1 (forced)
    #   -> shell_storage_trim: r is not None ==> no 500; the verdict rides the
    #      payload: {'ok': False, ...}  [shell_system_apis.py:848-849]
    # BRANCH honest ok:False with the tool's output attached -- controlled
    #        degrade, HTTP 200 (the request itself worked; the trim did not).
    fake_os.rc_for['fstrim'] = 1
    resp = client.post('/api/shell/storage/trim', json={'mount': os.getcwd()})
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is False


def test_storage_format_requires_explicit_confirm(client, fake_os):
    # ENTRY  POST /api/shell/storage/format {'device':'/dev/sdz1','fstype':'ext4'}
    #        (NO confirm flag)
    #   -> shell_system_apis.shell_storage_format()  [shell_system_apis.py:853]
    #      -> gate 1: _valid_device regex ^/dev/[A-Za-z0-9/_-]+$ (pure, no I/O)
    #         [shell_system_apis.py:126-131] -- passes
    #      -> gate 2: fstype in the 7-FS _FS_FORMAT map -- passes
    #      -> gate 3: confirm is falsy ==> 400 {'error': 'format is
    #         destructive: pass confirm=true to proceed',
    #         'requires_confirm': True}  [shell_system_apis.py:866-868]
    # BRANCH boundary log stays EMPTY: the confirm gate sits BEFORE even the
    #        protected-device findmnt probes, so an unconfirmed format costs
    #        the OS nothing at all.
    resp = client.post('/api/shell/storage/format',
                       json={'device': '/dev/sdz1', 'fstype': 'ext4'})
    assert resp.status_code == 400
    assert resp.get_json()['requires_confirm'] is True
    assert fake_os.calls == []


def test_storage_format_refuses_the_system_disk(client, fake_os):
    # ENTRY  POST /api/shell/storage/format
    #        {'device':'/dev/sdz1','fstype':'ext4','confirm':True}
    #   -> shell_storage_format, confirm passed, so the protected-device sweep
    #      runs: _is_protected_device -> _protected_devices  [shell_system_apis.py:152-165]
    #      -> a LOOP over the 5 OS mountpoints ('/', '/boot', '/boot/efi',
    #         '/nix', '/nix/store'), each probed via
    #         _run(['findmnt','-nro','SOURCE',<mp>])  [BOUNDARY x5]
    # DATA   canned findmnt answers '/dev/sdz1' for every mountpoint ==> the
    #        protected set becomes {'/dev/sdz1', '/dev/sdz' (whole-disk
    #        parent)} ==> the requested device IS the running OS ==> 403
    #        'refusing to format a system disk (root/boot/nix)'
    # BRANCH no mkfs.* argv EVER appears in the boundary log -- the guard that
    #        makes it structurally impossible for a stray click to wipe the
    #        disk the OS is running from.
    fake_os.stdout_for['findmnt'] = '/dev/sdz1'
    resp = client.post('/api/shell/storage/format',
                       json={'device': '/dev/sdz1', 'fstype': 'ext4',
                             'confirm': True})
    assert resp.status_code == 403
    assert 'system disk' in resp.get_json()['error']
    probe_calls = [c for c in fake_os.calls if c[:3] == ['findmnt', '-nro', 'SOURCE']]
    assert len(probe_calls) == 5          # one per protected mountpoint
    assert not any(str(c[0]).startswith('mkfs') for c in fake_os.calls)


def test_storage_format_happy_path_builds_exact_mkfs_argv(client, fake_os, monkeypatch):
    # ENTRY  POST /api/shell/storage/format
    #        {'device':'/dev/sdz1','fstype':'ext4','label':'HIVE','confirm':True}
    #   -> shell_storage_format  [shell_system_apis.py:853]
    #      -> confirm ok; findmnt probes answer EMPTY (FakeOS default) ==> not
    #         protected, not mounted
    #      -> argv assembled from the ONE _FS_FORMAT map + label flag:
    #         ['mkfs.ext4','-F'] + ['-L','HIVE'] + ['/dev/sdz1']
    #         [shell_system_apis.py:873-876]
    #      -> shutil.which('mkfs.ext4') gates on tool presence -- patched here
    #         (this dev box has no mkfs; the subprocess itself is FakeOS'd
    #         either way)
    #      -> _run_async_bounded(cmd, run_timeout=120, wait=8)
    #         [shell_system_apis.py:33]: the format runs on a daemon worker;
    #         FakeOS settles instantly ==> finished True ==> the REAL verdict
    #         [BOUNDARY]
    # DATA   rc 0 ==> {'ok': True, 'device': '/dev/sdz1', 'fstype': 'ext4',
    #        'output': ''}
    # BRANCH (narrated, not forced) a SLOW format would return 202
    #        {'running': True} instead -- the bounded-worker discipline that
    #        keeps the 1-2 thread shell pool from pinning.
    monkeypatch.setattr('shutil.which', lambda tool: '/run/current-system/sw/bin/' + tool)
    resp = client.post('/api/shell/storage/format',
                       json={'device': '/dev/sdz1', 'fstype': 'ext4',
                             'label': 'HIVE', 'confirm': True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True and body['device'] == '/dev/sdz1'
    assert ['mkfs.ext4', '-F', '-L', 'HIVE', '/dev/sdz1'] in fake_os.calls


def test_storage_devices_parses_lsblk_tree(client, fake_os):
    # ENTRY  GET /api/shell/storage/devices
    #   -> shell_system_apis.shell_storage_devices()  [shell_system_apis.py:751]
    #      -> _lsblk_devices()  [shell_system_apis.py:265]
    #         -> _run(['lsblk','-J','-b','-o','NAME,PATH,TYPE,SIZE,ROTA,MODEL,
    #            MOUNTPOINT,FSTYPE'], timeout=8)  [BOUNDARY]
    #         -> json.loads(stdout), then a RECURSIVE walk (_walk) flattens the
    #            disk->partition tree depth-first: parent row first, children
    #            immediately after
    # DATA   canned one-disk-one-partition JSON ==> two flat entries; the
    #        child's null model becomes '' ((model or '').strip()), the
    #        parent's is preserved.
    fake_os.stdout_for['lsblk'] = (
        '{"blockdevices": [{"name": "sda", "path": "/dev/sda", "type": "disk",'
        ' "size": 500107862016, "rota": false, "model": "Samsung SSD",'
        ' "mountpoint": null, "fstype": null, "children": ['
        '{"name": "sda1", "path": "/dev/sda1", "type": "part",'
        ' "size": 536870912, "rota": false, "model": null,'
        ' "mountpoint": "/boot", "fstype": "vfat"}]}]}')
    resp = client.get('/api/shell/storage/devices')
    assert resp.status_code == 200
    assert resp.get_json() == {'devices': [
        {'name': 'sda', 'path': '/dev/sda', 'type': 'disk',
         'size': 500107862016, 'rota': False, 'model': 'Samsung SSD',
         'mountpoint': None, 'fstype': None},
        {'name': 'sda1', 'path': '/dev/sda1', 'type': 'part',
         'size': 536870912, 'rota': False, 'model': '',
         'mountpoint': '/boot', 'fstype': 'vfat'},
    ]}
