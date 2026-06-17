"""M1 — acSend is INVERTED from a launcher into intent → decompose → compose.

The 4.5 → 7 move (see docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md §1 +
the validation report §4.2 M1): the DEFAULT desktop intent path must route
free-form intent through the brain's EXISTING decompose (/chat → CREATE/REUSE,
NO parallel path) and render the result as COMPOSED UI pushed through the
now-wired agent_ui_update channel — not only a chat bubble + TTS.  'open
<named app>' is demoted to a fallback fast-path.

Behavioural (mock the brain boundary, call the real route, assert observable
side-effects — NO grep/string tests for the behaviour):
  - POST an intent to /api/agent/ask → a composed card reaches the A2UI push
    store (_agent_components) AND the route reports composed=True;
  - the casual-chat reply text is still returned (the bubble is preserved);
  - when the human has halted the hive, NO composed card is pushed (the
    kill-switch governs the composer) but the reply still comes back;
  - _compose_intent_result reuses agent_ui_update (the real wired channel),
    not a parallel push.

One clearly-labelled source guard (test_source_guard_*) asserts the JS
fallback fast-path 'open <app>' survives — a behavioural JS test would need a
headless WebKit, so the launcher-fallback preservation is guarded at source
ALONGSIDE the behavioural backend tests above (per memory/feedback_no_grep_tests.md).

Run isolated (this box OOMs the full suite):
    python -m pytest tests/unit/test_m1_intent_compose.py --noconftest -p no:capture -q
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


@pytest.fixture
def svc():
    return LiquidUIService(a2ui_enabled=True)


@pytest.fixture
def client(svc):
    app = svc._create_flask_app()
    app.config['TESTING'] = True
    return app.test_client()


def _fake_chat(response='Drafted your trip plan.', **extra):
    """Stand in for the brain's /chat intent classifier (CREATE/REUSE/...)."""
    payload = {'response': response}
    payload.update(extra)
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def _ask(client, audit, text='plan a weekend trip to Goa', halted=False,
         chat=None):
    """POST an intent through the REAL route with ONLY the brain boundary +
    the two security boundaries mocked."""
    chat = chat if chat is not None else _fake_chat()
    with patch('requests.post', return_value=chat), \
         patch('security.immutable_audit_log.get_audit_log',
               return_value=audit), \
         patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=halted):
        r = client.post('/api/agent/ask',
                        data=json.dumps({'text': text}),
                        content_type='application/json')
    return r, json.loads(r.data)


# ── The crux: intent → decompose → compose (not just a bubble) ──

def test_intent_routes_through_brain_and_composes_a_card(svc, client):
    audit = MagicMock()
    r, data = _ask(client, audit)
    assert r.status_code == 200
    # The route reports it COMPOSED the desktop, not just narrated.
    assert data['composed'] is True
    # A composed card reached the A2UI push store (the real wired channel
    # agent_ui_update writes to → SSE → renderAgentOverlay paints it).
    pushed = svc._agent_components.get('desktop_intent')
    assert pushed, 'composed card never reached the A2UI push store'
    card = pushed[-1]
    assert card['type'] == 'card'
    assert card['content'] == 'Drafted your trip plan.'   # the brain's decompose
    assert card['intent'] == 'plan a weekend trip to Goa'  # carried verbatim
    # The compose was provable, exactly like a goal dispatch.
    audit.log_event.assert_called()


def test_brain_decompose_is_reused_not_a_parallel_path(svc, client):
    # The composed content MUST come from the brain's /chat payload — proving
    # we route through the existing intent classifier, not a second decompose.
    audit = MagicMock()
    r, data = _ask(client, audit, text='build me a CSV of my expenses',
                   chat=_fake_chat(response='Created the expense agent.',
                                   Agent_status='Review Mode',
                                   prompt_id='42'))
    assert data['composed'] is True
    card = svc._agent_components['desktop_intent'][-1]
    assert card['content'] == 'Created the expense agent.'
    # The brain's own routing decided the framing (agent created → agent glyph).
    assert card['title'] == 'Review Mode'
    assert card['icon'] == 'smart_toy'


def test_casual_chat_reply_still_returned(svc, client):
    # Inverting the surface must NOT break casual chat: the reply text is still
    # returned so the bubble renders + TTS speaks.
    audit = MagicMock()
    r, data = _ask(client, audit, text='hello there',
                   chat=_fake_chat(response='Hi! How can I help?'))
    assert data['response'] == 'Hi! How can I help?'
    assert data['composed'] is True


def test_kill_switch_blocks_the_composer_but_reply_survives(svc, client):
    # When the human halts the hive, the COMPOSER is governed like any dispatch
    # — no card is painted — but the conversational reply still comes back.
    audit = MagicMock()
    r, data = _ask(client, audit, halted=True)
    assert data['composed'] is False                 # nothing composed
    assert svc._agent_components == {}               # no card reached the store
    assert data['response'] == 'Drafted your trip plan.'  # bubble preserved


def test_empty_brain_reply_composes_nothing(svc, client):
    audit = MagicMock()
    r, data = _ask(client, audit, chat=_fake_chat(response=''))
    assert data['composed'] is False
    assert svc._agent_components == {}


def test_compose_helper_uses_the_wired_push_channel(svc):
    # _compose_intent_result MUST delegate to agent_ui_update (the single wired
    # B1/B2 channel), never a parallel push — DRY / one-dispatch-path gate.
    with patch.object(svc, 'agent_ui_update',
                      return_value=True) as push:
        ok = svc._compose_intent_result(
            'do the thing', {'response': 'done'})
    assert ok is True
    push.assert_called_once()
    agent_id, component = push.call_args[0]
    assert component['type'] == 'card'
    assert component['content'] == 'done'


# ── Source guard (explicitly labelled): the 'open <app>' fallback survives ──
# A behavioural test of the client launcher would need a headless WebKit; the
# backend behaviour above is the primary coverage.  This guard only proves the
# demoted fast-path was NOT deleted in the inversion (memory/feedback_no_grep_tests.md
# permits a labelled source guard ALONGSIDE — never instead of — behavioural tests).

def test_source_guard_open_app_fallback_fastpath_preserved():
    import inspect
    from integrations.agent_engine import liquid_ui_service as m
    src = inspect.getsource(m)
    # Both intent entry points keep the launch-a-named-app fast-path.
    assert src.count("Fallback fast-path: launch a NAMED app directly") == 2
    # And the default still posts to the brain compose route.
    assert "/api/agent/ask" in src
