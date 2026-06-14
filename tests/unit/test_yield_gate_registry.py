"""Behavioural tests for the core-layer yield-gate registry.

``core.foreground.should_yield_to_user`` is an inversion-of-control accessor to
the SINGLE canonical daemon-yield gate (``integrations.agent_engine.dispatch
.should_yield_to_user``).  ``core/`` may not import ``integrations/``, so dispatch
registers itself via ``core.foreground.set_yield_gate``.  These tests exercise
the real registry functions (no grep / source-shape assertions): they set the
gate, call the accessor, and observe the returned value — including the
fail-OPEN ``False`` contract when the gate is unregistered or raises.
"""
import core.foreground as fg


def test_unregistered_gate_fails_open_false():
    """No gate registered (None) -> accessor returns False (fail-open)."""
    saved = fg._yield_gate
    try:
        fg.set_yield_gate(None)
        assert fg.should_yield_to_user() is False
    finally:
        fg.set_yield_gate(saved)


def test_registered_gate_value_is_proxied():
    """A registered gate's boolean verdict is returned verbatim."""
    saved = fg._yield_gate
    try:
        fg.set_yield_gate(lambda: True)
        assert fg.should_yield_to_user() is True
        fg.set_yield_gate(lambda: False)
        assert fg.should_yield_to_user() is False
    finally:
        fg.set_yield_gate(saved)


def test_raising_gate_fails_open_false():
    """A gate that raises must not propagate — accessor fails open to False."""
    saved = fg._yield_gate

    def _boom():
        raise RuntimeError("gate exploded")

    try:
        fg.set_yield_gate(_boom)
        assert fg.should_yield_to_user() is False
    finally:
        fg.set_yield_gate(saved)


def test_importing_dispatch_registers_its_gate():
    """Importing dispatch wires its own should_yield_to_user as the gate."""
    saved = fg._yield_gate
    try:
        # Clear first so we observe the import-time side-effect, not a stale gate.
        fg.set_yield_gate(None)
        import importlib
        import integrations.agent_engine.dispatch as dispatch
        importlib.reload(dispatch)  # re-run module body -> re-registers the gate
        assert callable(fg._yield_gate)
        assert fg._yield_gate is dispatch.should_yield_to_user
    finally:
        fg.set_yield_gate(saved)
