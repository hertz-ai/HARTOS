"""An agent UI card never leaves the node on the EventBus WAMP bridge.

Review of a0ecafe09: once the backend owns a LiquidUIService, every card it
accepts is emitted as ``agent.ui.update``, and the EventBus WAMP bridge
publishes every emit to ``com.hartos.event.<topic>``.  That URI carries no
user, so by the canonical Crossbar rule (security.edge_privacy
.crossbar_leg_is_users_own: a URI that is not the user's own reaches
whoever subscribes, which is other people) it is not the user's.  Nunba
points that bridge at central in Hybrid/Hive mode, and a WhatsApp pair_code
card carries the live linking code in ``code`` / ``clipboard_payload`` /
``deeplink``, which no DLP pattern matches.  At a0ecafe09^ nothing in the
backend emitted the card, so this was a new egress.

Contract pinned here: the card still reaches the owner's SSE stream (the
Demopage) and in-process listeners; it is never published on the WAMP
bridge; every other topic keeps bridging exactly as before.

Grown from the reviewer's probe (scratchpad/review_rework/
probe_lui_wamp_egress.py).  Real EventBus, real LiquidUIService, a real
bootstrap registration; the WAMP session is a recording fake, and the SSE
transport, audit DB and hive breaker are the mocked boundaries.
"""
import asyncio
import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from core.platform.events import EventBus
from core.platform.registry import get_registry, reset_registry

CODE = 'K7Q2-9XPL'


class _RecordingSession:
    def __init__(self):
        self.published = []

    async def publish(self, uri, payload):
        self.published.append((uri, payload))


@pytest.fixture()
def wamp_bus(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_DATA_DIR', str(tmp_path))
    reset_registry()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    session = _RecordingSession()
    bus = EventBus()
    bus._wamp_session = session
    bus._wamp_loop = loop
    bus._wamp_connected = True
    get_registry().register('events', lambda: bus, singleton=True)
    yield bus, session, loop
    loop.call_soon_threadsafe(loop.stop)
    reset_registry()


def _drain(loop):
    """Let every publish already handed to the WAMP loop run."""
    for _ in range(3):
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(5)


def _backend_liquid_ui():
    from core.platform.bootstrap import _register_liquid_ui
    reg = get_registry()
    with patch('core.port_registry.is_os_mode', return_value=False):
        _register_liquid_ui(reg)
    return reg.get('LiquidUIService')


def _push_pair_code(svc, bus):
    """Push the WhatsApp pair_code card exactly as hart_intelligence_entry
    does, with the EventBus emit made synchronous so the test can assert on
    everything the emit did before it returned."""
    card = {'type': 'pair_code', 'channel': 'whatsapp',
            'channel_type': 'whatsapp', 'code': CODE,
            'clipboard_payload': CODE,
            'deeplink': 'whatsapp://link?code=' + CODE,
            'instructions': 'enter ' + CODE + ' (60-second window)'}
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False), \
            patch('security.immutable_audit_log.get_audit_log',
                  return_value=MagicMock()), \
            patch('core.platform.events.emit_event',
                  side_effect=lambda t, d=None, async_=True: bus.emit(t, d)):
        return svc.agent_ui_update('u-1', card, user_id='u-1')


def test_pair_code_card_is_never_published_on_the_wamp_bridge(wamp_bus):
    bus, session, loop = wamp_bus
    svc = _backend_liquid_ui()
    local = []
    bus.on('agent.ui.update', lambda t, d: local.append(d))

    with patch('core.platform.events.broadcast_sse_safe') as sse:
        assert _push_pair_code(svc, bus) is True
    _drain(loop)

    leaked = [(u, p) for u, p in session.published
              if 'agent.ui' in u or CODE in json.dumps(p)]
    assert leaked == [], 'the linking code left the node: %r' % leaked
    # ...while the person it is for still gets it, on their own stream.
    sse_calls = [c for c in sse.call_args_list
                 if c.args and c.args[0] == 'agent.ui.update']
    assert sse_calls and sse_calls[0].kwargs.get('user_id') == 'u-1'
    assert CODE in json.dumps(sse_calls[0].args[1])
    # ...and in-process listeners are untouched.
    assert local and local[0]['component']['code'] == CODE


def test_other_topics_still_bridge_to_wamp(wamp_bus):
    """The withhold is for agent UI only: peer gossip, theme and the rest
    of the node's events keep their WAMP leg."""
    bus, session, loop = wamp_bus
    with patch('core.platform.events.broadcast_sse_safe'):
        bus.emit('peer.capability.announce', {'node': 'n1'})
        bus.emit('theme.changed', {'theme': 'aurora'})
    _drain(loop)
    uris = [u for u, _ in session.published]
    assert 'com.hartos.event.peer.capability.announce' in uris
    assert 'com.hartos.event.theme.changed' in uris


def test_an_agent_ui_event_from_the_wamp_side_is_not_echoed(wamp_bus):
    """The inbound leg is unchanged: a card that ARRIVED over WAMP fires
    local listeners and is not re-published."""
    bus, session, loop = wamp_bus
    local = []
    bus.on('agent.ui.update', lambda t, d: local.append(d))
    bus.emit('agent.ui.update', {'agent_id': 'a', 'component': {'type': 'card'}},
             _from_wamp=True)
    _drain(loop)
    assert local
    assert session.published == []


def test_the_withhold_is_decided_by_the_canonical_ownership_rule(wamp_bus):
    """The bridge holds no ownership rule of its own: it asks
    crossbar_leg_is_users_own about the URI it would publish, for the card's
    user.  Were that URI the user's own, the card would bridge (proved
    unpatched in test_egress_one_rule.py); the rule is asked about exactly
    com.hartos.event.agent.ui.update and u-1."""
    bus, session, loop = wamp_bus
    asked = []

    def _rule(uri, user_id=''):
        asked.append((uri, user_id))
        return True

    with patch('security.edge_privacy.crossbar_leg_is_users_own',
               side_effect=_rule), \
            patch('core.platform.events.broadcast_sse_safe'):
        bus.emit('agent.ui.update', {'agent_id': 'a', 'user_id': 'u-1',
                                     'component': {'type': 'card'}})
    _drain(loop)
    assert asked == [('com.hartos.event.agent.ui.update', 'u-1')]
    assert [u for u, _ in session.published] == [
        'com.hartos.event.agent.ui.update']


def test_the_real_rule_says_the_bridge_uri_is_not_the_users():
    from core.peer_link.message_bus import crossbar_topic_is_per_user
    from security.edge_privacy import crossbar_uri_is_per_user
    assert crossbar_uri_is_per_user('com.hartos.event.agent.ui.update',
                                    'u-1') is False
    assert crossbar_uri_is_per_user('com.hartos.event.agent.ui.update') is False
    assert crossbar_uri_is_per_user('com.hertzai.hevolve.chat.{user_id}') is True
    # the MessageBus question still answers through the same rule
    assert crossbar_topic_is_per_user('chat.response') is True
    assert crossbar_topic_is_per_user('community.feed') is False


def test_nothing_bridges_when_the_rule_cannot_be_asked(wamp_bus):
    """Every bridged event asks the egress rule (review of a4ea04651: the
    bridge carried addressed topics raw); with no rule, nothing leaves on
    the bridge -- the owner keeps SSE and in-process listeners."""
    bus, session, loop = wamp_bus
    import builtins
    real_import = builtins.__import__

    def _no_bus(name, *a, **k):
        if name == 'security.edge_privacy':
            raise ImportError('simulated')
        return real_import(name, *a, **k)

    with patch('builtins.__import__', side_effect=_no_bus), \
            patch('core.platform.events.broadcast_sse_safe'):
        bus.emit('agent.ui.update', {'agent_id': 'a',
                                     'component': {'type': 'card'}})
        bus.emit('theme.changed', {'theme': 'aurora'})
    _drain(loop)
    assert session.published == []
