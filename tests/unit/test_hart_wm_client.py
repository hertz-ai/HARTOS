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
