"""A plan run that saved nothing leaves no cached writer and no review mode.

Follow-up to 38c753ea (goal 5ba50f98, central 2026-11-20: the plan writer's
model answered 429 from 12:14:50Z to 12:15:00Z).  That run now saves nothing
and answers "nothing was built".  Its review (F1, measured) found two traces
left behind:

  F1  gather_info caches its writer per user and prompt
      (gather_agentdetails.user_agents).  The retry reused it.  After 4
      failed gathers for system_daemon_89682852097, with attempts 3 and 4 on
      the expert, the writer stayed on ['local'] and its history grew from 1
      to 4 copies of the prompt.  A failed run now drops its writer, so the
      retry builds a fresh one on its own turn's model.
  F2  the four /chat sites that run _autonomous_gather_info still set review
      mode and answered Agent_status='Review Mode' beside "nothing was
      built".  They now share _autonomous_creation_status: a run that saved
      nothing stays in 'Creation Mode' and sets no review flag.

F1 drives the real _autonomous_gather_info, the real gather_info and the
real autogen writer; only the model call (OpenAIWrapper.create) is replaced,
and it fails like central's 429.  F2 drives the real helper, and the real
`if autonomous:` block of /chat with it.
"""
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')

EXPERT = {'model': 'claude-haiku-5-5', 'api_key': 'k-test',
          'base_url': 'https://api.anthropic.com/v1'}
USER, PID = 'system_daemon', 89682852097
BRIEF = 'Rice order desk: find importers and draft the first outreach'
# What a run that saved nothing answers (38c753ea).
NOTHING_BUILT = ("the planner's model did not answer; nothing was built, so ask "
                 "again to re-plan")


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


def test_a_retry_after_a_failed_run_builds_its_writer_on_its_own_model(
        hie, tmp_path, monkeypatch):
    """FAILS ON 38c753ea: the third run reused the writer built on the
    node's model, with the two failed prompts still in its history."""
    import autogen
    from core.agent_tools import is_user_facing_error
    from hartos import gather_agentdetails
    from hartos.threadlocal import thread_local_data
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(hie, '_held_plan_turn', lambda *a, **k: None)
    # The writer's "thinking" lines go to the UI bus; none listens here.
    from hartos import create_recipe
    monkeypatch.setattr(create_recipe, '_push_thinking', lambda *a, **k: None)
    gather_agentdetails.user_agents.pop(f'{USER}_{PID}', None)
    calls = []

    def create(self, **config):
        calls.append(([c.get('model') for c in self._config_list],
                      sum(1 for m in config['messages']
                          if m.get('role') == 'user' and BRIEF in str(m.get('content')))))
        raise RuntimeError('Error code: 429 - Rate limit exceeded: free-models-per-day')

    try:
        with patch.object(autogen.OpenAIWrapper, 'create', create), \
                hie.app.app_context():
            for attempt in range(3):
                thread_local_data.set_model_config_override(
                    [dict(EXPERT)] if attempt == 2 else None)
                reply = hie._autonomous_gather_info(USER, BRIEF, PID)
                assert is_user_facing_error(reply), reply
    finally:
        thread_local_data.clear_model_config_override()
        gather_agentdetails.user_agents.pop(f'{USER}_{PID}', None)

    assert len(calls) == 3, calls
    models, copies = calls[2]
    assert models == ['claude-haiku-5-5'], 'the retry stayed on the old writer'
    assert copies == 1, 'the retry carried the failed prompts in its history'
    assert not (tmp_path / f'{PID}.json').exists()


@pytest.fixture
def flags(hie, monkeypatch):
    """The real helper's state: its review flags and its lifecycle records."""
    recorded = []
    monkeypatch.setattr(hie, 'review_agents', {})
    monkeypatch.setattr(hie, '_touch_agent_timestamp', lambda *a: None)
    monkeypatch.setattr(hie, '_record_lifecycle',
                        lambda status, *a, **k: recorded.append(status))
    return recorded


def test_a_run_that_saved_nothing_is_not_put_in_review(hie, flags):
    """FAILS ON 38c753ea (no shared helper)."""
    from core.agent_tools import user_facing_error
    nothing = user_facing_error(RuntimeError(NOTHING_BUILT))
    status = hie._autonomous_creation_status(USER, PID, nothing, 'daemon create')
    assert status == 'Creation Mode'
    assert hie.review_agents == {}
    assert 'Review Mode' not in flags


def test_a_run_that_saved_or_held_a_plan_is_put_in_review_as_before(hie, flags):
    status = hie._autonomous_creation_status(
        USER, PID, 'Agent details gathered autonomously. Moving to review.',
        'daemon create')
    assert status == 'Review Mode'
    assert hie.review_agents == {f'{USER}_{PID}': True}
    assert flags == ['Review Mode']


def test_the_daemons_create_that_saved_nothing_answers_no_review(hie, flags, tmp_path):
    """The real `if autonomous:` block of /chat, with the real helper: the
    gather saved nothing, so no recipe runs, the failed-turn sentence is the
    reply, and it carries no 'Review Mode' (FAILS ON 38c753ea)."""
    from core.agent_tools import is_user_facing_error, user_facing_error
    from tests.unit import test_yield_pauses_every_agent as harness
    nothing = user_facing_error(RuntimeError(NOTHING_BUILT))
    ran = []
    ns = harness._first_dispatch_ns(
        tmp_path, lambda *a, **k: ran.append(a) or 'Agent Created Successfully',
        gather_reply=nothing, write_config=False)
    ns['review_agents'] = hie.review_agents
    # getattr: on a build without the helper the block never reads it.
    ns['_autonomous_creation_status'] = getattr(hie, '_autonomous_creation_status', None)
    text, payload = harness._run_first_dispatch(ns)
    assert ran == [], 'a recipe ran with no plan saved'
    assert is_user_facing_error(text) and text == nothing
    assert payload['Agent_status'] != 'Review Mode'
    assert hie.review_agents.get('u1_777') is not True
    assert 'Review Mode' not in flags
