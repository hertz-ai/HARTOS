"""A flow's end asks for its schedule without the chat, and leaves no ask open.

LIVE (2026-11-26, installed build #5, speech agent 95568353503, flow 7): the
flow end put "Reflect on the sequence of tasks and create scheduled_tasks
..." to the flow's agents in the build's group chat with their tools live,
and _save_flow_recipe read the schedule from the chat's last message.  The
round ran from 12:23:46 to 12:32:20: the agents sent the parent messages,
opened three cards, read the camera and saved a child_day the parent never
gave, and the turns said "Step 5 is waiting for your reply" on a flow of
four steps.  When the JSON came it was in a fence; the flow 7 recipe has no
schedule.  Three asks opened after the last step was banked were still open
when flow 8 started, and flow 8's own asks were refused five times as "3 own
asks already open" (12:34:28-12:37:24).

Now the schedule is ONE stateless call to the node's model
(core.llm_json.ask_model_json, as the dependency question has asked since
09431c2a1), with the flow's own text and the person's recorded answers of
the flow: nothing is posted to any chat and no tool runs.  A job REUSE
cannot run (an action outside the flow, a cron that does not parse) is
dropped with a warning, and every kept job carries the flow's persona.  A
flow the model gave no schedule is saved marked (schedule_asked false) and
asked once more before the build says the agent is built (hartos-17's
review: no silent empty schedule).  The ordered actions stay the flow's
final_recipe.  A saved job is the job REUSE's create_schedule registers for
the flow's persona at session start.  And the flow end closes the build's
asks that are still open (create_recipe._close_finished_action_asks, the
close a banked step already gets) before the next flow starts.

Each saved timed job also runs once right away, for its creator to review
(owner 2026-11-26 ~14:50 IST: "Run once right away", "at creation time is
to review"), with hartos-17's conditions: the build answers "built" first
and the job runs after, right away, on its own thread, as the creator's own
build work: between rounds it gives the model to a person's request being
served, and it does not stop for the creator being at the computer; it runs
only its own actions and reads what is saved for the person; the asks it
opens are the build's and close when it ends; the creator is told plainly
what it did, and a failure is told the same way while the saved build stays
as it is.  A time run a person paused stops at its action instead of asking
for rounds the selector ends at once.  The build end used to run each job
inside the person's last turn; that never fired while no flow recipe held a
job (0 of 361 on the owner's desktop), and it would have raised at once: it
handed the plan's action texts to get_action_byaction_id, which reads
action_ids.

    python -m pytest tests/unit/test_a_flow_end_asks_its_schedule_without_the_chat.py -q -p no:cacheprovider --noconftest
"""
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    ACTIONS, PROMPT_ID, UP, USER_ID, _content, _run, create_env)

_SCHEDULER = 'Reflect on the sequence'
_NONE = {'scheduled_tasks': [], 'visual_scheduled_tasks': []}


@pytest.fixture
def idle_desktop(monkeypatch):
    """tests/conftest.py's idle_desktop, here so the file also runs under
    --noconftest: nobody is at this computer, so no background gate says stop."""
    from core import resource_governor
    monkeypatch.setattr(resource_governor, 'get_physical_input_state',
                        lambda **_kw: (0, None))
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, 'person_using_the_machine', lambda: None)
    monkeypatch.setattr(dispatch, 'should_yield_to_user', lambda: False)


def _the_model_answers(monkeypatch, *schedules, before_schedule=None):
    """The node's model: no dependencies, and the schedule questions
    answered in turn from ``schedules`` (None: a call that timed out; the
    last answer repeats).  ``before_schedule()`` runs as each dependency
    question is asked, which is inside the flow end, before its schedule.
    Returns the schedule calls made, as (content, schema, kw)."""
    asked = []
    answers = list(schedules) or [_NONE]

    def model(content, schema, name, **kw):
        if name == 'flow_schedule':
            asked.append((content, schema, kw))
            return answers[min(len(asked), len(answers)) - 1]
        if before_schedule is not None:
            before_schedule()
        return {key: [] for key in schema['properties']}
    import core.llm_json
    monkeypatch.setattr(core.llm_json, 'ask_model_json', model)
    return asked


def _the_review_runs(env, monkeypatch, fails=None):
    """The build-end review run's time group, recorded instead of run (it
    would call the model, which this harness forbids): what it was given,
    whether the build had already answered when it ran, the ask scope and
    the turn id it ran under.  It waits for the test to say the build
    answered (``built``), so a run made inside the person's turn shows as
    answered_first False.  A run that does not fail finishes its actions,
    the way state_transition1 counts them.  ``fails``: an exception the run
    raises."""
    import integrations.agent_engine.person_ask as pa
    from core.llm_outbound_logger import turn_request_id
    from hartos.threadlocal import thread_local_data
    from integrations.agent_engine.dispatch import is_current_request_autonomous
    built = threading.Event()
    ran = []

    def time_run(task_description, user_id, prompt_id, entry, actions=(),
                 action_exit_point=None):
        answered = built.wait(timeout=60)
        ran.append({'task': task_description, 'user_id': user_id,
                    'prompt_id': prompt_id, 'entry': entry, 'exit': action_exit_point,
                    'actions': [a.get('action_id') for a in actions],
                    'answered_first': answered, 'ask_scope': pa._create_run_of.get(),
                    'request_id': thread_local_data.get_request_id(),
                    'turn_id': turn_request_id(),
                    'background': is_current_request_autonomous()})
        if fails is not None:
            raise fails
        env.cr.time_actions[UP].current_action = int(action_exit_point) + 1
        return 'done'
    monkeypatch.setattr(env.cr, 'time_based_execution', time_run)
    return built, ran


def _only_on_the_review_thread(value, otherwise):
    """A stand-in that answers ``value()`` on a review run's thread and
    ``otherwise`` on any other (the build's own turn asks the same gate)."""
    def answer(*_a, **_k):
        if threading.current_thread().name.startswith('job-review-'):
            return value()
        return otherwise
    return answer


def _the_reviews_end():
    for thread in threading.enumerate():
        if thread.name.startswith('job-review-'):
            thread.join(timeout=120)
            assert not thread.is_alive(), 'a review run did not end'


def _told(env):
    """What the creator was sent (send_message_to_user1's text)."""
    return [str(args[1]) for args in env.sent]


def _saved(env, flow=0):
    path = env.prompts / f'{PROMPT_ID}_{flow}_recipe.json'
    assert path.exists(), f'flow {flow} was not saved'
    return json.loads(path.read_text(encoding='utf-8'))


def _two_flows(env):
    config_path = env.prompts / f'{PROMPT_ID}.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['flows'] = [
        {'persona': 'Researcher', 'sub_goal': 'weekly summary', 'actions': ACTIONS},
        {'persona': 'Researcher', 'sub_goal': 'monthly summary', 'actions': ACTIONS},
    ]
    config_path.write_text(json.dumps(config), encoding='utf-8')


def _log(caplog):
    return '\n'.join(r.getMessage() for r in caplog.records)


def test_the_flow_end_puts_no_question_to_the_agents(create_env, monkeypatch):
    env = create_env
    script = env.script
    _the_model_answers(monkeypatch, _NONE)
    reached = []
    agent_reply = script.assistant

    def assistant(recipient, messages=None, sender=None, config=None):
        if _SCHEDULER in _content(script._last()):
            reached.append(_content(script._last())[:80])
        return agent_reply(recipient, messages=messages, sender=sender, config=config)
    script.assistant = assistant

    replies = _run(env, turns=1)

    _saved(env)
    assert reached == [], (
        f'the flow end put the schedule question to the agents, their tools '
        f'live, {len(reached)} time(s); replies {replies!r}')
    assert not any(_SCHEDULER in _content(m) for m in script.gc.messages), (
        'the schedule question was posted to the build chat')


def test_the_models_schedule_is_saved_from_one_stateless_call(create_env, monkeypatch):
    env = create_env
    job = {'cron_expression': '0 9 * * 1', 'action_entry_point': 1,
           'action_exit_point': 3,
           'job_description': 'Collect and summarise the sources again'}
    visual = {'cron_expression': '0 18 * * *',
              'job_description': 'Look at the desk for printed sources'}
    asked = _the_model_answers(monkeypatch, {'scheduled_tasks': [job],
                                             'visual_scheduled_tasks': [visual]})
    built, ran = _the_review_runs(env, monkeypatch)

    replies = _run(env, turns=1)
    built.set()
    _the_reviews_end()

    saved = _saved(env)
    assert len(asked) == 1, f'expected one schedule question, got {len(asked)}'
    content, schema, kw = asked[0]
    for action in ACTIONS:
        assert action in content, f'the question does not name {action!r}'
    assert kw.get('once') is True, kw
    assert sorted(schema['required']) == ['scheduled_tasks', 'visual_scheduled_tasks']
    assert saved.get('scheduled_tasks') == [dict(job, persona='Researcher')], saved
    assert saved.get('visual_scheduled_tasks') == [dict(visual, persona='Researcher')], saved
    assert 'schedule_asked' not in saved, saved
    assert [a.get('action_id') for a in saved['actions']] == [1, 2, 3], saved
    assert replies == ['Agent Created Successfully'], replies


def test_a_saved_job_runs_once_for_review_after_the_build_answered(
        create_env, monkeypatch):
    """Owner 2026-11-26: the build runs each timed job once right away so its
    creator can review it.  It runs after "built" is answered, under its own
    id (on the thread-local and on the context variable the model calls
    carry; not a daemon_ id: it is the creator's own build work), only over
    its own actions, in the build's ask scope, and the creator is told
    plainly what it did; a camera job is named and left to its time."""
    env = create_env
    job = {'cron_expression': '0 9 * * 1', 'action_entry_point': 2,
           'action_exit_point': 3, 'job_description': 'Summarise the week'}
    visual = {'cron_expression': '0 18 * * *',
              'job_description': 'Look at the desk for printed sources'}
    _the_model_answers(monkeypatch, {'scheduled_tasks': [job],
                                     'visual_scheduled_tasks': [visual]})
    built, ran = _the_review_runs(env, monkeypatch)

    replies = _run(env, turns=1)
    built.set()
    _the_reviews_end()

    assert replies == ['Agent Created Successfully'], replies
    assert len(ran) == 1, ran
    run = ran[0]
    assert run['answered_first'] is True, (
        'the review ran inside the person\'s turn, before "built" was answered')
    assert (run['task'], run['user_id'], run['prompt_id']) == (
        job['job_description'], USER_ID, PROMPT_ID), run
    assert (run['entry'], run['exit']) == (2, 3), run
    assert run['actions'] == [1, 2, 3], 'the run must get the saved actions, by id'
    assert run['ask_scope'] == (str(PROMPT_ID), str(USER_ID)), run
    review_id = f'job-review-{PROMPT_ID}'
    assert (run['request_id'], run['turn_id'], run['background']) == (
        review_id, review_id, False), run
    told = _told(env)
    report = [t for t in told if job['job_description'] in t and 'once now' in t]
    assert report, told
    assert 'stopped before' not in report[0] and 'next on Monday' in report[0], report
    assert not any(word in report[0] for word in ('0 9 * * 1', 'step', 'action')), (
        f'the report shows machinery: {report[0]!r}')
    assert any(visual['job_description'] in t and 'camera' in t for t in told), told


def test_the_review_tells_what_the_job_sent_and_none_of_its_machinery(
        create_env, monkeypatch):
    env = create_env
    job = {'cron_expression': '0 9 * * 1', 'action_entry_point': 1,
           'action_exit_point': 2, 'job_description': 'Summarise the week'}
    _the_model_answers(monkeypatch, {'scheduled_tasks': [job],
                                     'visual_scheduled_tasks': []})
    built, ran = _the_review_runs(env, monkeypatch)
    built.set()
    real_run = env.cr.time_based_execution

    def time_run(*a, **k):
        group = env.cr.time_agents[UP]['time_group_chat']
        group.messages += [
            {'role': 'user', 'name': 'user_proxy_x',
             'content': 'This is the time now ... the current Action to execute: step 1'},
            {'role': 'assistant', 'name': 'time_agent',
             'content': '@user {"message2user": "Here is your week: three sessions."}'},
            {'role': 'user', 'name': 'StatusVerifier',
             'content': '{"status": "completed", "action": "send", "action_id": 2, '
                        '"message": "The summary was sent."}'},
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'TERMINATE'}]
        return real_run(*a, **k)
    monkeypatch.setattr(env.cr, 'time_based_execution', time_run)

    _run(env, turns=1)
    _the_reviews_end()

    assert len(ran) == 1, ran
    report = [t for t in _told(env) if 'Summarise the week' in t]
    assert len(report) == 1, _told(env)
    assert 'It sent you: "Here is your week: three sessions."' in report[0], report
    for machinery in ('status', 'TERMINATE', 'current Action', 'action_id'):
        assert machinery not in report[0], (machinery, report[0])


def test_a_review_that_fails_is_told_plainly_and_the_build_stays(
        create_env, monkeypatch):
    env = create_env
    job = {'cron_expression': '0 9 * * 1', 'action_entry_point': 1,
           'action_exit_point': 2, 'job_description': 'Summarise the week'}
    _the_model_answers(monkeypatch, {'scheduled_tasks': [job],
                                     'visual_scheduled_tasks': []})
    built, ran = _the_review_runs(env, monkeypatch,
                                  fails=RuntimeError('the model went away'))
    built.set()

    replies = _run(env, turns=1)
    before = (env.prompts / f'{PROMPT_ID}_0_recipe.json').read_bytes()
    _the_reviews_end()

    assert replies == ['Agent Created Successfully'], replies
    assert len(ran) == 1, ran
    told = _told(env)
    assert any('could not finish' in t and 'stays saved' in t for t in told), told
    assert not any('RuntimeError' in t or 'went away' in t for t in told), told
    assert (env.prompts / f'{PROMPT_ID}_0_recipe.json').read_bytes() == before
    assert _saved(env).get('scheduled_tasks') == [dict(job, persona='Researcher')]


def test_the_review_does_not_stop_for_the_creator_at_the_computer(
        create_env, monkeypatch):
    """hartos-17's ruling on the wait (2026-11-26): until the owner says
    otherwise the review is the creator's own build work, run right away;
    the creator being at the computer does not stop it (the background
    yield, dispatch.background_work_must_yield, answers no on its thread)."""
    env = create_env
    job = {'cron_expression': '0 9 * * 1', 'action_entry_point': 1,
           'action_exit_point': 2, 'job_description': 'Summarise the week'}
    _the_model_answers(monkeypatch, {'scheduled_tasks': [job],
                                     'visual_scheduled_tasks': []})
    built, ran = _the_review_runs(env, monkeypatch)
    built.set()
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, 'person_using_the_machine',
                        _only_on_the_review_thread(lambda: 'user_present', None))
    yields = []
    recorded_run = env.cr.time_based_execution

    def time_run(*a, **k):
        yields.append(dispatch.background_work_must_yield())
        return recorded_run(*a, **k)
    monkeypatch.setattr(env.cr, 'time_based_execution', time_run)

    replies = _run(env, turns=1)
    _the_reviews_end()

    assert replies == ['Agent Created Successfully'], replies
    assert len(ran) == 1, ran
    assert yields == [False], f'the review would stop for the creator: {yields}'


def test_a_time_round_waits_while_a_persons_request_is_served(create_env, monkeypatch):
    """Between rounds the time group gives the model to a person's request in
    flight: state_transition1 waits (core.foreground.wait_until_clear) before
    it picks a speaker, so the review's first round runs only once the
    build's own request has been answered, and a new turn of the creator's
    is answered before the review's next round."""
    env = create_env
    from core import foreground
    real_wait = env.cr._wait_for_a_persons_request
    waited = []
    monkeypatch.setattr(env.cr, '_wait_for_a_persons_request', waited.append)
    monkeypatch.setitem(env.cr.final_recipe, PROMPT_ID, {'actions': []})
    monkeypatch.setitem(env.cr.time_actions, UP, None)   # create_time_agents writes it
    with env.app.app_context():
        agents = env.cr.create_time_agents(USER_ID, PROMPT_ID, 'Researcher', '', [])
        group = agents['time_group_chat']
        group.messages = []
        picked = group.speaker_selection_method(agents['time_user'], group)
    assert waited == [UP], f'the round did not wait for a request in flight: {waited}'
    assert picked is agents['time_agent'], picked

    answered = threading.Event()

    def a_round():
        with env.app.app_context():
            real_wait(UP)
        answered.set()
    foreground.enter_foreground()
    try:
        threading.Thread(target=a_round, name='time-round', daemon=True).start()
        assert not answered.wait(0.5), 'the round went on while the request was served'
    finally:
        foreground.exit_foreground()
    assert answered.wait(10), 'the round did not go on once the request was answered'


def test_the_review_closes_the_asks_it_opened_when_it_ends(create_env, monkeypatch):
    env = create_env
    job = {'cron_expression': '0 9 * * 1', 'action_entry_point': 1,
           'action_exit_point': 1, 'job_description': 'Ask how the week went'}
    _the_model_answers(monkeypatch, {'scheduled_tasks': [job],
                                     'visual_scheduled_tasks': []})
    built, ran = _the_review_runs(env, monkeypatch)
    built.set()
    import integrations.agent_engine.person_ask as pa
    closes = []
    real_close = pa.close_create_run_asks

    def close(prompt_id, user_id):
        closes.append((str(prompt_id), str(user_id), len(ran)))
        return real_close(prompt_id, user_id)
    monkeypatch.setattr(pa, 'close_create_run_asks', close)

    _run(env, turns=1)
    _the_reviews_end()

    assert len(ran) == 1, ran
    assert (str(PROMPT_ID), str(USER_ID), 1) in closes, (
        f'no close of the build\'s asks after the review run: {closes}')


def test_a_job_runs_only_its_own_actions_and_reads_what_is_saved(
        create_env, monkeypatch):
    """The time group's run (create_recipe._run_time_group) stops after the
    job's action_exit_point, and its first message carries what is saved
    for the person, so the run need not ask for it."""
    env = create_env
    from core.agent_tools import person_store_id
    from hartos import helper
    helper.clear_pause_request()
    sent = _a_time_group(env, monkeypatch)
    monkeypatch.setitem(env.cr.agent_data, person_store_id(UP), {'child_name': 'Zoey'})

    with env.app.app_context():
        env.cr._run_time_group('Weekly review', USER_ID, PROMPT_ID, 2, UP,
                               action_exit_point=3)

    assert len(sent) == 2, f'the job ran past its last action: {sent}'
    assert 'step 2 of the weekly job' in sent[0], sent[0]
    assert 'step 3 of the weekly job' in sent[1], sent[1]
    assert 'Zoey' in sent[0], 'the run was not given what is saved for the person'


def test_a_time_run_a_person_paused_stops_at_its_action(create_env, monkeypatch):
    """state_transition1 ends a background round while a person is using the
    computer (helper.yield_between_rounds marks the turn).  The time run
    stops there with the pause reply and its action where it was; it went
    on asking for rounds the selector ended at once."""
    env = create_env
    from core.agent_tools import is_user_pause
    from hartos import helper
    helper.clear_pause_request()

    def paused():
        helper._turn_pause.requested = True
    sent = _a_time_group(env, monkeypatch, each_round=paused)

    try:
        with env.app.app_context():
            out = env.cr._run_time_group('Weekly review', USER_ID, PROMPT_ID, 2, UP,
                                         action_exit_point=3)
    finally:
        helper.clear_pause_request()

    assert len(sent) == 1, f'rounds were asked for after the pause: {sent}'
    assert is_user_pause(out), out
    assert env.cr.time_actions[UP].current_action == 2


def test_an_action_that_never_finishes_is_nudged_only_so_often(create_env, monkeypatch):
    """A round that ends with no completed verdict is nudged ("Lets continue
    the work ...") as often as the main loop re-sends a remedy, then the run
    stops; the nudge went on with no end."""
    env = create_env
    from hartos import helper
    helper.clear_pause_request()
    sent = _a_time_group(env, monkeypatch, verdict='pending')

    with env.app.app_context():
        out = env.cr._run_time_group('Weekly review', USER_ID, PROMPT_ID, 2, UP,
                                     action_exit_point=3)

    assert len(sent) == 1 + env.cr._REMEDY_MAX_ATTEMPTS, sent
    assert all('Lets continue the work' in m for m in sent[1:]), sent
    assert out == 'done', out
    assert env.cr.time_actions[UP].current_action == 2


def _a_time_group(env, monkeypatch, each_round=None, verdict='completed'):
    """A time group for the four actions of a weekly job whose rounds are
    recorded instead of run: a round ends with the verifier's ``verdict``
    and a completed one finishes its action, the way state_transition1
    counts it, unless ``each_round()`` (run first) ends the round as the
    selector does for a person.  Returns the messages each round was
    started with."""
    from hartos.helper import Action
    actions = [{'action_id': n, 'action': f'step {n} of the weekly job',
                'can_perform_without_user_input': 'yes'} for n in range(1, 5)]
    monkeypatch.setitem(env.cr.time_actions, UP, Action(actions))
    group = SimpleNamespace(messages=[])
    sent = []

    def initiate_chat(recipient, message=None, **kw):
        sent.append(message)
        if each_round is not None:
            each_round()
            return
        step = env.cr.time_actions[UP].current_action
        group.messages += [
            {'role': 'user', 'name': 'StatusVerifier',
             'content': json.dumps({'status': verdict, 'action_id': step})},
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'TERMINATE'}]
        if verdict == 'completed':
            env.cr.time_actions[UP].current_action += 1
    seat = SimpleNamespace(initiate_chat=initiate_chat)
    monkeypatch.setitem(env.cr.time_agents, UP, {
        'time_group_chat': group, 'time_user': seat, 'time_manager': object(),
        'chat_instructor1': seat})
    return sent


def test_a_saved_job_is_registered_by_reuse_for_the_flows_persona(
        create_env, monkeypatch):
    """The round trip: the job the flow end saves is the job REUSE's
    create_schedule registers at session start, under the persona the
    agent's plan gives the flow, for its time, starting at its action."""
    env = create_env
    job = {'cron_expression': '30 19 * * *', 'action_entry_point': 2,
           'action_exit_point': 3, 'job_description': 'Send the evening summary'}
    _the_model_answers(monkeypatch, {'scheduled_tasks': [job],
                                     'visual_scheduled_tasks': []})
    built, _ran = _the_review_runs(env, monkeypatch)
    built.set()
    _run(env, turns=1)
    _the_reviews_end()

    import hartos.reuse_recipe as rr
    persona = json.loads((env.prompts / f'{PROMPT_ID}.json').read_text(
        encoding='utf-8'))['flows'][0]['persona']
    added = []

    class _Scheduler:
        def add_job(self, func, trigger=None, id=None, args=None, **kw):
            added.append({'func': func, 'trigger': trigger, 'id': id, 'args': args})
    monkeypatch.setattr(rr, 'scheduler', _Scheduler())
    monkeypatch.setattr(rr, 'get_flow_number', lambda uid, pid: (0, persona))
    monkeypatch.setattr(rr, 'ACTION_API', '')
    with env.app.app_context():
        rr.create_schedule(PROMPT_ID, USER_ID)

    jobs = [a for a in added if str(a['id']).startswith('sched_')]
    assert len(jobs) == 1, added
    assert jobs[0]['id'] == f'sched_{UP}_0', jobs
    assert jobs[0]['args'] == [job['job_description'], USER_ID, PROMPT_ID, 2], jobs
    trigger = str(jobs[0]['trigger'])
    assert "hour='19'" in trigger and "minute='30'" in trigger, trigger


def test_the_call_carries_the_flows_own_text_and_the_persons_answers(
        create_env, monkeypatch):
    """The chat round saw the whole chat, which is where a creator says
    "every evening at 7": the stateless call gets the flow's text from the
    agent's plan and what the person answered in the flow."""
    env = create_env

    def the_person_answered():
        env.lh.record_user_answer(UP, 2, 'Send me the summary every evening at 7',
                                  0, request_id='msg_person_e2e')
    asked = _the_model_answers(monkeypatch, _NONE,
                               before_schedule=the_person_answered)

    _run(env, turns=1)

    assert len(asked) == 1, asked
    content = asked[0][0]
    for words in ('Researcher', 'weekly summary',
                  'Send me the summary every evening at 7'):
        assert words in content, f'{words!r} is not in the schedule question'


def test_the_ordered_actions_are_the_flows_final_recipe_when_it_is_saved(
        create_env, monkeypatch):
    """create_recipe.py's timer path reads final_recipe[prompt_id]['actions']
    (hartos-17's review): the flow end still writes the ordered actions
    there before the save."""
    env = create_env
    _two_flows(env)
    _the_model_answers(monkeypatch, _NONE)
    at_save = []
    real_save = env.cr._save_flow_recipe

    def save(flow, *a, **k):
        at_save.append((flow, json.loads(json.dumps(env.cr.final_recipe.get(PROMPT_ID)))))
        return real_save(flow, *a, **k)
    monkeypatch.setattr(env.cr, '_save_flow_recipe', save)

    _run(env, turns=1)

    assert [flow for flow, _ in at_save] == [0, 1], at_save
    for flow, final in at_save:
        assert isinstance(final, dict) and final.get('status') == 'completed', (flow, final)
        assert [a.get('action_id') for a in final.get('actions') or []] == [1, 2, 3], (
            flow, final)


def test_no_answer_marks_the_flow_and_the_build_end_asks_again(
        create_env, monkeypatch, caplog):
    env = create_env
    _two_flows(env)
    job = {'cron_expression': '0 9 * * 1', 'action_entry_point': 1,
           'action_exit_point': 3, 'job_description': 'Summarise the week'}
    # Flow 0's end: no answer.  Flow 1's end: no job.  The build end asks
    # flow 0 again and gets its job.
    asked = _the_model_answers(monkeypatch, None, _NONE,
                               {'scheduled_tasks': [job],
                                'visual_scheduled_tasks': []})
    built, _ran = _the_review_runs(env, monkeypatch)
    built.set()
    marked = []
    real_save = env.cr._save_flow_recipe

    def save(flow, *a, **k):
        out = real_save(flow, *a, **k)
        marked.append((flow, _saved(env, flow).get('schedule_asked')))
        return out
    monkeypatch.setattr(env.cr, '_save_flow_recipe', save)

    with caplog.at_level(logging.INFO):
        replies = _run(env, turns=1)
    _the_reviews_end()

    assert marked == [(0, False), (1, None)], marked
    assert len(asked) == 3, f'expected flow 0, flow 1, then flow 0 again: {len(asked)}'
    assert 'weekly summary' in asked[2][0], 'the build end asked another flow again'
    flow0 = _saved(env, 0)
    assert flow0.get('scheduled_tasks') == [dict(job, persona='Researcher')], flow0
    assert 'schedule_asked' not in flow0, flow0
    assert _saved(env, 1).get('scheduled_tasks') == [], _saved(env, 1)
    log = _log(caplog)
    assert '[FLOW-SCHEDULE] flow 0: the model gave no schedule' in log, log[-3000:]
    assert '[FLOW-SCHEDULE] flow 0: asked again before the build ends' in log, log[-3000:]
    assert replies == ['Agent Created Successfully'], replies


def test_a_job_reuse_cannot_run_is_dropped_and_said(create_env, monkeypatch, caplog):
    env = create_env
    good = {'cron_expression': '30 7 * * *', 'action_entry_point': 2,
            'action_exit_point': 2, 'job_description': 'Summarise the sources'}
    _the_model_answers(monkeypatch, {
        'scheduled_tasks': [
            good,
            {'cron_expression': '0 9 * * *', 'action_entry_point': 9,
             'action_exit_point': 9, 'job_description': 'no such action'},
            {'cron_expression': 'every morning', 'action_entry_point': 1,
             'action_exit_point': 1, 'job_description': 'not a cron'},
            {'cron_expression': '0 9 * * *', 'action_entry_point': 3,
             'action_exit_point': 1, 'job_description': 'ends before it starts'},
            'not a job'],
        'visual_scheduled_tasks': [{'cron_expression': '61 * * * *',
                                    'job_description': 'minute 61'}]})
    built, _ran = _the_review_runs(env, monkeypatch)
    built.set()

    with caplog.at_level(logging.INFO):
        _run(env, turns=1)
    _the_reviews_end()

    saved = _saved(env)
    assert saved.get('scheduled_tasks') == [dict(good, persona='Researcher')], saved
    assert saved.get('visual_scheduled_tasks') == [], saved
    assert _log(caplog).count('[FLOW-SCHEDULE] flow 0: dropped') == 5, _log(caplog)[-4000:]


def test_the_flow_end_closes_the_builds_open_asks_before_the_next_flow(
        create_env, monkeypatch, caplog):
    env = create_env
    _two_flows(env)
    _the_model_answers(monkeypatch, _NONE)
    events = []
    import integrations.agent_engine.person_ask as pa

    def close(prompt_id, user_id):
        events.append(('closed', str(prompt_id), str(user_id)))
        return 2
    monkeypatch.setattr(pa, 'close_create_run_asks', close)
    real_save = env.cr._save_flow_recipe

    def save(flow, *a, **k):
        out = real_save(flow, *a, **k)
        events.append(('saved', flow))
        return out
    monkeypatch.setattr(env.cr, '_save_flow_recipe', save)
    real_next = env.cr._next_flow_reply

    def next_flow(*a, **k):
        events.append(('next',))
        return real_next(*a, **k)
    monkeypatch.setattr(env.cr, '_next_flow_reply', next_flow)

    with caplog.at_level(logging.INFO):
        _run(env, turns=1)

    assert ('saved', 0) in events and ('next',) in events, events
    between = events[events.index(('saved', 0)) + 1:events.index(('next',))]
    assert ('closed', str(PROMPT_ID), str(USER_ID)) in between, (
        f'flow 0 ended with its asks left open for flow 1: {events}')
    assert f'[CREATE-ASKS] flow 0 of agent {PROMPT_ID} ended: closed 2' in _log(caplog), (
        _log(caplog)[-3000:])
