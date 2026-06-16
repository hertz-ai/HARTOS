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
