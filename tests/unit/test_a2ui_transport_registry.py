"""B1 (in-process half): the A2UI push channel must be reachable through ONE
canonical, KeyError-safe accessor, and a served LiquidUIService must register
itself so in-process emitters (channel consent cards, the voice bridge,
model-ready toasts) can find it.

Regression this guards: the six callers reached for the instance three broken
ways — a dead `from core.platform.service_registry import ServiceRegistry`
(that module does not exist; only `core.platform.registry`), `ServiceRegistry.get()`
called on the CLASS (it is an instance method), and `'LiquidUIService'` registered
NOWHERE in production. Every push silently no-op'd.

Behavioural (no grep/source-shape asserts): uses the REAL ServiceRegistry and a
REAL LiquidUIService; asserts get_or_none is KeyError-safe, _register_self makes
the instance reachable + is idempotent, and a push via the canonical accessor
lands in the SSE store.

Run isolated (this box OOMs the full suite):
    python -m pytest tests/unit/test_a2ui_transport_registry.py --noconftest -p no:capture -q
"""
from unittest.mock import patch

import pytest

from core.platform.registry import get_registry, reset_registry
from integrations.agent_engine.liquid_ui_service import LiquidUIService


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


def test_get_or_none_is_keyerror_safe():
    # The old callers crashed/raised; the canonical accessor returns None.
    assert get_registry().get_or_none('LiquidUIService') is None


def test_register_self_makes_instance_reachable():
    svc = LiquidUIService(a2ui_enabled=True)
    svc._register_self()
    assert get_registry().get_or_none('LiquidUIService') is svc


def test_register_self_is_idempotent():
    svc = LiquidUIService(a2ui_enabled=True)
    svc._register_self()
    svc._register_self()   # a second serve must not raise double-register
    assert get_registry().get_or_none('LiquidUIService') is svc


def test_push_via_canonical_accessor_reaches_store():
    svc = LiquidUIService(a2ui_enabled=True)
    svc._register_self()
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False):
        reached = get_registry().get_or_none('LiquidUIService').agent_ui_update(
            'agent-9', {'type': 'card', 'title': 'composed by intent'})
    assert reached is True
    assert svc._agent_components.get('agent-9')   # landed in the SSE store
