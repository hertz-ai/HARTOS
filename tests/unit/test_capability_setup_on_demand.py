"""A task that needs a capability this node does not have offers to set it up.

Owner, 2026-09-21: "on demand task based auto setup via setup card and consent
shd work via canonical design which is already available agentically."

What was missing was only the wire.  The card (ConsentService.check_or_request
-> consent.request -> AgentOverlay) and the work (error_advice's self_heal goal
-> repair_backend_venv -> install_backend_full) both existed and neither knew
about the other, so a voiced turn that found no cloning engine installed fell
to the default voice in silence -- measured on the owner's desktop 2026-09-20,
every voiced turn, with the only routes to an install being an admin button and
a boot-time pre-warm goal that walks every engine regardless of what any task
needs.

These tests drive the real ConsentService against a real consent table and
assert what the owner and the daemon would actually see: one card carrying the
reason, no work before the answer, the work raised once the answer is yes, and
a scope that keeps one capability's yes from covering another's.

    python -m pytest tests/unit/test_capability_setup_on_demand.py -q
"""
import contextlib
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.agent_engine import capability_setup  # noqa: E402
from integrations.agent_engine.capability_setup import (  # noqa: E402
    SETUP_CONSENT_TYPE, offer_voice_clone_setup, request_capability_setup,
)

OWNER = 'owner-1'
CAPABILITY = 'tts:f5_tts'
REASON = 'Speaking in a recorded voice needs a cloning engine. Set it up?'


@pytest.fixture
def consents(monkeypatch):
    """A private consent table, this node's owner, and every ask it sends.

    The same shape as tests/unit/test_computer_control_consent.py: one
    in-memory table this thread alone may touch, so the background services
    another import may start cannot write into the test's rows.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social import models
    from integrations.social.models import UserConsent

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    UserConsent.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    test_thread = threading.get_ident()

    @contextlib.contextmanager
    def _session(commit=True):
        if threading.get_ident() != test_thread:
            raise RuntimeError('consent test table: not the test thread')
        db = factory()
        try:
            yield db
            if commit:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(models, 'db_session', _session)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    asks = []

    def _emit(topic, data, msg_id=None):
        if (topic == 'consent.request'
                and threading.get_ident() == test_thread):
            asks.append(dict(data, _msg_id=msg_id))

    with patch('integrations.social.consent_service._emit', _emit):
        yield asks
    engine.dispose()


@pytest.fixture
def work():
    """A spy where the provisioning goal would be raised."""
    with patch('core.error_advice.handle_exception') as raised:
        yield raised


@contextlib.contextmanager
def _db():
    from integrations.social.models import db_session
    with db_session(commit=True) as db:
        yield db


def _rows():
    from integrations.social.models import UserConsent
    with _db() as db:
        return db.query(UserConsent).all()


def _offer():
    return request_capability_setup(CAPABILITY, reason=REASON,
                                    category='tts.probe',
                                    context={'backend': 'f5_tts'})


# ── the ask ──────────────────────────────────────────────────────────────

def test_the_first_offer_files_one_card_and_does_no_work(consents, work):
    assert _offer() == 'asked'

    assert len(consents) == 1, consents
    ask = consents[0]
    assert ask['consent_type'] == SETUP_CONSENT_TYPE
    assert ask['scope'] == CAPABILITY
    assert ask['reason'] == REASON, 'the card says what is being set up'
    assert ask['user_id'] == OWNER
    work.assert_not_called(), 'nothing may be installed before the answer'
    assert len(_rows()) == 1


def test_offering_on_every_turn_still_shows_one_card(consents, work):
    for _ in range(3):
        assert _offer() == 'asked'

    assert len({a['_msg_id'] for a in consents}) == 1, (
        'a stable msg_id is what collapses the re-asks into one card')
    assert len(_rows()) == 1, 'and one row, not one per turn'
    work.assert_not_called()


def test_without_an_owner_nobody_is_asked_and_nothing_is_written(
        consents, work, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)

    assert _offer() == 'unavailable'
    assert consents == []
    assert _rows() == []
    work.assert_not_called()


def test_a_capability_that_cannot_be_a_scope_is_refused(consents, work):
    assert request_capability_setup('', reason=REASON,
                                    category='tts.probe') == 'unavailable'
    assert request_capability_setup('x' * 101, reason=REASON,
                                    category='tts.probe') == 'unavailable'
    assert consents == []
    work.assert_not_called()


# ── the answer ───────────────────────────────────────────────────────────

def test_a_yes_raises_the_provisioning_work_the_repair_tool_reads(
        consents, work):
    from integrations.social.consent_service import ConsentService
    with _db() as db:
        ConsentService.grant_consent(db, OWNER, SETUP_CONSENT_TYPE,
                                     scope=CAPABILITY)

    assert _offer() == 'provisioning'

    work.assert_called_once()
    _args, kwargs = work.call_args
    assert kwargs['category'] == 'tts.probe', (
        'the category repair_backend_venv documents for a TTS backend')
    assert kwargs['agent_remediation'] is True, 'an agent must pick this up'
    assert kwargs['context']['backend'] == 'f5_tts', (
        'the field the repair tool reads to know which engine to install')
    assert kwargs['context']['capability'] == CAPABILITY


def test_a_yes_to_one_capability_is_not_a_yes_to_another(consents, work):
    from integrations.social.consent_service import ConsentService
    with _db() as db:
        ConsentService.grant_consent(db, OWNER, SETUP_CONSENT_TYPE,
                                     scope='tts:chatterbox_ml')

    assert _offer() == 'asked', (
        'a 12 GB engine already allowed cannot authorise a different install')
    work.assert_not_called()


def test_a_no_stands_and_nothing_is_asked_again(consents, work):
    from integrations.social.consent_service import ConsentService
    assert _offer() == 'asked'
    consents.clear()
    with _db() as db:  # the card's "Don't allow"
        ConsentService.revoke_consent(db, OWNER, SETUP_CONSENT_TYPE,
                                      scope=CAPABILITY)

    assert _offer() == 'declined'

    assert consents == [], 'a no is not re-asked'
    work.assert_not_called()


def test_consent_unreachable_is_not_an_install(consents, work, monkeypatch):
    from integrations.social import models

    @contextlib.contextmanager
    def _broken(commit=True):
        raise RuntimeError('no database here')
        yield  # pragma: no cover

    monkeypatch.setattr(models, 'db_session', _broken)

    assert _offer() == 'unavailable'
    work.assert_not_called()


# ── what the offer names: only an engine that could run here ─────────────

class TestTheGapTheOfferNames:
    """tts_router.clone_engines_not_installed answers "what would have to be
    set up for a voice to be cloned here", so the card never offers an engine
    this machine could not run even after installing it."""

    @pytest.fixture
    def router(self, monkeypatch):
        from integrations.channels.media import tts_router
        monkeypatch.setattr(tts_router, '_engine_available_cache', {})
        monkeypatch.setattr(tts_router, '_get_gpu_info',
                            lambda: {'cuda_available': True, 'free_gb': 3.2})
        return tts_router

    def test_it_lists_the_missing_cloners_in_ladder_order(self, router,
                                                          monkeypatch):
        monkeypatch.setattr(router, '_is_engine_installed', lambda e: False)
        monkeypatch.setattr(router, '_can_fit_on_gpu', lambda e: True)

        missing = router.clone_engines_not_installed('en')

        assert missing, 'nothing is installed, so the ladder is all gap'
        assert 'f5_tts' in missing
        ladder = router.LANG_ENGINE_PREFERENCE['en']
        assert missing == sorted(missing, key=ladder.index), 'ladder order'
        assert all(router.ENGINE_REGISTRY[e].voice_clone for e in missing)
        assert len(missing) == len(set(missing))

    def test_an_installed_engine_is_not_a_gap(self, router, monkeypatch):
        monkeypatch.setattr(router, '_can_fit_on_gpu', lambda e: True)
        monkeypatch.setattr(router, '_is_engine_installed',
                            lambda e: e == 'f5_tts')

        assert 'f5_tts' not in router.clone_engines_not_installed('en')

    def test_a_gpu_only_engine_that_cannot_fit_this_card_is_never_offered(
            self, router, monkeypatch):
        """Measured on the owner's desktop 2026-09-21 with the LLM resident
        (1.84 GB free of 8): every GPU-only cloner in the English ladder --
        chatterbox_turbo 3.8, omnivoice 3.0, xtts_v2 1.8, f5_tts 1.3 -- was
        refused by the fit gate, and the gap was ['neutts_air'], the one
        cloner that runs on the CPU when it has to.  An offer that cannot
        run after installing is worse than no offer."""
        monkeypatch.setattr(router, '_is_engine_installed', lambda e: False)
        monkeypatch.setattr(router, '_can_fit_on_gpu',
                            lambda e: e == 'f5_tts')

        missing = router.clone_engines_not_installed('en')

        assert 'f5_tts' in missing, 'the GPU-only engine that does fit'
        for starved in ('chatterbox_turbo', 'omnivoice', 'xtts_v2'):
            assert starved not in missing, (
                f'{starved} is GPU-only and does not fit this card')
        assert missing[0] == 'neutts_air', (
            'ladder order decides, and a CPU-capable cloner is never gated '
            'on VRAM it does not need')

    def test_with_no_gpu_no_gpu_only_engine_is_offered(self, router,
                                                       monkeypatch):
        monkeypatch.setattr(router, '_get_gpu_info',
                            lambda: {'cuda_available': False})
        monkeypatch.setattr(router, '_is_engine_installed', lambda e: False)
        monkeypatch.setattr(router, '_can_fit_on_gpu', lambda e: False)

        from integrations.channels.media.tts_router import TTSDevice
        for engine_id in router.clone_engines_not_installed('en'):
            assert router.ENGINE_REGISTRY[engine_id].device != TTSDevice.GPU_ONLY

    def test_a_cloud_engine_is_not_a_local_setup(self, router, monkeypatch):
        monkeypatch.setattr(router, '_is_engine_installed', lambda e: False)
        monkeypatch.setattr(router, '_can_fit_on_gpu', lambda e: True)

        from integrations.channels.media.tts_router import TTSDevice
        for engine_id in router.clone_engines_not_installed('en'):
            assert router.ENGINE_REGISTRY[engine_id].device != TTSDevice.CLOUD


# ── the voiced turn's own offer ──────────────────────────────────────────

def test_no_offer_when_a_cloner_is_installed(consents, work, monkeypatch):
    """Then the turn failed for some other reason and an offer would be a
    lie."""
    monkeypatch.setattr(capability_setup, 'request_capability_setup',
                        MagicMock(side_effect=AssertionError('asked anyway')))
    with patch('integrations.channels.media.tts_router.'
               'clone_engines_not_installed', return_value=[]):
        assert offer_voice_clone_setup('en') == 'unavailable'

    assert consents == []
    work.assert_not_called()


def test_source_guard_the_voiced_turn_offers_on_both_of_its_silent_exits():
    """The trigger lives in hart_intelligence_entry._speak_in_voice, which no
    unit test can import (13k lines, LangChain + autogen at module scope --
    the same reason test_capability_consent_canonical.py reads the source).
    The behaviour above is tested for real; this only pins that the two paths
    where a clone was wanted and not delivered both reach it, since a missing
    call is exactly the silence this change exists to end.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / 'hart_intelligence_entry.py'
    tree = ast.parse(src.read_text(encoding='utf-8'))
    functions = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}

    assert '_offer_voice_clone_setup' in functions, (
        'the offer helper is gone; a voiced turn would fail in silence again')
    speak = functions.get('_speak_in_voice')
    assert speak is not None
    calls = [n for n in ast.walk(speak)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == '_offer_voice_clone_setup']
    assert len(calls) == 2, (
        f'_speak_in_voice has {len(calls)} offer call(s): both the '
        f'no-audio exit and the cannot-clone exit must offer the setup')


def test_the_offer_names_the_best_missing_engine_and_its_backend(consents,
                                                                 work):
    with patch('integrations.channels.media.tts_router.'
               'clone_engines_not_installed', return_value=['f5_tts',
                                                            'xtts_v2']):
        assert offer_voice_clone_setup('en') == 'asked'

    assert len(consents) == 1
    ask = consents[0]
    assert ask['scope'] == 'tts:f5_tts', 'the first engine that fits'
    assert 'recorded voice' in ask['reason']
    assert 'default voice' in ask['reason'], (
        'the card says what the owner is hearing meanwhile')
