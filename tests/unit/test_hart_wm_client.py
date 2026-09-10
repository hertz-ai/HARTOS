"""Phase 6 (the moat, brain-side): HartWmClient arranges REAL windows via the
swaymsg shim, and every DESTRUCTIVE verb is fail-CLOSED behind the constitution
+ audited — an agent closing a window is governed like a goal dispatch.

Behavioural: mock ONLY the boundaries (the swaymsg subprocess + the security
gate); call the real client; assert the commands issued + the refuse/allow.

    python -m pytest tests/unit/test_hart_wm_client.py --noconftest -p no:capture -q
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import integrations.agent_engine.hart_wm_client as wm
from integrations.agent_engine.hart_wm_client import HartWmClient


def _proc(rc=0, out=''):
    return SimpleNamespace(returncode=rc, stdout=out, stderr='')


def _sway_client():
    c = HartWmClient()
    c._backend = 'sway'   # force the shim regardless of the host
    return c


def test_list_windows_parses_real_sway_tree():
    c = _sway_client()
    tree = {'type': 'root', 'nodes': [
        {'type': 'con', 'app_id': 'firefox', 'name': 'Mozilla', 'id': 7,
         'focused': True, 'rect': {'x': 0}, 'nodes': [], 'floating_nodes': []}],
        'floating_nodes': []}
    with patch.object(wm, '_run', return_value=_proc(0, json.dumps(tree))):
        wins = c.list_windows()
    assert any(w['app_id'] == 'firefox' and w['id'] == 7 and w['focused']
               for w in wins)


def test_place_window_issues_move_and_resize():
    c = _sway_client()
    with patch.object(wm, '_run', return_value=_proc(0)) as run:
        r = c.place_window(7, 10, 20, 800, 600)
    assert r['ok'] is True
    cmd = run.call_args.args[0]          # ['swaymsg', '<command string>']
    assert cmd[0] == 'swaymsg'
    assert '[con_id=7]' in cmd[1] and 'move position 10 20' in cmd[1] \
        and 'resize set 800 600' in cmd[1]


def test_close_window_refused_when_hive_halted():
    c = _sway_client()
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=True), \
         patch.object(wm, '_run', return_value=_proc(0)) as run:
        r = c.close_window(7, 'agent-1')
    assert r['ok'] is False
    run.assert_not_called()              # never reached swaymsg kill


def test_close_window_fail_closed_when_guardrail_unavailable():
    c = _sway_client()
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False), \
         patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               side_effect=RuntimeError('guardrails down')), \
         patch.object(wm, '_run', return_value=_proc(0)) as run:
        r = c.close_window(7, 'agent-1')
    assert r['ok'] is False              # destructive op blocked, not proceeded
    run.assert_not_called()


def test_close_window_allowed_and_audited_when_clear():
    c = _sway_client()
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False), \
         patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               return_value=(True, '', '')), \
         patch('security.immutable_audit_log.get_audit_log') as audit, \
         patch.object(wm, '_run', return_value=_proc(0)) as run:
        r = c.close_window(7, 'agent-1')
    assert r['ok'] is True
    assert run.called                    # swaymsg kill issued
    audit.return_value.log_event.assert_called()   # the close is provable


def test_no_compositor_returns_empty_not_crash():
    c = HartWmClient()
    c._backend = None
    assert c.list_windows() == []


def test_dispatch_place_routes_to_place_window():
    c = _sway_client()
    with patch.object(wm, '_run', return_value=_proc(0)) as run:
        r = c.dispatch_verb('window.place',
                            {'con_id': 7, 'x': 10, 'y': 20, 'w': 800, 'h': 600},
                            'agent-1')
    assert r['ok'] is True
    assert 'move position 10 20' in run.call_args.args[0][1]


def test_dispatch_close_is_fail_closed_gated():
    c = _sway_client()
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=True), \
         patch.object(wm, '_run', return_value=_proc(0)) as run:
        r = c.dispatch_verb('window.close', {'con_id': 7}, 'agent-1')
    assert r['ok'] is False
    run.assert_not_called()


def test_dispatch_unknown_verb_and_bad_args():
    c = _sway_client()
    assert c.dispatch_verb('window.frobnicate', {}, 'a')['ok'] is False
    assert c.dispatch_verb('window.place', {'con_id': 'NaN'}, 'a')['ok'] is False


# ── Phase 5: the additive native-window summon path (no phantom handle) ──

def test_summon_stays_honest_unsupported_when_no_native_window_bound():
    # Tier-2 shim cannot await a map; with NO native window already known, summon
    # must report unsupported and NEVER fabricate a handle (no-phantom-windows).
    c = _sway_client()
    with patch.object(HartWmClient, '_native_window_handle', return_value=None):
        r = c.summon_app('blender')
    assert r['ok'] is False and r['error'] == 'unsupported'
    assert 'handle' not in r            # no phantom handle


def test_summon_reuses_an_existing_real_native_window_handle():
    # The additive path: if HART-comp already mapped this manifest (a REAL map,
    # recorded in AppRegistry), summon hands back THAT handle — not a phantom.
    c = _sway_client()
    with patch.object(HartWmClient, '_native_window_handle',
                      return_value='win_9c04'):
        r = c.summon_app('blender')
    assert r['ok'] is True
    assert r['handle'] == 'win_9c04' and r['mapped'] is True and r['reused'] is True


def test_summon_empty_manifest_id_rejected():
    c = _sway_client()
    assert c.summon_app('')['ok'] is False


def test_summon_native_handle_lookup_is_safe_without_registry():
    # On a headless node AppRegistry may be unregistered; the lookup must not
    # crash — it returns None and summon falls to the honest unsupported.
    c = _sway_client()
    # _native_window_handle imports get_registry lazily; force the import to fail.
    with patch('core.platform.registry.get_registry',
               side_effect=RuntimeError('no registry')):
        assert c._native_window_handle('blender') is None


# ─── Tier-1: the HART-comp framed-JSON transport ────────────────────────────
#
# THE DEFECT these cover, measured on the Samsung box 2026-09-07 under the
# native tier. compositor/src/ipc.rs serves the whole verb surface against the
# real Space<Window> (proved live: window.list answered ok=true, an unknown
# method answered code=unsupported), but the brain had no client for it and
# only ever shelled swaymsg. Worse, _is_wayland() returns True on SWAYSOCK
# alone and hart-liquid-ui.nix always sets it, so the client reported
# available=True on a tier where the relay has no upstream sway and EVERY verb
# failed. A banked layout replayed as "available: true, replayed 0 of 3" with
# three anonymous ok=false values and no reason anywhere.

import os
import socket
import struct
import threading
import time as _time

import pytest

HAS_AF_UNIX = hasattr(socket, 'AF_UNIX')
needs_af_unix = pytest.mark.skipif(
    not HAS_AF_UNIX,
    reason='AF_UNIX is Linux-side; the node has it, this dev host may not')


class _FakeCompositor:
    """A stand-in speaking ipc.rs's wire: 4-byte BE length + JSON, both ways.

    Real socket, real framing, real accept loop. The only thing faked is the
    window tree, which is the right boundary: the framing is exactly what must
    NOT be mocked, because a short read or a wrong byte order is the bug class
    this transport can actually have.
    """

    def __init__(self, tmpdir, responder):
        self.path = os.path.join(str(tmpdir), 'hart-comp.sock')
        self.responder = responder
        self.requests = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(8)
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _read_request(self, conn):
        head = conn.recv(4)
        if len(head) != 4:
            return None
        (n,) = struct.unpack('>I', head)
        body = b''
        while len(body) < n:
            c = conn.recv(n - len(body))
            if not c:
                return None
            body += c
        req = json.loads(body.decode())
        self.requests.append(req)
        return req

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            try:
                req = self._read_request(conn)
                if req is not None:
                    reply = self.responder(req)
                    if reply is not None:
                        out = json.dumps(reply).encode()
                        conn.sendall(struct.pack('>I', len(out)) + out)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass


def _ok_reply(result):
    return lambda req: {'v': 1, 'id': req.get('id'), 'ok': True,
                        'result': result, 'error': None}


@needs_af_unix
def test_a_live_hart_comp_socket_is_detected_over_the_sway_shim(tmp_path, monkeypatch):
    """The honesty fix. With a real compositor socket answering, the backend is
    hart-comp even though SWAYSOCK is set, because the unit sets SWAYSOCK on
    the native tier too, where it means nothing."""
    srv = _FakeCompositor(tmp_path, _ok_reply({'windows': []}))
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        monkeypatch.setenv('SWAYSOCK', '/run/hart/sway-ipc.sock')
        c = HartWmClient()
        assert c._backend == 'hart-comp'
        assert c.available is True
    finally:
        srv.close()


@needs_af_unix
def test_a_dead_socket_file_is_not_a_transport(tmp_path, monkeypatch):
    """A socket FILE outlives the process that bound it. Detection connects
    rather than stat-ing, so a stale path never becomes a claimed transport."""
    # Bind and close WITHOUT ever listening: the inode is left behind exactly
    # as a crashed compositor leaves it, and connect(2) gets ECONNREFUSED.
    # (Closing a _FakeCompositor is not equivalent -- its accept() thread keeps
    # the listener alive on the fd, so the "stale" socket still answers.)
    stale = os.path.join(str(tmp_path), 'stale-hart-comp.sock')
    _dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _dead.bind(stale)
    _dead.close()
    assert os.path.exists(stale), 'the stale socket FILE must still be there'
    monkeypatch.setenv('HART_COMP_SOCK', stale)
    # XDG_RUNTIME_DIR is the SECOND candidate, and on a real node it points at a
    # live compositor socket. Leaving it set made this test pass for the wrong
    # reason on the dev host (no AF_UNIX, so it skipped) and fail on the node,
    # where candidate 2 connected for real. Point it somewhere empty so the
    # stale path is genuinely the only candidate.
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path / 'no-such-runtime'))
    monkeypatch.delenv('SWAYSOCK', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.delenv('XDG_SESSION_TYPE', raising=False)
    with patch('integrations.agent_engine.shell_desktop_apis._is_wayland',
               return_value=False):
        c = HartWmClient()
    assert c._backend is None
    assert c.available is False


@needs_af_unix
def test_window_list_normalises_hart_comp_records(tmp_path, monkeypatch):
    """One output shape whichever compositor answered: hart-comp's
    handle/title/geometry map onto the id/name/rect callers already read, and
    its extra truth rides along rather than being dropped."""
    win = {'handle': 'win_1a2b', 'app_id': 'org.hart.Files', 'title': 'Files',
           'geometry': {'x': 10, 'y': 20, 'w': 800, 'h': 600},
           'focused': True, 'kind': 'xdg', 'mapped': True,
           'workspace': 2, 'visible': True}
    srv = _FakeCompositor(tmp_path, _ok_reply({'windows': [win]}))
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        c = HartWmClient()
        rows = c.list_windows()
    finally:
        srv.close()
    assert len(rows) == 1
    r = rows[0]
    assert r['id'] == 'win_1a2b'          # the handle IS the id on this tier
    assert r['name'] == 'Files'           # title -> name
    assert r['rect'] == win['geometry']   # geometry -> rect
    assert r['focused'] is True
    assert r['workspace'] == 2 and r['visible'] is True


@needs_af_unix
def test_verbs_reach_the_compositor_with_the_wire_names_it_expects(tmp_path, monkeypatch):
    """The arg translation is the point: the brain speaks con_id, ipc.rs speaks
    handle, and a workspace number is 1-based on the wire for both tiers."""
    srv = _FakeCompositor(tmp_path, _ok_reply({'switched': True}))
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        c = HartWmClient()
        assert c.switch_workspace(3)['ok'] is True
        assert c.focus_window(7)['ok'] is True
        assert c.place_window(7, 1, 2, 3, 4)['ok'] is True
        assert c.move_to_workspace(7, 2)['ok'] is True
        assert c.tile_layout('grid')['ok'] is True
    finally:
        srv.close()
    sent = {r['method']: r['args'] for r in srv.requests}
    assert sent['workspace.switch'] == {'workspace': 3}
    assert sent['window.focus'] == {'handle': '7'}
    assert sent['window.place'] == {'handle': '7',
                                    'target': {'x': 1, 'y': 2, 'w': 3, 'h': 4}}
    assert sent['window.move_to_workspace'] == {'handle': '7', 'workspace': 2}
    # "grid" is HART-comp's own arrangement. The sway container-layout allowlist
    # must not be applied to a compositor that actually implements it.
    assert sent['window.tile'] == {'layout': 'grid'}


@needs_af_unix
def test_a_compositor_error_keeps_its_code_and_message(tmp_path, monkeypatch):
    """Honest failure, carried through. not_found from ipc.rs must not be
    flattened into the bare False the swaymsg path used to return."""
    def responder(req):
        return {'v': 1, 'id': req.get('id'), 'ok': False, 'result': None,
                'error': {'code': 'not_found',
                          'message': 'no mapped window for handle 99'}}
    srv = _FakeCompositor(tmp_path, responder)
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        c = HartWmClient()
        r = c.focus_window(99)
    finally:
        srv.close()
    assert r['ok'] is False
    assert r['error'] == 'not_found'
    assert 'no mapped window' in r['message']


@needs_af_unix
def test_close_is_still_refused_by_the_constitution_on_the_native_tier(tmp_path, monkeypatch):
    """The gate runs BEFORE a transport is chosen, so the new tier cannot become
    a way around it: a refused close never reaches the compositor at all."""
    srv = _FakeCompositor(tmp_path, _ok_reply({'closed': True}))
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        c = HartWmClient()
        with patch.object(HartWmClient, '_guard_destructive', return_value=False):
            r = c.close_window(5, 'agent_x')
    finally:
        srv.close()
    assert r == {'ok': False, 'error': 'refused-by-constitution'}
    assert not [q for q in srv.requests if q['method'] == 'window.close']


@needs_af_unix
def test_an_allowed_close_does_reach_the_compositor(tmp_path, monkeypatch):
    """The other half of the gate: allowed means it really is dispatched, with
    the handle ipc.rs expects."""
    srv = _FakeCompositor(tmp_path, _ok_reply({'closed': True}))
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        c = HartWmClient()
        with patch.object(HartWmClient, '_guard_destructive', return_value=True):
            r = c.close_window(5, 'agent_x')
    finally:
        srv.close()
    assert r['ok'] is True and r['closed'] is True
    assert [q for q in srv.requests
            if q['method'] == 'window.close'][0]['args'] == {'handle': '5'}


@needs_af_unix
def test_a_reply_split_across_reads_still_reassembles(tmp_path, monkeypatch):
    """Framing, not mocked. A stream socket may hand back a frame in pieces;
    treating the first read as the whole frame is how framed protocols corrupt
    silently, so this reply is deliberately dribbled out 7 bytes at a time."""

    class _Dribbler(_FakeCompositor):
        def _serve(self):
            while not self._stop:
                try:
                    conn, _ = self._srv.accept()
                except OSError:
                    return
                try:
                    if self._read_request(conn) is not None:
                        out = json.dumps({'v': 1, 'id': 'brain', 'ok': True,
                                          'result': {'windows': []},
                                          'error': None}).encode()
                        framed = struct.pack('>I', len(out)) + out
                        for i in range(0, len(framed), 7):
                            conn.sendall(framed[i:i + 7])
                            _time.sleep(0.001)
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

    srv = _Dribbler(tmp_path, None)
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        c = HartWmClient()
        assert c.list_windows() == []
        assert c._hc('window.list')['ok'] is True
    finally:
        srv.close()


@needs_af_unix
def test_a_socket_that_accepts_but_never_answers_is_not_a_backend(tmp_path, monkeypatch):
    """THE TRAP THIS TRANSPORT MUST NOT FALL INTO.

    HART_COMP_SOCK points at a systemd socket-activated relay, and systemd
    ALWAYS accepts the connection, spawning a relay that then exits 1 when it
    cannot find an upstream compositor. So connect(2) succeeding proves
    nothing at all: it is the same worthless signal as SWAYSOCK being set,
    which is what made the client claim a working window manager on a tier
    that had none. Detection therefore requires a real answer, and a peer that
    accepts and hangs up must leave the backend unclaimed.
    """
    srv = _FakeCompositor(tmp_path, lambda req: None)   # accepts, never answers
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path / 'no-such-runtime'))
        with patch('integrations.agent_engine.shell_desktop_apis._is_wayland',
                   return_value=False):
            c = HartWmClient()
        assert c._backend is None, 'a silent peer must not count as a compositor'
        assert c.available is False
        # And the low-level call says WHY, flagged as a transport failure so a
        # refusal is never confused with a dead pipe.
        r = HartWmClient._call_on(srv.path, 'window.list')
        assert r['ok'] is False and r['_transport'] is True
        assert 'no response frame' in r['error'] or 'failed' in r['error']
    finally:
        srv.close()


@needs_af_unix
def test_a_refusal_is_not_retried_as_a_transport_failure(tmp_path, monkeypatch):
    """not_found is the compositor answering correctly. Retrying it would
    dispatch a destructive verb twice, so only _transport failures re-probe."""
    def responder(req):
        return {'v': 1, 'id': req.get('id'), 'ok': False, 'result': None,
                'error': {'code': 'not_found', 'message': 'no mapped window'}}
    srv = _FakeCompositor(tmp_path, responder)
    try:
        monkeypatch.setenv('HART_COMP_SOCK', srv.path)
        c = HartWmClient()
        c._backend = 'hart-comp'          # detection saw window.list refuse too
        c._hc_path = srv.path
        before = len(srv.requests)
        r = c.focus_window(4)
    finally:
        srv.close()
    assert r['ok'] is False and r['error'] == 'not_found'
    assert '_transport' not in r
    assert len(srv.requests) - before == 1, 'a refusal must be dispatched ONCE'


def test_a_failed_swaymsg_run_reports_why():
    """The Tier-2 half of the same honesty rule. A bare ok=False is what kept
    the native-tier breakage invisible."""
    c = _sway_client()
    with patch.object(wm, '_run', return_value=SimpleNamespace(
            returncode=1, stdout='', stderr='Unable to receive IPC response')):
        r = c.switch_workspace(2)
    assert r['ok'] is False
    assert 'Unable to receive IPC response' in r['error']


def test_no_transport_at_all_says_so():
    c = _sway_client()
    with patch.object(wm, '_run', return_value=None):
        r = c.switch_workspace(2)
    assert r['ok'] is False and 'no window-manager transport' in r['error']
