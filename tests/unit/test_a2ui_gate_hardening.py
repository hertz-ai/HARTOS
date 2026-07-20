"""B2: agent_ui_update must be FULLY gated before any window.* verb rides it.
On top of the landed kill-switch + audit (test_a2ui_guardrail_audit.py) this adds
a per-agent rate cap, server-side XSS rejection, and a fail-CLOSED guardrail for
DESTRUCTIVE verbs (benign display cards stay fail-open).

Behavioural: real LiquidUIService, mock only the security boundary; assert the
observable refuse/allow and that benign cards are unaffected. No grep asserts.

    python -m pytest tests/unit/test_a2ui_gate_hardening.py --noconftest -p no:capture -q
"""
from unittest.mock import patch

import pytest

import integrations.agent_engine.liquid_ui_service as m
from integrations.agent_engine.liquid_ui_service import LiquidUIService


@pytest.fixture
def svc():
    return LiquidUIService(a2ui_enabled=True)


@pytest.fixture(autouse=True)
def _not_halted():
    # Isolate these tests from the kill-switch (covered separately).
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False):
        yield


def test_rate_cap_blocks_a_flood(svc):
    results = [svc.agent_ui_update('flooder', {'type': 'card'})
               for _ in range(30)]
    assert results[0] is True            # burst is allowed
    assert results[-1] is False          # a flood is eventually capped
    assert results.count(False) >= 5


def test_rate_cap_is_per_agent(svc):
    for _ in range(25):
        svc.agent_ui_update('noisy', {'type': 'card'})
    assert svc.agent_ui_update('quiet', {'type': 'card'}) is True


def test_xss_script_tag_rejected(svc):
    ok = svc.agent_ui_update('a', {'type': 'card',
                                   'title': '<script>steal()</script>'})
    assert ok is False
    assert svc._agent_components == {}


def test_xss_nested_javascript_uri_rejected(svc):
    ok = svc.agent_ui_update('a', {'type': 'form',
                                   'fields': [{'label': 'x',
                                               'value': 'javascript:evil()'}]})
    assert ok is False


def test_xss_img_onerror_rejected(svc):
    # The classic <img onerror=> vector the prior regex (script/iframe/
    # javascript:/data: only) MISSED — now caught via the tag + the inline
    # event-handler pattern.
    ok = svc.agent_ui_update('a', {'type': 'card',
                                   'title': '<img src=x onerror=alert(1)>'})
    assert ok is False
    assert svc._agent_components == {}


def test_xss_svg_onload_nested_field_rejected(svc):
    # Nested field (the recursion + broadened pattern together close it).
    ok = svc.agent_ui_update('a', {'type': 'product_card',
                                   'items': [{'name': '<svg onload=evil()>'}]})
    assert ok is False


def test_benign_text_with_on_word_not_false_rejected(svc):
    # The event-handler pattern is anchored to a tag context, so plain display
    # text like 'online =' / 'donation=' (no '<...') must NOT be rejected.
    ok = svc.agent_ui_update('a', {'type': 'card',
                                   'title': 'Status: online = true, donation=5'})
    assert ok is True


def test_benign_card_passes(svc):
    assert svc.agent_ui_update('a', {'type': 'card',
                                     'title': 'Q3 numbers ready'}) is True


def test_guardrail_ok_when_allowed(svc):
    with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               return_value=(True, '', '')):
        assert svc._a2ui_guardrail_ok({'type': 'window.close'}) is True


def test_guardrail_fail_closed_when_unavailable(svc):
    with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               side_effect=RuntimeError('guardrails down')):
        assert svc._a2ui_guardrail_ok({'type': 'window.close'}) is False


def test_destructive_verb_is_failclosed_in_push(svc):
    # A destructive type with an unavailable guardrail is blocked at push time;
    # a benign card with the same outage is not (fail-open).
    with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               side_effect=RuntimeError('down')):
        with patch.object(m, 'DESTRUCTIVE_COMPONENT_TYPES',
                          frozenset({'card'})):
            assert svc.agent_ui_update('a', {'type': 'card'}) is False
        # not destructive -> the guardrail outage doesn't block the card
        assert svc.agent_ui_update('b', {'type': 'card'}) is True
