"""An agent asks the owner before it acts on this computer.

Live 2026-09-14 the VLM loop wrote C:\\Users\\Public\\search_llm_config.py
(write_file, 18:33:41) and ran it (shell, 18:33:45) for agent 88659566083,
and nothing asked the person at the desk first.  The action path
(run_local_agentic_loop -> execute_action(safety=True) -> _check_safety)
checks the rate, the window blocklist, placeholder credentials and
destructive closes; none of them asks anyone.  The LangChain Shell_Command
tool (hart_intelligence_entry._handle_shell_command_tool) had only its
denylist in front of the shell.

The permission belongs to the desktop owner: the machine is theirs, and an
agent's creator can be someone else (this node's agents have 43 creators).
It is one consent type, computer_control, kept by ConsentService like every
other consent, so the privacy page lists it and a revoke ends it.

A grant covers every agent for now.  user_consents has
UNIQUE(user_id, agent_id, consent_type, scope), which rejects a per-agent
grant once a per-agent ask exists; the migration that lifts it is a
follow-up.  The ask still names the agent that asked.

    python -m pytest tests/unit/test_computer_control_consent.py -q
"""
import contextlib
import os
import sys
import threading
import time
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OWNER = 'owner-1'
CREATOR = 'creator-7'
AGENT = '88659566083'


# ── a real consent table, the owner, and a record of every ask ───────────

@pytest.fixture
def consents(monkeypatch):
    """The gate's db_session, on a private in-memory consent table.

    Yields the list of asks (consent.request events) the gate sent."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social import models
    from integrations.social.models import UserConsent

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    UserConsent.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    # Only this test's thread may use its consent table, and only its asks
    # are recorded.  Importing hart_intelligence_entry (the shell tests)
    # starts background services that call db_session and _emit too: live
    # run 2026-09-14, VisionService's screen-capture loop filed an ask here
    # mid-test.  On this one shared connection their writes and rollbacks
    # would also mix with the test's.
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
            asks.append(dict(data))

    with patch('integrations.social.consent_service._emit', _emit):
        yield asks
    engine.dispose()


@pytest.fixture
def background():
    """A daemon dispatch: request_id carries the daemon_ tag."""
    from hartos.threadlocal import thread_local_data
    prior = thread_local_data.get_request_id()
    thread_local_data.set_request_id('daemon_goal-1')
    yield
    thread_local_data.set_request_id(prior)


@pytest.fixture
def foreground(monkeypatch):
    """A chat turn someone is watching, with a short wait for the answer."""
    from hartos.threadlocal import thread_local_data
    from integrations.vlm import safety
    prior = thread_local_data.get_request_id()
    thread_local_data.set_request_id('1789049953')
    monkeypatch.setattr(safety, 'COMPUTER_CONTROL_WAIT_SECONDS', 0.4)
    monkeypatch.setattr(safety, 'COMPUTER_CONTROL_POLL_SECONDS', 0.05)
    yield
    thread_local_data.set_request_id(prior)


def _grant(**kw):
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    with models.db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'computer_control', **kw)


def _revoke(**kw):
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    with models.db_session() as db:
        ConsentService.revoke_consent(db, OWNER, 'computer_control', **kw)


def _block(agent_id=AGENT, **kw):
    from integrations.vlm.safety import computer_control_block
    return computer_control_block(agent_id, **kw)


# ── the consent type ─────────────────────────────────────────────────────

def test_computer_control_is_a_consent_type():
    from integrations.social.consent_service import CONSENT_TYPES
    assert 'computer_control' in CONSENT_TYPES


def test_check_or_request_asks_once_and_then_passes_on_a_grant(consents):
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    with models.db_session() as db:
        assert ConsentService.check_or_request(
            db, OWNER, 'screen_capture', reason='why') is False
    assert [a['consent_type'] for a in consents] == ['screen_capture']
    assert consents[0]['reason'] == 'why'
    with models.db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'screen_capture')
    consents.clear()
    with models.db_session() as db:
        assert ConsentService.check_or_request(
            db, OWNER, 'screen_capture') is True
    assert consents == [], 'a granted consent was asked for again'


# ── the gate ─────────────────────────────────────────────────────────────

def test_no_grant_in_the_background_refuses_at_once_and_asks_the_owner(
        consents, background):
    started = time.monotonic()
    refusal = _block()
    assert refusal, 'an agent was let act on the machine with no grant'
    assert time.monotonic() - started < 1.0, 'a background run waited'
    assert len(consents) == 1
    ask = consents[0]
    assert ask['user_id'] == OWNER
    assert ask['consent_type'] == 'computer_control'
    assert ask['agent_id'] == AGENT


def test_the_ask_says_what_a_grant_allows(consents, background):
    from integrations.vlm.safety import COMPUTER_CONTROL_COVERS
    _block()
    reason = consents[0]['reason']
    assert COMPUTER_CONTROL_COVERS in reason
    for word in ('shell commands', 'files', 'mouse', 'keyboard', 'apps'):
        assert word in COMPUTER_CONTROL_COVERS, word


def test_a_grant_lets_the_agent_act(consents, background):
    _grant()
    assert _block() is None
    assert consents == [], 'the owner was asked again after granting'


def test_a_revoke_stops_the_agent_again(consents, background):
    _grant()
    assert _block() is None
    _revoke()
    assert _block(), 'a revoked grant still let the agent act'


@pytest.mark.parametrize('unknown', [None, '', 0, '0'])
def test_an_unknown_agent_is_asked_for_as_any_agent(consents, background,
                                                    unknown):
    """Never guess an agent: the ask and the grant it leads to are blanket."""
    assert _block(unknown)
    assert consents[0]['agent_id'] is None
    assert 'could not be identified' in consents[0]['reason']


def test_no_owner_means_no(consents, background, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    refusal = _block()
    assert refusal and 'signed in' in refusal
    assert consents == [], 'an ask was sent with nobody to answer it'


def test_a_watched_turn_waits_for_the_owners_answer(consents, foreground):
    def _owner_answers(_seconds):
        _grant()
    assert _block(sleep=_owner_answers) is None


def test_a_watched_turn_stops_waiting_after_the_window(consents, foreground):
    started = time.monotonic()
    refusal = _block()
    waited = time.monotonic() - started
    assert refusal
    assert 0.35 <= waited < 3.0, f'waited {waited:.2f}s for a 0.4s window'
    assert len({a['agent_id'] for a in consents}) == 1


def test_a_watched_wait_logs_once_not_per_look(consents, foreground, caplog):
    """A 90s wait looks ~30 times; each denied look used to log a WARNING
    (check_consent), burying real warnings.  One line starts the wait and
    one refusal ends it."""
    import logging
    caplog.set_level(logging.DEBUG)
    assert _block()
    denials = [r for r in caplog.records
               if r.name == 'hevolve.consent' and r.levelno >= logging.WARNING]
    waits = [r for r in caplog.records
             if r.name == 'hevolve.vlm.safety' and 'waiting up to' in r.getMessage()]
    refusals = [r for r in caplog.records
                if r.name == 'hevolve.vlm.safety' and r.levelno == logging.WARNING]
    assert denials == [], f'{len(denials)} denial warnings for one wait'
    assert len(waits) == 1, [r.getMessage() for r in waits]
    assert len(refusals) == 1, [r.getMessage() for r in refusals]


def test_a_failed_check_is_a_no(consents, background, monkeypatch):
    from integrations.social import models

    def _broken(commit=True):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(models, 'db_session', _broken)
    refusal = _block()
    assert refusal and 'could not be checked' in refusal


# ── the owner's no ───────────────────────────────────────────────────────
#
# The card's "Don't allow" (consent_api.decline_consent) is revoke_consent
# on the ask: with no active grant it marks the ask declined.  A no stands
# until the owner allows agents again (hartos-3e ruling (a)).

def _decline(agent_id=AGENT):
    _revoke(agent_id=agent_id)


def test_declined_is_true_only_after_the_owner_says_no(consents, background):
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    _block()
    with models.db_session() as db:
        assert ConsentService.declined(
            db, OWNER, 'computer_control', agent_id=AGENT) is False
    _decline()
    with models.db_session() as db:
        assert ConsentService.declined(
            db, OWNER, 'computer_control', agent_id=AGENT) is True
        assert ConsentService.declined(
            db, OWNER, 'computer_control', agent_id='other-agent') is False


@pytest.fixture
def long_window(foreground, monkeypatch):
    """A watched turn whose window cannot run out during one look: these
    tests count the gate's sleeps (injected, so nothing really sleeps)
    instead of timing it, so a slow look cannot end the wait early."""
    from integrations.vlm import safety
    monkeypatch.setattr(safety, 'COMPUTER_CONTROL_WAIT_SECONDS', 60.0)


def _ask_filed():
    """The pending ask, as the gate files it on its first look."""
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    with models.db_session() as db:
        ConsentService.request_consent(db, OWNER, 'computer_control',
                                       agent_id=AGENT, reason='asks')


def test_a_no_during_the_wait_refuses_at_once(consents, long_window):
    sleeps = []

    def _owner_says_no(_seconds):
        sleeps.append(1)
        _decline()

    refusal = _block(sleep=_owner_says_no)
    assert refusal and 'said no' in refusal, refusal
    assert len(sleeps) == 1, (
        f'the gate kept waiting after the owner said no ({len(sleeps)} sleeps)')


def test_after_a_no_the_agent_is_refused_at_once_and_not_asked_again(
        consents, long_window):
    _ask_filed()
    _decline()
    consents.clear()
    sleeps = []
    refusal = _block(sleep=sleeps.append)
    assert refusal and 'said no' in refusal, refusal
    assert sleeps == [], f'a declined agent waited ({len(sleeps)} sleeps)'
    assert consents == [], 'the owner was asked again after saying no'


def test_allowing_all_agents_after_a_no_lets_the_agent_act(consents,
                                                          background):
    _block()
    _decline()
    assert _block()
    _grant()  # the privacy page's "Allow ALL agents to control this computer"
    assert _block() is None


def test_a_no_to_one_agent_leaves_other_agents_asking(consents, background):
    _block()
    _decline()
    consents.clear()
    refusal = _block('other-agent')
    assert refusal and 'said no' not in refusal, refusal
    assert [a['agent_id'] for a in consents] == ['other-agent']


# ── the VLM loop ─────────────────────────────────────────────────────────

class _OneShellCommand:
    """Stands in for Qwen3VLBackend; asks for one shell command per step."""

    def route_task(self, _instruction):
        return 'single_shot'

    def try_taskbar_pre_check(self, *_a, **_k):
        return None

    def detect_grounding_bias(self, *_a, **_k):
        return None

    def _call_api(self, _messages):
        return ('{"Reasoning": "check the disk", "Next Action": "shell", '
                '"command": "Get-PSDrive C", "Status": "IN_PROGRESS"}')


@pytest.fixture
def loop(monkeypatch):
    """The real loop and executor; the screen, the VLM and the machine are
    replaced.  Yields (local_loop, ran, prompt_ids_seen, ribbon_calls)."""
    lct = pytest.importorskip('integrations.vlm.local_computer_tool')
    from integrations.vlm import local_loop, qwen3vl_backend, safety
    from hartos.threadlocal import thread_local_data
    import core.config_cache as cc
    import core.http_pool as hp

    ran, seen, ribbon = [], [], []

    def _act(action):
        ran.append(action)
        seen.append(thread_local_data.get_prompt_id())
        return {'output': 'Free 24.2 GB'}

    monkeypatch.setattr(qwen3vl_backend, 'get_qwen3vl_backend',
                        lambda: _OneShellCommand(), raising=False)
    monkeypatch.setattr(lct, 'take_screenshot', lambda _tier: 'ZmFrZQ==',
                        raising=False)
    monkeypatch.setattr(lct, '_execute_inprocess', _act)
    monkeypatch.setattr(lct, '_emit_audit', lambda *a, **k: None)
    monkeypatch.setattr(lct, '_check_reasoning_mismatch', lambda action: None)
    monkeypatch.setattr(cc, 'is_bundled', lambda: True)
    monkeypatch.setattr(hp, 'pooled_get',
                        lambda url, **kw: ribbon.append(url.rsplit('/', 1)[-1]))
    monkeypatch.setenv('HEVOLVE_VLM_UNIFIED', '1')
    monkeypatch.setenv('HEVOLVE_VLM_LOOP_SAFETY', '1')
    monkeypatch.setenv('HEVOLVE_VLM_LOOP_VERIFY', '0')
    safety.get_session_guard().reset()
    yield local_loop, ran, seen, ribbon
    safety.get_session_guard().reset()


def _run(local_loop, prompt_id=AGENT):
    return local_loop.run_local_agentic_loop(
        {'instruction_to_vlm_agent': 'report the free disk space',
         'enhanced_instruction': 'report the free disk space',
         'user_id': CREATOR, 'prompt_id': prompt_id,
         'max_ETA_in_seconds': 60},
        tier='inprocess')


def test_the_loop_does_not_act_without_the_owners_permission(
        consents, background, loop):
    local_loop, ran, _seen, ribbon = loop
    out = _run(local_loop)
    assert not ran, f'the loop acted on the machine unasked: {ran}'
    assert out['exit_reason'] == 'consent_required', out
    assert out['status'] == 'incomplete'
    assert 'show' not in ribbon, 'the AI-control ribbon went up for a run that never acted'
    assert consents and consents[0]['user_id'] == OWNER, (
        "the ask went to the agent's creator, not the machine's owner")
    assert (CREATOR, AGENT) not in local_loop.list_active_sessions()


def test_the_loop_acts_once_the_owner_allows(consents, background, loop):
    local_loop, ran, _seen, _ribbon = loop
    _grant()
    _run(local_loop)
    assert ran, 'a granted run did not act'


def test_the_run_is_the_threads_agent_while_it_acts(consents, background,
                                                   loop):
    """The shell action inside a run checks this thread's prompt_id, so it
    must be the run's agent, and the caller's must come back after."""
    from hartos.threadlocal import thread_local_data
    local_loop, ran, seen, _ribbon = loop
    _grant()
    thread_local_data.set_prompt_id('caller-agent')
    try:
        _run(local_loop)
        assert ran and set(seen) == {AGENT}, seen
        assert thread_local_data.get_prompt_id() == 'caller-agent'
    finally:
        thread_local_data.set_prompt_id(None)


def test_the_callers_agent_comes_back_when_the_run_raises(
        consents, background, loop, monkeypatch):
    from hartos.threadlocal import thread_local_data
    local_loop, _ran, _seen, _ribbon = loop
    _grant()

    def _boom(*_a, **_k):
        raise RuntimeError('loop body failed')

    monkeypatch.setattr(local_loop, '_drive_local_agentic_loop', _boom)
    thread_local_data.set_prompt_id('caller-agent')
    try:
        with pytest.raises(RuntimeError):
            _run(local_loop)
        assert thread_local_data.get_prompt_id() == 'caller-agent'
    finally:
        thread_local_data.set_prompt_id(None)


def test_the_callers_report_names_the_missing_permission(consents,
                                                         background, loop):
    from integrations.vlm import response_view
    local_loop, _ran, _seen, _ribbon = loop
    out = _run(local_loop)
    summary = response_view.outcome_summary(out)
    assert 'consent_required' not in summary, summary
    assert 'allowed' in summary, summary


# ── the shell tool ───────────────────────────────────────────────────────

try:
    from core.subprocess_safe import BoundedResult
    import hart_intelligence_entry as hie  # noqa: TID251 -- the tool under test lives here
    _has_hie = True
except Exception:
    _has_hie = False

needs_hie = pytest.mark.skipif(
    not _has_hie, reason='hart_intelligence_entry import failed')


@needs_hie
def test_the_shell_tool_asks_before_running(consents, background):
    with patch.object(hie, 'run_bounded') as run:
        out = hie._handle_shell_command_tool('echo hi')
    run.assert_not_called()
    assert out.startswith('Shell_Command not run'), out
    assert consents and consents[0]['consent_type'] == 'computer_control'


@needs_hie
def test_the_shell_tool_runs_once_the_owner_allows(consents, background):
    _grant()
    with patch.object(hie, 'run_bounded') as run:
        run.return_value = BoundedResult(returncode=0, stdout='hi',
                                         stderr='', timed_out=False)
        out = hie._handle_shell_command_tool('echo hi')
    run.assert_called_once()
    assert out.startswith('Exit code: 0'), out


@needs_hie
def test_a_destructive_command_is_refused_without_asking(consents,
                                                         background):
    with patch.object(hie, 'run_bounded') as run:
        out = hie._handle_shell_command_tool('rm -rf /')
    run.assert_not_called()
    assert 'destructive pattern' in out
    assert consents == [], 'the owner was asked to allow a blocked command'


@needs_hie
def test_computer_action_reports_the_missing_permission(consents,
                                                        background, loop):
    """Computer_Action's answer to the model says what is missing instead of
    offering another route that needs the same permission."""
    out = hie._handle_computer_action_tool('open the settings')
    assert 'different approach' not in out, out
    assert 'allow' in out.lower(), out
