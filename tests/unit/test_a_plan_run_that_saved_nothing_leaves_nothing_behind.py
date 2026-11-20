"""A plan run that saved nothing leaves no failed history and no review mode.

Follow-up to 38c753ea (goal 5ba50f98, central 2026-11-20: the plan writer's
model answered 429 from 12:14:50Z to 12:15:00Z).  That run now saves nothing
and answers "nothing was built".  Its review found two traces left behind:

  F1  gather_info caches its writer per user and prompt
      (gather_agentdetails.user_agents), and the retry reused it.  After 4
      failed gathers for system_daemon_89682852097, with attempts 3 and 4 on
      the expert, the writer stayed on ['local'] and its history grew from 1
      to 4 copies of the prompt (measured).
  F2  the four /chat sites that run _autonomous_gather_info still set review
      mode and answered Agent_status='Review Mode' beside "nothing was
      built".  They now share _autonomous_creation_status: a run that saved
      nothing stays in 'Creation Mode' and sets no review flag.

30fb3b75 fixed F1 by dropping the writer and was rejected.  Its reviewer
measured this: Nunba's chat page answers any reply that is not "completed"
with "Yes, proceed with the next step".  A fresh writer then planned that
sentence, and the task appeared nowhere in its messages.  A person's own
writer on the same key also lost its conversation.

Now the writer is kept.  A send whose model call fails leaves the history as
it was, and a cached writer runs on its turn's model
(hartos.helper.bind_agents_to_turn_model).

F1 drives the real _autonomous_gather_info, the real gather_info and the
real autogen writer.  Only the model call (OpenAIWrapper.create) is
replaced, and it fails like central's 429.  F2 drives the real helper, and
the real `if autonomous:` block of /chat with it.
"""
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')

EXPERT = {'model': 'claude-haiku-5-5', 'api_key': 'k-test',
          'base_url': 'https://api.anthropic.com/v1'}
USER, PID = 'system_daemon', 89682852097
BRIEF = 'Rice order desk: find importers and draft the first outreach'
# What Nunba's chat page sends after any reply that is not "completed".
PROCEED = 'Yes, proceed with the next step'
# What a run that saved nothing answers (38c753ea).
NOTHING_BUILT = ("the planner's model did not answer; nothing was built, so ask "
                 "again to re-plan")


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def writer(hie, tmp_path, monkeypatch):
    """Runs _autonomous_gather_info turns whose model call always fails, and
    records what each call was sent: (models, messages)."""
    import autogen
    from core.agent_tools import is_user_facing_error
    from hartos import create_recipe, gather_agentdetails
    from hartos.threadlocal import thread_local_data
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(hie, '_held_plan_turn', lambda *a, **k: None)
    # The writer's "thinking" lines go to the UI bus; none listens here.
    monkeypatch.setattr(create_recipe, '_push_thinking', lambda *a, **k: None)
    key = f'{USER}_{PID}'
    gather_agentdetails.user_agents.pop(key, None)
    state = SimpleNamespace(calls=[], key=key, agents=gather_agentdetails.user_agents,
                            saved=tmp_path / f'{PID}.json')

    def create(self, **config):
        state.calls.append(([c.get('model') for c in self._config_list],
                            [dict(m) for m in config['messages']]))
        raise RuntimeError('Error code: 429 - Rate limit exceeded: free-models-per-day')
    monkeypatch.setattr(autogen.OpenAIWrapper, 'create', create)

    def run(message, model=None):
        thread_local_data.set_model_config_override([dict(model)] if model else None)
        try:
            with hie.app.app_context():
                reply = hie._autonomous_gather_info(USER, message, PID)
        finally:
            thread_local_data.clear_model_config_override()
        assert is_user_facing_error(reply), reply
    state.run = run
    yield state
    gather_agentdetails.user_agents.pop(key, None)


def _copies(messages, text, role=None):
    """How many of ``messages`` (of ``role``, when given) hold ``text``."""
    return sum(1 for m in messages
               if (role is None or m.get('role') == role) and text in str(m.get('content')))


def test_a_retry_after_failed_runs_runs_on_its_own_model_with_one_copy(writer):
    """F1 (FAILS ON 38c753ea): the third run reused the writer built on the
    node's model, with the two failed prompts still in its history."""
    for model in (None, None, EXPERT):
        writer.run(BRIEF, model)
    assert len(writer.calls) == 3, writer.calls
    models, messages = writer.calls[2]
    assert models == ['claude-haiku-5-5'], 'the retry stayed on the old model'
    assert _copies(messages, BRIEF, 'user') == 1, (
        'the retry carried the failed prompts in its history')
    assert not writer.saved.exists()


def test_the_pages_proceed_after_failed_runs_still_plans_the_task(writer):
    """FAILS ON 30fb3b75: the writer was dropped, so a fresh writer that never
    saw the task planned the page's "Yes, proceed with the next step"."""
    writer.run(BRIEF)
    writer.run(BRIEF)
    writer.run(PROCEED, EXPERT)
    models, messages = writer.calls[-1]
    assert models == ['claude-haiku-5-5']
    assert _copies(messages, BRIEF) == 1, (
        'the writer does not hold the original task exactly once')
    assert _copies(messages, PROCEED, 'user') == 1


def test_a_persons_own_writer_keeps_its_conversation(writer):
    """FAILS ON 30fb3b75: a failed autonomous run on the same user and prompt
    dropped the person's own writer and its conversation."""
    from hartos.gather_agentdetails import create_agents_for_user
    assistant, proxy = create_agents_for_user(USER)
    writer.agents[writer.key] = (assistant, proxy)
    proxy.send('I want an agent that finds rice importers', assistant,
               request_reply=False, silent=True)
    assistant.send('{"status": "pending", "question": "Which countries?"}', proxy,
                   request_reply=False, silent=True)
    before = [dict(m) for m in assistant.chat_messages[proxy]]
    writer.run(BRIEF)
    kept = writer.agents.get(writer.key)
    assert kept is not None and kept[0] is assistant and kept[1] is proxy, (
        "the person's writer was dropped")
    assert [dict(m) for m in assistant.chat_messages[proxy]] == before, (
        'the failed run left its message in the conversation')
    assert _copies(writer.calls[-1][1], 'finds rice importers', 'user') == 1


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
