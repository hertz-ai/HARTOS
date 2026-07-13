"""The agentic HOME feed - ONE wired compose path (renderer + feed unified).

Before this, the home renderer (hartHome.js render()) was the single home
painter and the agentic intake (HartHome.compose, wired to the SSE
'home'/'home_compose' branch) existed - but the FEED was severed: COMPONENT_TYPES
did not allowlist 'home'/'home_compose', so agent_ui_update rejected the push,
and there was NO server-side producer. The SSE consumer was a dead consumer.

This test pins the unify: the allowlist accepts the type, compose_home is the ONE
server producer, and it flows through the SAME governed agent_ui_update channel
(not a parallel push) into the SSE store that HartHome.compose drains.

Behavioural (mock only the two security boundaries agent_ui_update consults;
call the REAL method/route; assert observable side-effects on the push store).

Run isolated (this box OOMs the full suite):
    python -m pytest tests/unit/test_home_compose_feed.py --noconftest -p no:capture -q
"""
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from integrations.agent_engine.liquid_ui_service import (
    COMPONENT_TYPES, LiquidUIService)


@pytest.fixture
def svc():
    return LiquidUIService(a2ui_enabled=True)


@pytest.fixture
def client(svc):
    app = svc._create_flask_app()
    app.config['TESTING'] = True
    return app.test_client()


_HERO = {'eyebrow': 'Earned on the hive', 'amount': 2140, 'amount_unit': 'Spark'}
_ROWS = [{'title': 'Continue', 'accent': 'teal',
          'cards': [{'title': 'Weekly recap', 'icon': 'edit_note'}]}]


# ── The allowlist no longer rejects the agentic home push ──

def test_home_compose_is_an_allowed_a2ui_component():
    # Without this, agent_ui_update drops the push (unknown type -> False) and the
    # SSE 'home_compose' consumer stays a dead consumer with no feed.
    assert 'home_compose' in COMPONENT_TYPES
    props = set(COMPONENT_TYPES['home_compose']['props'])
    assert {'hero', 'rows'} <= props
    # 'home' is the back-compat alias the SSE consumer also accepts.
    assert 'home' in COMPONENT_TYPES


# ── compose_home is the ONE producer; it reuses the wired push channel ──

def test_compose_home_delegates_to_agent_ui_update(svc):
    with patch.object(svc, 'agent_ui_update', return_value=True) as push:
        ok = svc.compose_home(hero=_HERO, rows=_ROWS)
    assert ok is True
    push.assert_called_once()
    agent_id, component = push.call_args[0]
    assert component['type'] == 'home_compose'      # canonical type
    assert component['hero'] == _HERO
    assert component['rows'] == _ROWS


def test_compose_home_noop_when_empty(svc):
    # Nothing to compose -> never touches the push channel, returns False.
    with patch.object(svc, 'agent_ui_update', return_value=True) as push:
        ok = svc.compose_home()
    assert ok is False
    push.assert_not_called()


# ── End-to-end through the REAL route into the SSE push store ──

def _compose(client, body):
    audit = MagicMock()
    with patch('security.immutable_audit_log.get_audit_log', return_value=audit), \
         patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False):
        r = client.post('/api/home/compose', data=json.dumps(body),
                        content_type='application/json')
    return r, json.loads(r.data)


def test_route_pushes_composition_into_the_sse_store(svc, client):
    r, data = _compose(client, {'hero': _HERO, 'rows': _ROWS})
    assert r.status_code == 200
    assert data['success'] is True
    # The composition reached the A2UI push store that the SSE stream drains and
    # HartHome.compose renders (the now-live feed, no parallel path).
    pushed = svc._agent_components.get('home_composer')
    assert pushed, 'composed home never reached the A2UI push store'
    comp = pushed[-1]
    assert comp['type'] == 'home_compose'
    assert comp['hero'] == _HERO and comp['rows'] == _ROWS


def test_route_accepts_a_wrapped_payload(svc, client):
    # {payload:{hero,rows}} is accepted too (an agent may wrap its composition).
    r, data = _compose(client, {'payload': {'hero': _HERO, 'rows': _ROWS},
                                'agent_id': 'home_composer'})
    assert data['success'] is True
    comp = svc._agent_components['home_composer'][-1]
    assert comp['hero'] == _HERO and comp['rows'] == _ROWS


def test_kill_switch_blocks_the_home_feed(svc, client):
    # The composer is governed like any agent dispatch: a halted hive refuses it.
    audit = MagicMock()
    with patch('security.immutable_audit_log.get_audit_log', return_value=audit), \
         patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=True):
        r = client.post('/api/home/compose',
                        data=json.dumps({'hero': _HERO, 'rows': _ROWS}),
                        content_type='application/json')
    data = json.loads(r.data)
    assert data['success'] is False
    assert svc._agent_components == {}      # nothing composed while halted


# ── G1: the LLM-composed ambient MOOD rides the SAME home push (was dropped) ──

def test_route_carries_the_llm_mood_into_the_store(svc, client):
    # The /api/home/compose route used to drop `mood`; now it flows
    # compose_home(mood=) -> agent_ui_update -> store, so the SSE consumer can
    # resolve it (HartPalette.byId) and paint it live. Wire (c) of mood->DOM.
    r, data = _compose(client, {'hero': _HERO, 'rows': _ROWS, 'mood': 'aurora'})
    assert data['success'] is True
    comp = svc._agent_components['home_composer'][-1]
    assert comp['type'] == 'home_compose'
    assert comp.get('mood') == 'aurora', 'the route dropped the mood (G1 regression)'


def test_route_wrapped_payload_carries_mood(svc, client):
    # mood on a wrapped {payload:{...,mood}} composition survives too.
    r, data = _compose(
        client, {'payload': {'hero': _HERO, 'rows': _ROWS, 'mood': 'solar'}})
    assert data['success'] is True
    assert svc._agent_components['home_composer'][-1].get('mood') == 'solar'


def test_sse_home_branch_wires_the_mood_to_the_dom(svc):
    """G1 wire (b): the served shell's SSE home_compose branch resolves the mood id
    (HartPalette.byId) and paints it LIVE (HartPalette.paint) — the LLM-composed mood
    reaches the DOM. The byId->paint PRIMITIVES are behaviourally tested in
    test_customization_hub.mjs ([G1] block); this asserts the 4-line glue is wired
    into the actual served shell (HartPalette.byId is introduced ONLY by this wire, so
    its presence in the render is unambiguous)."""
    html = svc.render_desktop_shell()
    assert 'HartPalette.byId' in html, 'SSE branch does not resolve the LLM mood id'
    assert 'HartPalette.paint' in html, 'SSE branch does not paint the resolved mood'
    # reuses the existing home_compose event (no new field/channel): reads .mood off it
    assert 'ev.mood' in html or '.mood' in html


# ── G4: the SSE store->CLIENT round-trip (the read leg, never integration-tested) ──

def test_sse_stream_delivers_a_pushed_component_to_the_client(svc, client):
    """G4: the full agent -> transport -> store -> CLIENT round-trip. A component
    pushed via agent_ui_update is DRAINED by /api/notifications/stream and delivered
    as an SSE 'data:' frame. Every prior 'SSE store' assertion stopped at the
    in-process _agent_components dict; this exercises the read leg the shell's
    EventSource actually consumes."""
    with patch('security.immutable_audit_log.get_audit_log', return_value=MagicMock()), \
         patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False):
        assert svc.agent_ui_update('agent_x',
                                   {'type': 'card', 'title': 'Hello G4'}) is True
    # Beat the stream's last_check (set when the generator starts) deterministically,
    # so there is no 2s thread race: stamp the stored component's _ts far in the future.
    svc._agent_components['agent_x'][-1]['_ts'] = time.time() + 3600
    with client.get('/api/notifications/stream',
                    headers={'Accept': 'text/event-stream'}, buffered=False) as r:
        first = next(iter(r.response), b'')
    if isinstance(first, str):
        first = first.encode()
    assert first.startswith(b'data: '), 'stream did not emit an SSE data frame: %r' % first
    assert b'Hello G4' in first, 'the pushed component never reached the SSE client leg'
    assert b'"type": "card"' in first or b'"type":"card"' in first


# ── G6: the agent-readable spec catalogue has a real consumer (a discovery route) ──

def test_a2ui_specs_route_exposes_the_component_catalogue(svc, client, tmp_path):
    """G6: GET /api/a2ui/specs returns list_component_specs() -- the ONE catalogue an
    agent / the local intelligence reads to know what components exist (builtins +
    agent-registered customs) and how to compose each from its spec. Was: the accessor
    had no route/consumer (called only by tests)."""
    svc._data_dir = str(tmp_path)   # keep the registered type out of the real data dir
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False):
        svc.register_component_type('composer', 'aura_ring',
                                    {'props': ['radius', 'hue']})
    r = client.get('/api/a2ui/specs')
    assert r.status_code == 200
    by_name = {s.get('name'): s for s in json.loads(r.data)['specs']}
    # builtins are catalogued with the agent-readable contract (mount + compose)
    assert 'card' in by_name and by_name['card'].get('mount') == 'a2ui'
    assert 'agent_ui_update' in by_name['card'].get('compose', '')
    assert 'metric' in by_name
    # the agent-registered custom type is catalogued too (runtime-extensible)
    assert 'aura_ring' in by_name


if __name__ == '__main__':
    # Inline runner (pytest OOMs on this box): execute every test_* and report.
    import sys
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    _svc = LiquidUIService(a2ui_enabled=True)
    _app = _svc._create_flask_app()
    _app.config['TESTING'] = True
    _client = _app.test_client()
    for name, fn in fns:
        argc = fn.__code__.co_argcount
        args = []
        # fresh svc/client per test (mirror the fixtures' isolation)
        if argc:
            _svc = LiquidUIService(a2ui_enabled=True)
            _app = _svc._create_flask_app()
            _app.config['TESTING'] = True
            _client = _app.test_client()
        names = fn.__code__.co_varnames[:argc]
        for n in names:
            args.append(_client if n == 'client' else _svc)
        try:
            fn(*args)
            print('  OK  ', name)
        except Exception as e:
            failed += 1
            print(' FAIL ', name, '->', repr(e))
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
