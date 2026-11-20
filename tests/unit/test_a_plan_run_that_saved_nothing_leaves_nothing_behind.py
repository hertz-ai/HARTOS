"""A plan run that saved nothing loses nothing and enters no review mode.

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

Two attempts at F1 were rejected; their reviewers measured each fault:
  * 30fb3b75 dropped the writer.  Nunba's chat page answers any reply that
    is not "completed" with "Yes, proceed with the next step", and a fresh
    writer planned that sentence with the task nowhere in its messages.  A
    person's own writer on the same key lost its conversation.
  * 86e5185d took a failed send back whole.  A person's writer holds their
    request only in its history, so after a 429 on it, "try again" reached
    the model alone.  A concurrent turn on another model also switched the
    writer under a running gather, and a failed "proper format" re-ask was
    left in both histories.

Now a failed send keeps the message it carried, once, unanswered, and takes
back everything after it.  The next send of the same message does not add a
second copy.  The writer is picked up through helper.group_turn, so a turn
whose model differs from a running one's is held
(held_for_another_turn_reply), not switched.  A failed re-ask is taken back
whole.

These drive the real gather_info (and _autonomous_gather_info) with the real
autogen writer.  Only the model call is replaced (OpenAIWrapper.create,
scripted per call), with autogen's text extraction reading the scripted
text.  F2 drives the real helper, and the real `if autonomous:` block of
/chat with it.
"""
import os
import tempfile
import threading
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
REQUEST = 'build me an agent that finds rice importers'
TRY_AGAIN = 'try again'
# What a run that saved nothing answers (38c753ea).
NOTHING_BUILT = ("the planner's model did not answer; nothing was built, so ask "
                 "again to re-plan")
LIMITED = RuntimeError('Error code: 429 - Rate limit exceeded: free-models-per-day')


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def writer(hie, tmp_path, monkeypatch):
    """The real writer with a scripted model.  Each model call records
    (models, messages) and takes the next entry of ``script``: an exception
    is raised, text is the reply, a callable runs and then fails the call.
    With the script used up, every call fails like central's 429."""
    import autogen
    from hartos import create_recipe, gather_agentdetails
    from hartos.threadlocal import thread_local_data
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(hie, '_held_plan_turn', lambda *a, **k: None)
    # The writer's "thinking" lines go to the UI bus; none listens here.
    monkeypatch.setattr(create_recipe, '_push_thinking', lambda *a, **k: None)
    key = f'{USER}_{PID}'
    gather_agentdetails.user_agents.pop(key, None)
    state = SimpleNamespace(calls=[], script=[], key=key,
                            agents=gather_agentdetails.user_agents,
                            saved=tmp_path / f'{PID}.json')

    def create(self, **config):
        state.calls.append(([c.get('model') for c in self._config_list],
                            [dict(m) for m in config['messages']]))
        step = state.script.pop(0) if state.script else LIMITED
        if callable(step):
            step()
            raise LIMITED
        if isinstance(step, BaseException):
            raise step
        return step
    monkeypatch.setattr(autogen.OpenAIWrapper, 'create', create)
    monkeypatch.setattr(autogen.OpenAIWrapper, 'extract_text_or_completion_object',
                        classmethod(lambda cls, response: [response]))

    def on_model(model):
        thread_local_data.set_model_config_override([dict(model)] if model else None)

    def run(message, model=None):
        """One _autonomous_gather_info turn (a daemon's, or a forced create)."""
        on_model(model)
        try:
            with hie.app.app_context():
                return hie._autonomous_gather_info(USER, message, PID)
        finally:
            thread_local_data.clear_model_config_override()

    def gather(message, model=None):
        """One turn of a person's own writer (gather_info, not autonomous)."""
        from hartos.gather_agentdetails import gather_info
        on_model(model)
        try:
            with hie.app.app_context():
                return gather_info(USER, message, PID)
        finally:
            thread_local_data.clear_model_config_override()
    state.run, state.gather = run, gather
    yield state
    gather_agentdetails.user_agents.pop(key, None)


def _copies(messages, text, role=None):
    """How many of ``messages`` (of ``role``, when given) are ``text``, or
    hold it."""
    return sum(1 for m in messages
               if (role is None or m.get('role') == role) and text in str(m.get('content')))


def _histories(writer):
    assistant, proxy = writer.agents[writer.key]
    return [dict(m) for m in proxy.chat_messages[assistant]], \
        [dict(m) for m in assistant.chat_messages[proxy]]


def test_a_retry_after_failed_runs_runs_on_its_own_model_with_one_copy(writer):
    """F1 (FAILS ON 38c753ea): the third run reused the writer built on the
    node's model, with the two failed prompts still in its history."""
    from core.agent_tools import is_user_facing_error
    for model in (None, None, EXPERT):
        assert is_user_facing_error(writer.run(BRIEF, model))
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
    assert _copies(messages, BRIEF, 'user') == 1, (
        'the writer does not hold the original task exactly once')
    assert _copies(messages, PROCEED, 'user') == 1


def test_a_persons_failed_request_is_sent_again_with_try_again(writer):
    """FAILS ON 86e5185d: a person's request failed with a 429, they typed
    "try again" (the error reply tells them to), and the model was sent only
    "try again"; their request was gone."""
    from core.agent_tools import is_user_facing_error
    assert is_user_facing_error(writer.gather(REQUEST))
    writer.gather(TRY_AGAIN)
    writer.gather(TRY_AGAIN)
    for models, messages in writer.calls[1:]:
        assert _copies(messages, REQUEST, 'user') == 1, 'the request was lost or repeated'
        assert _copies(messages, TRY_AGAIN, 'user') == 1


def test_a_persons_own_writer_keeps_its_conversation(writer):
    """FAILS ON 30fb3b75: a failed autonomous run on the same user and prompt
    dropped the person's own writer and its conversation.  The failed
    message stays once, unanswered."""
    from hartos.gather_agentdetails import create_agents_for_user
    assistant, proxy = create_agents_for_user(USER)
    writer.agents[writer.key] = (assistant, proxy)
    proxy.send('I want an agent that finds rice importers', assistant,
               request_reply=False, silent=True)
    assistant.send('{"status": "pending", "question": "Which countries?"}', proxy,
                   request_reply=False, silent=True)
    before = [dict(m) for m in assistant.chat_messages[proxy]]
    writer.run(BRIEF)
    writer.run(BRIEF)
    kept = writer.agents.get(writer.key)
    assert kept is not None and kept[0] is assistant and kept[1] is proxy, (
        "the person's writer was dropped")
    after = [dict(m) for m in assistant.chat_messages[proxy]]
    assert after[:2] == before, "the person's conversation changed"
    assert _copies(after, BRIEF) == 1, 'the failed message was lost or repeated'
    assert _copies(writer.calls[-1][1], 'finds rice importers', 'user') == 1


def test_a_turn_on_another_model_is_held_while_a_gather_runs(writer):
    """FAILS ON 86e5185d: a gather on the expert was running when a gather on
    the node's model came for the same writer; the second one switched the
    writer to ['local'] and called the model mid-turn."""
    from core.agent_tools import is_held_for_another_turn
    second = {}

    def other_turn():
        thread = threading.Thread(target=lambda: second.update(
            reply=writer.gather(TRY_AGAIN)))
        thread.start()
        thread.join(60)
        assistant, _proxy = writer.agents[writer.key]
        second['models'] = [c.get('model') for c in assistant.llm_config['config_list']]
    writer.script = [other_turn]
    writer.gather(REQUEST, EXPERT)
    assert is_held_for_another_turn(second.get('reply')), second
    assert len(writer.calls) == 1, 'the held turn called the model'
    assert second['models'] == ['claude-haiku-5-5'], 'the running turn was switched'


def test_a_failed_format_re_ask_leaves_both_histories_as_they_were(writer):
    """FAILS ON 86e5185d (and on 38c753ea): the reply was 'completed' with no
    flows, so the writer re-asks for the proper format; that send failed and
    its message stayed in both histories."""
    finished = '{"status": "completed", "name": "Rice Desk"}'
    writer.script = [finished, LIMITED]
    reply = writer.gather(REQUEST)
    assert reply == finished
    proxy_side, assistant_side = _histories(writer)
    assert [m['content'] for m in assistant_side] == [REQUEST, finished]
    assert [m['content'] for m in proxy_side] == [REQUEST, finished]


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


def test_a_turn_held_for_another_did_not_run_and_is_not_put_in_review(hie, flags):
    """A gather held for another turn of its writer (helper.group_turn) saved
    nothing either (FAILS ON 86e5185d)."""
    from core.agent_tools import held_for_another_turn_reply
    status = hie._autonomous_creation_status(
        USER, PID, held_for_another_turn_reply(), 'daemon create')
    assert status == 'Creation Mode'
    assert hie.review_agents == {}


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
