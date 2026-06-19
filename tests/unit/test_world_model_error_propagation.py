"""Embodied error propagation through the hevolveai hive.

WorldModelBridge talks to HevolveAI's embodied model over HTTP (/v1/actions,
/v1/sensors/batch, /v1/feedback/latest). Before this, a failure only bumped the
LOCAL circuit breaker — nothing reached the hive's error machinery. These tests
assert _propagate_embodied_error() records every embodied failure into the central
ExceptionCollector (the SAME sink SelfHealingDispatcher consumes → fix goals).
Node/subsystem reachability is NOT re-broadcast here — one health source of truth
(check_health / peer_discovery.get_health); a prior consumer-less
embodied.backend_down gossip was removed (parallel-path cleanup).

Behavioural: real WorldModelBridge, boundary (pooled_post / collector) mocked,
real circuit breaker exercised.

    python -m pytest tests/unit/test_world_model_error_propagation.py --noconftest -q
"""
from unittest.mock import patch, MagicMock

import requests

import integrations.agent_engine.world_model_bridge as wmb


def _make_bridge():
    """A bridge with in-process init skipped + HTTP enabled (talk to the mock)."""
    with patch.object(wmb.WorldModelBridge, '_init_in_process', lambda self: None):
        b = wmb.WorldModelBridge()
    b._http_disabled = False
    b._in_process = False
    b._node_id = 'node-test'
    return b


def _ok_action():
    return {'type': 'motor_velocity', 'target': 'left_wheel', 'params': {'velocity': 0.3}}


def test_send_action_non200_records_into_exception_collector():
    b = _make_bridge()
    resp = MagicMock(); resp.status_code = 500
    with patch.object(wmb, 'pooled_post', return_value=resp), \
            patch('exception_collector.ExceptionCollector') as MockEC:
        ok = b.send_action(_ok_action())
    assert ok is False
    inst = MockEC.get_instance.return_value
    assert inst.record.called, "embodied failure must reach the hive error sink"
    _, kwargs = inst.record.call_args
    assert kwargs.get('module') == 'world_model_bridge'
    assert kwargs.get('function') == 'send_action'
    assert kwargs.get('context', {}).get('status') == 500


def test_send_action_exception_records_into_exception_collector():
    b = _make_bridge()
    with patch.object(wmb, 'pooled_post',
                      side_effect=requests.exceptions.ConnectionError('boom')), \
            patch('exception_collector.ExceptionCollector') as MockEC:
        ok = b.send_action(_ok_action())
    assert ok is False
    assert MockEC.get_instance.return_value.record.called


def test_breaker_records_every_failure_to_collector_until_open():
    """Each embodied failure reaches the canonical error sink; once the breaker
    is OPEN the callers return early (no hammering a dead backend) — node health
    is surfaced via check_health/get_health, not a bespoke broadcast."""
    b = _make_bridge()
    resp = MagicMock(); resp.status_code = 500
    with patch.object(wmb, 'pooled_post', return_value=resp), \
            patch('exception_collector.ExceptionCollector') as MockEC:
        for _ in range(8):
            b.send_action(_ok_action())
    # default threshold=5 → 5 records, then send_action short-circuits on cb_open
    assert MockEC.get_instance.return_value.record.call_count == 5
    assert b._cb_is_open() is True


def test_sensor_ingest_failure_also_propagates():
    b = _make_bridge()
    resp = MagicMock(); resp.status_code = 503
    with patch.object(wmb, 'pooled_post', return_value=resp), \
            patch('exception_collector.ExceptionCollector') as MockEC:
        n = b.ingest_sensor_batch([{'sensor_id': 's0', 'sensor_type': 'imu'}])
    assert n == 0
    _, kwargs = MockEC.get_instance.return_value.record.call_args
    assert kwargs.get('function') == 'ingest_sensor_batch'


def test_success_does_not_propagate_or_gossip():
    b = _make_bridge()
    resp = MagicMock(); resp.status_code = 200
    with patch.object(wmb, 'pooled_post', return_value=resp), \
            patch('exception_collector.ExceptionCollector') as MockEC:
        ok = b.send_action(_ok_action())
    assert ok is True
    assert not MockEC.get_instance.return_value.record.called


def test_instruct_builds_canonical_vla_action_and_dispatches():
    """High-level instruct() → RobotAction.vla_instruct → send_action (one call)."""
    b = _make_bridge()
    captured = {}

    def fake_send(action):
        captured['action'] = action
        return True

    b.send_action = fake_send
    ok = b.instruct('pick up the red cube', observation={'rgb': 'x'}, horizon=4)
    assert ok is True
    a = captured['action']
    assert a['type'] == 'vla_instruct'
    assert a['params']['instruction'] == 'pick up the red cube'
    assert a['params']['horizon'] == 4
    assert a['params']['observation'] == {'rgb': 'x'}
