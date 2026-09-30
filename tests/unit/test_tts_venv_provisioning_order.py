"""Which TTS engine the bootstrap provisioner asks about next, and what a node
with no owner is told.

Review of 0fdd62bad / Nunba e5351913 (the consent gate in repair_backend_venv):

1. bootstrap_provision_tts_venvs picked "the FIRST engine where
   is_venv_healthy returns False" in its prose, and stopped when the repair
   returned success=False.  An engine the owner declined is unhealthy forever
   and its repair always returns success=False, so every tick picked it again
   and the other engines were never asked.  The choice is now code,
   next_tts_venv_to_provision(), which skips healthy engines AND engines the
   owner declined, so the provisioner moves on.

2. A node with no owner (HEVOLVE_OWNER_USER_ID unset: a server) can no longer
   install an engine through the tool.  The result now says why and what to
   do (capability_setup.NO_OWNER_REMEDY) instead of "the owner has not
   allowed it".

Boundaries stubbed: ENGINE_REGISTRY, Nunba's venv layer (tts.backend_venv,
tts.package_installer) and the consent store (ConsentService on a db_session).
The two tools and capability_setup run for real.
"""
import json
import sys
import types
from contextlib import contextmanager

import pytest

from integrations.agent_engine import capability_setup as cs
from integrations.coding_agent import backend_repair_tools as brt


# ── boundaries ──────────────────────────────────────────────────────────────

class _Spec:
    def __init__(self, engine_id, install_target):
        self.engine_id = engine_id
        self.install_target = install_target


ENGINES = [('a_venv', 'venv'), ('b_venv', 'venv'), ('main_one', 'main'),
           ('c_venv', 'venv'), ('d_venv', 'venv')]


@pytest.fixture
def registry(monkeypatch):
    tr = types.ModuleType('integrations.channels.media.tts_router')
    tr.ENGINE_REGISTRY = {e: _Spec(e, t) for e, t in ENGINES}
    monkeypatch.setitem(sys.modules, 'integrations.channels.media.tts_router', tr)
    return tr.ENGINE_REGISTRY


@pytest.fixture
def venv_layer(monkeypatch):
    """Nunba's venv layer: which venvs are healthy, and every install."""
    state = {'healthy': set(), 'installs': []}
    bv = types.ModuleType('tts.backend_venv')
    bv.is_venv_healthy = lambda engine, *a, **k: engine in state['healthy']
    bv.wipe_venv = lambda engine: None
    pi = types.ModuleType('tts.package_installer')

    def install_backend_full(engine):
        state['installs'].append(engine)
        state['healthy'].add(engine)
        return True, 'Ready'

    pi.install_backend_full = install_backend_full
    monkeypatch.setitem(sys.modules, 'tts', types.ModuleType('tts'))
    monkeypatch.setitem(sys.modules, 'tts.backend_venv', bv)
    monkeypatch.setitem(sys.modules, 'tts.package_installer', pi)
    return state


@pytest.fixture
def consent(monkeypatch):
    """The consent store at its boundary: the owner's answers per scope.
    'yes' / 'no' are on file; anything else is not answered yet (asking
    files a card, recorded in `asked`)."""
    state = {'answers': {}, 'asked': []}

    class _Consent:
        @staticmethod
        def check_or_request(db, owner, ctype, scope='*', reason=''):
            ans = state['answers'].get(scope)
            if ans is None:
                state['asked'].append(scope)
            return ans == 'yes'

        @staticmethod
        def check_consent(db, owner, ctype, scope='*', agent_id=None):
            return state['answers'].get(scope) == 'yes'

        @staticmethod
        def declined(db, owner, ctype, scope='*', agent_id=None):
            return state['answers'].get(scope) == 'no'

    @contextmanager
    def db_session(commit=False):
        yield object()

    svc = types.ModuleType('integrations.social.consent_service')
    svc.ConsentService = _Consent
    models = types.ModuleType('integrations.social.models')
    models.db_session = db_session
    monkeypatch.setitem(sys.modules, 'integrations.social.consent_service', svc)
    monkeypatch.setitem(sys.modules, 'integrations.social.models', models)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    return state


def _next():
    return json.loads(brt.next_tts_venv_to_provision())


def _repair(engine):
    return json.loads(brt.repair_backend_venv(engine))


# ── 1. the provisioner moves past a decline ────────────────────────────────

def test_the_next_engine_skips_healthy_and_declined_ones(
        registry, venv_layer, consent):
    venv_layer['healthy'].add('a_venv')
    consent['answers']['tts:b_venv'] = 'no'

    got = _next()

    assert got['engine'] == 'c_venv'
    assert got['declined'] == ['b_venv']
    assert got['done'] is False


def test_only_venv_engines_are_considered(registry, venv_layer, consent):
    for e in ('a_venv', 'b_venv', 'c_venv', 'd_venv'):
        venv_layer['healthy'].add(e)
    got = _next()
    assert got['engine'] is None and got['done'] is True   # main_one ignored


def test_the_provisioner_asks_about_every_engine_across_ticks(
        registry, venv_layer, consent):
    """The regression, as the goal runs it: one engine per tick.  The owner
    says no to the first; the others are still asked, one card at a time."""
    consent['answers']['tts:a_venv'] = 'no'
    consent['answers']['tts:c_venv'] = 'yes'

    picked = []
    for _tick in range(8):
        nxt = _next()
        if nxt['done'] or nxt['engine'] is None:
            break
        picked.append(nxt['engine'])
        result = _repair(nxt['engine'])
        if result.get('consent') == 'asked':
            consent['answers'][f"tts:{nxt['engine']}"] = 'yes'   # owner answers

    assert consent['asked'] == ['tts:b_venv', 'tts:d_venv']   # a_venv declined
    assert set(venv_layer['installs']) == {'b_venv', 'c_venv', 'd_venv'}
    assert 'a_venv' not in venv_layer['installs']


def test_an_unanswered_card_holds_the_provisioner_on_that_engine(
        registry, venv_layer, consent):
    """One card at a time: an engine still waiting for an answer is the one
    offered next (the owner is not sent a burst of cards)."""
    first = _next()['engine']
    assert _repair(first)['consent'] == 'asked'
    assert _next()['engine'] == first
    assert consent['asked'] == [f'tts:{first}']


def test_without_nunbas_venv_layer_the_choice_says_so(registry, monkeypatch):
    real_import = __import__

    def blocked(name, *a, **k):
        if name.startswith('tts.'):
            raise ImportError(f'no {name} here')
        return real_import(name, *a, **k)

    monkeypatch.setattr('builtins.__import__', blocked)
    got = _next()
    assert got['engine'] is None
    assert 'bundled' in got['error'].lower()


# ── the declined read never asks ────────────────────────────────────────────

def test_setup_declined_reads_without_asking(consent):
    consent['answers']['tts:x'] = 'no'
    assert cs.setup_declined('tts:x') is True
    assert cs.setup_declined('tts:y') is False
    assert consent['asked'] == []


def test_a_grant_after_a_no_is_not_a_decline(consent, monkeypatch):
    """ConsentService.declined is to be asked only after check_consent
    failed: a newer grant covers an older no."""
    consent['answers']['tts:x'] = 'yes'
    monkeypatch.setattr(
        sys.modules['integrations.social.consent_service'].ConsentService,
        'declined', staticmethod(lambda *a, **k: True))
    assert cs.setup_declined('tts:x') is False


def test_setup_declined_is_false_with_no_owner_or_no_store(consent, monkeypatch):
    consent['answers']['tts:x'] = 'no'
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    assert cs.setup_declined('tts:x') is False
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')

    @contextmanager
    def broken(commit=False):
        raise RuntimeError('db down')
        yield

    monkeypatch.setattr(sys.modules['integrations.social.models'], 'db_session', broken)
    assert cs.setup_declined('tts:x') is False


# ── 2. a node with no owner is told what to do ─────────────────────────────

def test_a_node_with_no_owner_is_told_how_to_repair(
        registry, venv_layer, consent, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')

    got = _repair('a_venv')

    assert got['success'] is False
    assert got['consent'] == 'unavailable'
    assert got['no_owner'] is True
    assert cs.NO_OWNER_REMEDY in got['message']
    assert 'HEVOLVE_OWNER_USER_ID' in got['message']
    assert venv_layer['installs'] == []                  # still nothing installed


def test_an_owner_whose_store_is_down_is_not_told_there_is_no_owner(
        registry, venv_layer, consent, monkeypatch):
    @contextmanager
    def broken(commit=False):
        raise RuntimeError('db down')
        yield

    monkeypatch.setattr(sys.modules['integrations.social.models'], 'db_session', broken)
    got = _repair('a_venv')
    assert got['consent'] == 'unavailable'
    assert got['no_owner'] is False
    assert cs.NO_OWNER_REMEDY not in got['message']


def test_the_owner_variable_has_one_reader(monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '  owner-9 ')
    assert cs.setup_owner() == 'owner-9'
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '   ')
    assert cs.setup_owner() is None
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    assert cs.setup_owner() is None


# ── the goal uses the choice, and the tool is registered beside the repair ──

def test_the_choice_is_registered_beside_the_repair():
    names = [t['name'] for t in brt.BACKEND_REPAIR_TOOLS]
    assert names == ['repair_backend_venv', 'next_tts_venv_to_provision']
    assert all(callable(t['func']) for t in brt.BACKEND_REPAIR_TOOLS)
