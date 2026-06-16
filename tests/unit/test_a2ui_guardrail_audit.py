"""agent_ui_update must respect the hive kill-switch and write a provable
audit trail — the Phase-2 safety prerequisite before any window.* verb can
ride the A2UI push path (see docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md).

Constitution: "every dispatch is provable" — an agent pushing UI is now
governed exactly like an agent dispatching a goal (dispatch.py:669-683): the
push is refused while the human has halted the HiveCircuitBreaker, and every
accepted push is recorded in the immutable audit log.

Behavioural (no grep/source-shape assertions): constructs the REAL
LiquidUIService, mocks ONLY the two security boundaries (the immutable audit
log + the circuit breaker), calls the real agent_ui_update, and asserts the
observable side-effects — refused-when-halted, stored, audited, fail-open.

Run isolated (this box OOMs the full suite):
    python -m pytest tests/unit/test_a2ui_guardrail_audit.py --noconftest -p no:capture -q
"""
from unittest.mock import MagicMock, patch

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


@pytest.fixture
def svc():
    return LiquidUIService(a2ui_enabled=True)


def _push(svc, audit, halted=False, agent_id='agent-7', component=None):
    component = component if component is not None else {
        'type': 'card', 'title': 'Q3 review'}
    with patch('security.immutable_audit_log.get_audit_log',
               return_value=audit), \
         patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=halted):
        return svc.agent_ui_update(agent_id, component)


def test_valid_push_is_recorded_in_immutable_audit(svc):
    audit = MagicMock()
    ok = _push(svc, audit)
    assert ok is True
    audit.log_event.assert_called_once()
    blob = repr(audit.log_event.call_args)
    assert 'card' in blob and 'agent-7' in blob   # who pushed what
    assert svc._agent_components.get('agent-7')    # reached the SSE store


def test_push_refused_when_hive_circuit_breaker_halted(svc):
    audit = MagicMock()
    ok = _push(svc, audit, halted=True)
    assert ok is False
    assert svc._agent_components == {}             # never reached the store


def test_breaker_unavailable_fails_open_for_benign_ui(svc):
    # If the guardrail module can't be consulted, a benign consent card must
    # still flow — failing closed here would silently break channel consent.
    audit = MagicMock()
    with patch('security.immutable_audit_log.get_audit_log',
               return_value=audit), \
         patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               side_effect=RuntimeError('guardrail import failed')):
        ok = svc.agent_ui_update('agent-7', {'type': 'card'})
    assert ok is True
    assert svc._agent_components.get('agent-7')


def test_audit_failure_does_not_block_benign_push(svc):
    # Audit is best-effort for benign UI cards — a logging hiccup must not
    # drop a user's consent card.
    audit = MagicMock()
    audit.log_event.side_effect = RuntimeError('disk full')
    ok = _push(svc, audit)
    assert ok is True
    assert svc._agent_components.get('agent-7')


def test_invalid_component_type_still_rejected(svc):
    audit = MagicMock()
    ok = _push(svc, audit, component={'type': 'not_a_real_type'})
    assert ok is False
    audit.log_event.assert_not_called()


def test_a2ui_disabled_short_circuits(svc):
    svc.a2ui_enabled = False
    assert svc.agent_ui_update('a', {'type': 'card'}) is False
