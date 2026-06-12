"""#66 — Direction B cross-process skill relay (out-of-process HiveMind).

When HiveMind runs in the spawned HevolveAI subprocess (bundled topology), the
in-process set_inbound_skill_hook never fires in HARTOS, so HARTOS-only peers
never learned WAMP-received skills.  Fix: the out-of-process bridge subscribes
to the SHARED `hivemind.skill.share` MessageBus topic itself and fans out via
the same Direction-B handler — no dependency on the (closed-source) HevolveAI
side.  These mock the bus + the fan-out boundary and assert the wiring.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _make_oop_bridge(bus):
    """Build a bridge in the OUT-of-process state (HiveMind not local), with the
    MessageBus mocked so __init__'s Direction-B subscription is observable."""
    env = {'HEVOLVEAI_API_URL': '', 'HEVOLVE_NODE_TIER': 'flat'}
    with patch.dict(os.environ, env, clear=False), \
         patch('integrations.agent_engine.world_model_bridge.WorldModelBridge._init_in_process'), \
         patch('integrations.agent_engine.world_model_bridge.WorldModelBridge._start_crawl_integrity_watcher'), \
         patch('core.peer_link.message_bus.get_message_bus', return_value=bus):
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        b = WorldModelBridge()
    b._in_process_retry_done = True
    return b


@pytest.fixture
def bus():
    return MagicMock()


def test_out_of_process_subscribes_to_skill_share(bus):
    """The relay activates at construction: an out-of-process bridge subscribes
    to hivemind.skill.share over the MessageBus (was a dead warning before)."""
    try:
        b = _make_oop_bridge(bus)
    except Exception as e:
        pytest.skip(f"world_model_bridge unavailable: {e}")
    assert b._in_process is False, "fixture must produce the out-of-process state"
    topics = [c.args[0] for c in bus.subscribe.call_args_list]
    assert 'hivemind.skill.share' in topics, (
        "out-of-process bridge must subscribe to hivemind.skill.share")


def test_cross_process_skill_forwards_to_direction_b(bus):
    try:
        b = _make_oop_bridge(bus)
    except Exception as e:
        pytest.skip(f"world_model_bridge unavailable: {e}")
    b._on_inbound_wamp_skill = MagicMock()
    pkt = {'event': 'ralt', 'task_id': 't1',
           'packet_wire': {'description': 'a skill'}, 'origin_node': 'remote-node'}

    b._on_cross_process_skill('hivemind.skill.share', pkt)

    b._on_inbound_wamp_skill.assert_called_once()
    call = b._on_inbound_wamp_skill.call_args
    assert call.args[0] is pkt
    assert call.kwargs.get('source') == 'remote-node'


def test_cross_process_skill_drops_own_publish(bus):
    """Defensive echo-prevention: a packet whose origin_node is our own node id
    is dropped (not re-fanned-out)."""
    try:
        b = _make_oop_bridge(bus)
    except Exception as e:
        pytest.skip(f"world_model_bridge unavailable: {e}")
    b._node_id = 'my-node'
    b._on_inbound_wamp_skill = MagicMock()

    b._on_cross_process_skill(
        'hivemind.skill.share',
        {'event': 'ralt', 'origin_node': 'my-node', 'packet_wire': {}})

    b._on_inbound_wamp_skill.assert_not_called()


def test_cross_process_skill_ignores_non_dict(bus):
    try:
        b = _make_oop_bridge(bus)
    except Exception as e:
        pytest.skip(f"world_model_bridge unavailable: {e}")
    b._on_inbound_wamp_skill = MagicMock()
    b._on_cross_process_skill('hivemind.skill.share', "not-a-dict")
    b._on_inbound_wamp_skill.assert_not_called()


# ---------------------------------------------------------------------------
# #118-B9: HTTP poller relay (the WORKING cross-process channel).
# The bus subscription above only fires for in-process republishers;
# Direction A publishes to WAMP via the in-process HiveMind, so out of
# process the bus topic has no publisher. The poller drains metadata from
# the HevolveAI subprocess's /v1/hivemind/inbound-events endpoint instead.
# ---------------------------------------------------------------------------

def _make_oop_bridge_with_http(bus):
    """Out-of-process bridge with an EXPLICIT HEVOLVEAI_API_URL so the HTTP
    channel (and thus the poller) stays enabled."""
    env = {'HEVOLVEAI_API_URL': 'http://127.0.0.1:8000', 'HEVOLVE_NODE_TIER': 'flat'}
    with patch.dict(os.environ, env, clear=False), \
         patch('integrations.agent_engine.world_model_bridge.WorldModelBridge._init_in_process'), \
         patch('integrations.agent_engine.world_model_bridge.WorldModelBridge._start_crawl_integrity_watcher'), \
         patch('core.peer_link.message_bus.get_message_bus', return_value=bus):
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        b = WorldModelBridge()
    b._in_process_retry_done = True
    return b


def test_poller_started_when_http_enabled(bus):
    try:
        b = _make_oop_bridge_with_http(bus)
    except Exception as e:
        pytest.skip(f"world_model_bridge unavailable: {e}")
    assert getattr(b, '_inbound_poller_thread', None) is not None, (
        "explicit HEVOLVEAI_API_URL must start the inbound-skill poller")


def test_poller_not_started_when_http_disabled(bus):
    try:
        b = _make_oop_bridge(bus)
    except Exception as e:
        pytest.skip(f"world_model_bridge unavailable: {e}")
    assert getattr(b, '_inbound_poller_thread', None) is None, (
        "no API URL -> HTTP disabled -> no poller")


def test_poll_relays_events_and_advances_cursor(bus):
    try:
        b = _make_oop_bridge_with_http(bus)
    except Exception as e:
        pytest.skip(f"world_model_bridge unavailable: {e}")
    b._on_inbound_wamp_skill = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        'status': 'ok',
        'now': 1234.5,
        'events': [{'event': 'ralt', 'task_id': 't42', 'source': 'peerQ',
                    'description': 'skill desc', 'complexity': 'low',
                    'origin_node': 'nodeQ', 'ts': 1234.0}],
    }
    with patch('integrations.agent_engine.world_model_bridge.pooled_get',
               return_value=resp) as pg:
        n = b._poll_inbound_skill_events()
    assert n == 1
    assert b._inbound_poll_cursor == 1234.5, "cursor must advance to server clock"
    pg.assert_called_once()
    packet, = b._on_inbound_wamp_skill.call_args.args[:1]
    assert packet['event'] == 'ralt'
    assert packet['task_id'] == 't42'
    assert packet['packet_wire']['description'] == 'skill desc'
    assert b._on_inbound_wamp_skill.call_args.kwargs['source'] == 'peerQ'


def test_poll_no_events_is_noop(bus):
    try:
        b = _make_oop_bridge_with_http(bus)
    except Exception as e:
        pytest.skip(f"world_model_bridge unavailable: {e}")
    b._on_inbound_wamp_skill = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {'status': 'ok', 'now': 99.0, 'events': []}
    with patch('integrations.agent_engine.world_model_bridge.pooled_get',
               return_value=resp):
        n = b._poll_inbound_skill_events()
    assert n == 0
    b._on_inbound_wamp_skill.assert_not_called()
