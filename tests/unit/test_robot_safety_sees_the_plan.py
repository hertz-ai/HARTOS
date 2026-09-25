"""Robot safety must judge the plan that will EXECUTE, and halt unless it says
safe (hevolveai Master 11.435 S3).

The safety intelligence was dispatched in parallel with an EMPTY plan, so the
speed, stairs and workspace checks never saw the motor trajectory the fused
plan then ran; a safety timeout or error read as safe through
.get('safe', True); an unavailable monitor read as safe. Unlike the older
think() tests, these run the REAL _invoke_safety and _fuse_results; only the
unrelated intelligences and the motor output are stubbed.
"""
import os
import sys
import types
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from integrations.robotics import intelligence_api as ia  # noqa: E402


class _Monitor:
    is_estopped = False

    def check_position_safe(self, position):
        return True


def _api(monkeypatch, trajectory):
    monkeypatch.setattr(ia, '_REGISTRY_PATH', os.devnull + '.missing')
    api = ia.RobotIntelligenceAPI()
    quiet = {'source': 'stub'}
    for name in ('_invoke_vision', '_invoke_language', '_invoke_spatial',
                 '_invoke_social', '_invoke_hivemind'):
        monkeypatch.setattr(api, name, lambda *a, **k: dict(quiet))
    monkeypatch.setattr(api, '_invoke_motor', lambda *a, **k: {
        'trajectory': trajectory, 'speed': 0.0, 'source': 'stub'})
    return api


def _step(speed):
    return {'action_type': 'navigate_to', 'target': 'base',
            'params': {'x': 1.0, 'y': 1.0, 'speed': speed}}


def _think(api, **constraints):
    with patch('integrations.robotics.safety_monitor.get_safety_monitor',
               return_value=_Monitor()):
        return api.think({'robot_id': 'r1', 'context': 'go',
                          'constraints': constraints})['action_plan']


def test_a_trajectory_over_the_speed_limit_halts(monkeypatch):
    plan = _think(_api(monkeypatch, [_step(5.0)]), max_speed=1.0)
    assert plan['primary_action'] == 'halt', plan
    assert any('exceeds max' in w for w in plan.get('safety_warnings', []))


def test_control_a_trajectory_within_limits_executes(monkeypatch):
    plan = _think(_api(monkeypatch, [_step(0.5)]), max_speed=1.0)
    assert plan['primary_action'] == 'execute_trajectory', plan
    assert plan['steps'] == [_step(0.5)]


def test_a_failing_safety_check_halts(monkeypatch):
    api = _api(monkeypatch, [_step(0.5)])
    monkeypatch.setattr(api, '_invoke_safety',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    assert _think(api, max_speed=1.0)['primary_action'] == 'halt'


def test_an_unavailable_monitor_halts(monkeypatch):
    api = _api(monkeypatch, [_step(0.5)])
    with patch('integrations.robotics.safety_monitor.get_safety_monitor',
               side_effect=ImportError('no monitor')):
        plan = api.think({'robot_id': 'r1', 'context': 'go',
                          'constraints': {'max_speed': 1.0}})['action_plan']
    assert plan['primary_action'] == 'halt'


def test_a_verdict_that_is_not_safe_true_halts(monkeypatch):
    api = _api(monkeypatch, [])
    for verdict in ({'error': 'timeout'}, {}, {'safe': None}):
        plan = api._fuse_results({'safety': verdict,
                                  'motor': {'trajectory': [_step(0.5)]}})
        assert plan['primary_action'] == 'halt', verdict
