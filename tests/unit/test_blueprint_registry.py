"""Behavioural tests for integrations.blueprint_registry.

register_all_blueprints(app) is the ONE wiring point both Nunba (main.py) and
standalone (hart_intelligence_entry.py) call to get every endpoint mounted. It
was 0% covered despite that. If a regression made it skip everything (or raise),
the app would boot with no routes — so the contract below is what must hold:
each registration is independently try/except-guarded, a missing dep is SKIPPED
not fatal, a None factory is skipped, an already-registered name is skipped
(no duplicate-name crash), and a live blueprint is actually registered.

Uses a fake app + sys.modules injection so no Flask app / real blueprint deps
are needed — the function's branching is what's under test, not the blueprints.
"""
from __future__ import annotations

import sys
import types

import pytest

from integrations.blueprint_registry import register_all_blueprints

MARKETPLACE_MOD = 'integrations.agent_engine.app_marketplace'


class FakeBP:
    def __init__(self, name):
        self.name = name


class FakeApp:
    """Minimal stand-in for a Flask app: a blueprints dict + register hook."""
    def __init__(self, preexisting=None):
        self.blueprints = {}
        for bp in (preexisting or []):
            self.blueprints[bp.name] = bp
        self.registered_calls = []

    def register_blueprint(self, bp):
        if bp.name in self.blueprints:
            # Flask itself raises on duplicate names; mirror that so a test
            # that lets a collision through would fail loudly here too.
            raise ValueError(f"duplicate blueprint name: {bp.name}")
        self.blueprints[bp.name] = bp
        self.registered_calls.append(bp)


def _inject_marketplace(monkeypatch, bp_value):
    """Put a fake app_marketplace module (with .marketplace_bp) in sys.modules
    so the registry's __import__ picks it up instead of the real one."""
    mod = types.ModuleType(MARKETPLACE_MOD)
    mod.marketplace_bp = bp_value
    monkeypatch.setitem(sys.modules, MARKETPLACE_MOD, mod)


class _RaisingModule:
    """A sys.modules stand-in whose marketplace_bp access raises — modules are
    immutable types so a property can't be attached to ModuleType, but
    sys.modules will happily hold any object with the right attribute protocol."""
    def __init__(self, exc):
        self._exc = exc

    @property
    def marketplace_bp(self):
        raise self._exc


def _bp_names(calls):
    return {getattr(bp, 'name', None) for bp in calls}


# ── contract shape ──────────────────────────────────────────────────────────
def test_returns_registered_and_skipped_lists():
    result = register_all_blueprints(FakeApp())
    assert set(result) == {'registered', 'skipped'}
    assert isinstance(result['registered'], list)
    assert isinstance(result['skipped'], list)
    # Every attempted blueprint lands in exactly one bucket; at least one entry.
    assert result['registered'] or result['skipped']


def test_import_error_is_skipped_not_fatal(monkeypatch):
    # The whole point of the per-blueprint try/except: a missing dep must skip
    # that blueprint, never abort the app boot. Injected deterministically (env
    # with all deps present would otherwise register everything).
    monkeypatch.setitem(
        sys.modules, MARKETPLACE_MOD, _RaisingModule(ImportError('no dep')))
    result = register_all_blueprints(FakeApp())  # must not raise
    assert 'marketplace' in result['skipped']
    assert 'marketplace' not in result['registered']


def test_no_blueprints_attr_does_not_crash():
    class BareApp:
        # No `.blueprints` — register_all_blueprints must tolerate it (the
        # hasattr guard) rather than AttributeError on a non-Flask caller.
        def register_blueprint(self, bp):
            pass
    result = register_all_blueprints(BareApp())
    assert set(result) == {'registered', 'skipped'}


# ── the register / skip branches, made deterministic via injection ──────────
def test_live_blueprint_is_registered(monkeypatch):
    _inject_marketplace(monkeypatch, FakeBP('marketplace'))
    app = FakeApp()
    result = register_all_blueprints(app)
    assert 'marketplace' in result['registered']
    assert 'marketplace' in app.blueprints


def test_none_factory_is_skipped(monkeypatch):
    _inject_marketplace(monkeypatch, None)  # marketplace_bp is None
    result = register_all_blueprints(FakeApp())
    assert 'marketplace' in result['skipped']
    assert 'marketplace' not in result['registered']


def test_already_registered_name_is_skipped_not_duplicated(monkeypatch):
    # A blueprint whose .name collides with one already on the app must be
    # skipped BEFORE register_blueprint is called (else Flask raises).
    _inject_marketplace(monkeypatch, FakeBP('marketplace'))
    app = FakeApp(preexisting=[FakeBP('marketplace')])
    result = register_all_blueprints(app)  # must not raise
    assert 'marketplace' in result['skipped']
    # The collided name was never (re-)registered. Other real blueprints on this
    # env may register fine — we only assert marketplace was not duplicated.
    assert 'marketplace' not in _bp_names(app.registered_calls)


def test_init_exception_is_swallowed_as_skip(monkeypatch):
    # A blueprint whose factory raises a non-ImportError must be caught by the
    # broad guard and skipped, not propagated.
    monkeypatch.setitem(
        sys.modules, MARKETPLACE_MOD,
        _RaisingModule(RuntimeError("blueprint init blew up")))
    result = register_all_blueprints(FakeApp())  # must not raise
    assert 'marketplace' in result['skipped']
