"""A recipe the model writes during a build carries no person's detail the
person gave while the agent was being built: a name or other detail of a
person that the agent saved is written as a blank.  The person's other words
("Done", "great", "Yes") stay as the model wrote them, since the model's
words are the agent's own.

An agent is built once and used by many people (owner, d1d88c954: people
are blanks, <child_name>, <home_language>, ..., filled per person at run
time).  32a34f214 made the trace bank write the person's build answers as
their blanks (lifecycle_hooks.build_answer_blanks / blank_build_answers).
The recipe the model writes itself was still saved as written: the action
save (RECIPE_RECEIVED), its late twin, the loop's save from the recovered
chat, and the flow's scheduled jobs that _save_flow_recipe copies from the
scheduler's reply.  A step "Say hi to Meena", after the build's test person
answered "Meena", put the name in the recipe file, which leaves the node
(push_recipe, peer_reuse's export).

The model's words are the agent's own, and REUSE shows the steps it wrote
as they are, so only what the code knows is a person's detail is blanked in
them (review of c72dc3443).  A reply blanked by the saved mark's rule,
"Done" or "great", made "When the child is done, say Great job!" read "When
the child is <saved_str>, say <saved_str> job!".  What the code knows is a
person's detail is a value the agent saved under a person's key: a blank the
plan author writes for a person (<child_name>, <family_names>, ...), a blank
this agent's plan writes, or a key that names a person or a name
(family_members, kid_name, child.first_name, mother, name).  It is written
as the key's blank, or as the saved mark when the key is no blank's shape
(<name>).  A list of names saved as one text is blanked name by name.  9f5fcd0ca
knew only the exact blanks, and a name saved as family_members, the demo
speech agent's own key, stayed in the recipe (review rv_66reuse).

This drives the REAL loop (create_recipe.recipe -> get_response_group) with
the scripted agents of tests/unit/test_create_loop_end_to_end.py, no LLM.
At step 1 the agent asks and the person answers in the chat, and the answer
is recorded on the session's real ledger as the create gate records it
(lifecycle_hooks.record_user_answer).  The scheduler's reply never ends the
scripted chat, so _save_flow_recipe is called on it directly.

    python -m pytest tests/unit/test_a_model_written_step_keeps_no_build_answer.py -q -p no:cacheprovider
"""
import json
import logging
from types import SimpleNamespace

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401
    PROMPT_ID, UP, USER_ID, create_env, _run)


def _person_answers_at_step_one(env, said, saves=()):
    """At step 1 the agent asks and the person answers, then the agent
    saves what it heard: ``said`` is [(the agent's line, the answer)],
    ``saves`` is [(memory key, value)].  Each answer is recorded on the
    ledger as the create gate records it."""
    real = env.script.helper
    done = []

    def helper(recipient, messages=None, sender=None, config=None):
        reply = real(recipient, messages, sender, config)
        if env.script.current() == 1 and not done:
            done.append(True)
            gc = env.script.gc.messages
            for agent_line, answer in said:
                gc.append({'role': 'assistant', 'name': 'Assistant',
                           'content': agent_line})
                gc.append({'role': 'user', 'name': 'UserProxy',
                           'content': answer})
                assert env.lh.record_user_answer(UP, 1, answer, 0), answer
            for i, (key, value) in enumerate(saves):
                cid = f'call_save_{i}'
                gc.append({'role': 'assistant', 'name': 'Assistant',
                           'content': None, 'tool_calls': [{
                               'id': cid, 'type': 'function', 'function': {
                                   'name': 'save_data_in_memory',
                                   'arguments': json.dumps(
                                       {'key': key, 'value': value})}}]})
                gc.append({'role': 'tool', 'name': 'Helper', 'content': 'Saved',
                           'tool_responses': [{'tool_call_id': cid,
                                               'role': 'tool',
                                               'content': 'Saved'}]})
        return reply
    env.script.helper = helper


def _model_writes_recipe(env, step=1, **fields):
    """The model's recipe for ``step`` (1 unless named) holds ``fields``
    (its steps and the rest); other steps get the script's own recipe."""
    real = env.script.verifier

    def verifier(recipient, messages=None, sender=None, config=None):
        before = len(env.script.recipe_requests)
        ok, reply = real(recipient, messages, sender, config)
        if (len(env.script.recipe_requests) > before
                and env.script.current() == step):
            data = json.loads(reply)
            data.update(fields)
            reply = json.dumps(data)
        return ok, reply
    env.script.verifier = verifier


def _model_files(env):
    """Each action file saved from the model's recipe: (raw text, data)."""
    out = []
    for p in sorted(env.prompts.glob(f'{PROMPT_ID}_0_*.json')):
        if p.name.endswith('_recipe.json'):
            continue
        raw = p.read_text(encoding='utf-8')
        data = json.loads(raw)
        if data.get('recipe_source') != 'execution_trace':
            out.append((p.name, raw, data))
    return out


def _step_one(env, step=1):
    files = {name: (raw, data) for name, raw, data in _model_files(env)}
    assert f'{PROMPT_ID}_0_{step}.json' in files, (
        f'step {step} was not saved from the model recipe; recipe requests: '
        f'{env.script.recipe_requests}; files: {sorted(files)}')
    return files[f'{PROMPT_ID}_0_{step}.json']


def test_a_name_the_person_gave_is_saved_as_its_blank(create_env, monkeypatch):
    """The person answered "Meena", the agent saved it as child_name, and
    the model's recipe says "Say hi to Meena": the file holds <child_name>
    and no Meena, in its steps or in a job it scheduled."""
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    _person_answers_at_step_one(
        env, [("What is your child's name?", 'Meena')],
        saves=[('child_name', 'Meena')])
    _model_writes_recipe(
        env,
        recipe=[{'steps': 'Say hi to Meena', 'tool_name': '',
                 'generalized_functions': ''}],
        scheduled_tasks=[{'cron_expression': '0 9 * * *',
                          'persona': 'Researcher', 'action_entry_point': 1,
                          'job_description': 'Remind Meena to practise'}])
    _run(env, turns=6)
    raw, data = _step_one(env)
    assert 'meena' not in raw.lower(), raw
    assert data['recipe'][0]['steps'] == 'Say hi to <child_name>', data['recipe']
    assert (data['scheduled_tasks'][0]['job_description']
            == 'Remind <child_name> to practise'), data['scheduled_tasks']
    flow = env.prompts / f'{PROMPT_ID}_0_recipe.json'
    if flow.exists():
        assert 'meena' not in flow.read_text(encoding='utf-8').lower()


def test_a_recipe_saved_late_keeps_no_name_the_person_gave(create_env,
                                                         monkeypatch, caplog):
    """The late-save twin: a recipe that arrives after its step moved on is
    saved by the second writer, which blanks the person's answers too."""
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    monkeypatch.setattr(env.cr, 'lifecycle_hook_track_recipe_completion',
                        lambda *a, **k: {'action': 'allow', 'message': None})
    _person_answers_at_step_one(env, [("What is your child's name?", 'Meena')],
                                saves=[('child_name', 'Meena')])
    _model_writes_recipe(
        env, recipe=[{'steps': 'Say hi to Meena', 'tool_name': '',
                      'generalized_functions': ''}])
    with caplog.at_level(logging.INFO):
        _run(env, turns=2)
    assert any('Saved Individual recipe (late)' in r.getMessage()
               for r in caplog.records), 'the late writer never saved a step'
    raw, data = _step_one(env)
    assert 'meena' not in raw.lower(), raw
    assert data['recipe'][0]['steps'] == "Say hi to <child_name>", data['recipe']


def test_a_word_the_agent_said_first_and_the_routing_stay(create_env,
                                                         monkeypatch):
    """The lesson asks for "ball" and the test child says "ball", which the
    agent saves as a word the child knows: the word is the lesson's and
    stays.  A "Done" the person said blanks no value the loop routes by
    (the step's status, autonomy, persona, tool)."""
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    _person_answers_at_step_one(env, [
        ('Tell me when you have finished.', 'Done'),
        ('Say "ball" after me.', 'ball'),
        ("What is your child's name?", 'Meena')],
        saves=[('known_words', 'ball'), ('child_name', 'Meena')])
    _model_writes_recipe(
        env, recipe=[{'steps': 'Say hi to Meena', 'tool_name': '',
                      'generalized_functions': ''},
                     {'steps': 'Ask the child to say "ball"',
                      'tool_name': 'google_search',
                      'generalized_functions': ''}])
    _run(env, turns=6)
    raw, data = _step_one(env)
    assert 'meena' not in raw.lower(), raw
    steps = [s['steps'] for s in data['recipe']]
    assert steps == ['Say hi to <child_name>', 'Ask the child to say "ball"'], steps
    assert data['status'] == 'done', data
    assert data['can_perform_without_user_input'] == 'yes', data
    assert data['persona'] == 'Researcher', data
    assert data['recipe'][1]['tool_name'] == 'google_search', data['recipe']


def test_a_job_the_scheduler_wrote_keeps_no_name_the_person_gave(create_env,
                                                                monkeypatch):
    """_save_flow_recipe copies the scheduled jobs from the scheduler's
    reply into the flow recipe, and each job's description is run for the
    agent's later users (create_time_agents_and_create_scheduled_jobs): the
    person's answer in it is its blank, and the job's timing stays."""
    from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType
    env = create_env
    led = SmartLedger(agent_id=str(PROMPT_ID), session_id=f'{UP}_0',
                      ledger_dir=str(env.prompts.parent))
    led.tasks['action_1'] = Task(task_id='action_1', description='ask the name',
                                 task_type=TaskType.PRE_ASSIGNED)
    led.tasks['action_1'].status = TaskStatus.IN_PROGRESS
    env.lh.register_ledger_for_session(UP, led)
    try:
        assert env.lh.record_user_answer(UP, 1, 'Meena', 0)
        monkeypatch.setitem(env.cr.final_recipe, PROMPT_ID,
                            {'status': 'completed', 'actions': []})
        chat = SimpleNamespace(messages=[
            {'role': 'assistant', 'name': 'Assistant',
             'content': "What is your child's name?"},
            {'role': 'user', 'name': 'UserProxy', 'content': 'Meena'},
            {'role': 'assistant', 'name': 'Assistant', 'content': None,
             'tool_calls': [{'id': 'call_name', 'type': 'function', 'function': {
                 'name': 'save_data_in_memory',
                 'arguments': json.dumps({'key': 'child_name',
                                          'value': 'Meena'})}}]},
            {'role': 'tool', 'name': 'Helper', 'content': 'Saved',
             'tool_responses': [{'tool_call_id': 'call_name', 'role': 'tool',
                                 'content': 'Saved'}]},
            {'role': 'user', 'name': 'ChatInstructor',
             'content': 'Reflect on the sequence of tasks and create '
                        'scheduled_tasks'},
            {'role': 'user', 'name': 'Assistant', 'content': json.dumps({
                'status': 'completed', 'dependency': [], 'recipe': '',
                'scheduled_tasks': [{
                    'cron_expression': '0 9 * * *', 'persona': 'Researcher',
                    'action_entry_point': 1, 'action_exit_point': 1,
                    'job_description': 'Remind Meena to practise'}],
                'visual_scheduled_tasks': []})}])
        with env.app.app_context():
            env.cr._save_flow_recipe(0, PROMPT_ID, UP, USER_ID, chat)
    finally:
        with env.lh._state_lock:
            env.lh._ledger_registry.pop(UP, None)
    flow = env.prompts / f'{PROMPT_ID}_0_recipe.json'
    raw = flow.read_text(encoding='utf-8')
    data = json.loads(raw)
    assert data.get('scheduled_tasks'), f'no job reached the flow recipe: {data}'
    assert 'meena' not in raw.lower(), raw
    job = data['scheduled_tasks'][0]
    assert job['job_description'] == 'Remind <child_name> to practise', job
    assert (job['cron_expression'], job['persona'], job['action_entry_point']) == (
        '0 9 * * *', 'Researcher', 1), job


def test_the_persons_one_word_replies_leave_the_agents_step_whole(create_env,
                                                                monkeypatch):
    """Review of c72dc3443: the person answered "Done" and "great", words
    the agent had not said (and the agent saved "great" as how the lesson
    felt), and the model's step says "When the child is done, say Great
    job!".  REUSE shows a step the model wrote as it is, so a blank there
    is no lesson: the step stays as the model wrote it.  The model's words
    are the agent's own; only a value saved under a person's key is
    blanked in them, so the name saved as child_name still is.

    lesson_felt and parent_present are no person's key: each names what
    its value is by its last word (how the lesson felt, whether a parent
    is present), not a person or a name, so "great" and "Yes" stay, and
    "Yes! Say it one more time." with them."""
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    _person_answers_at_step_one(env, [
        ('Tell me when you have finished.', 'Done'),
        ('How was that?', 'great'),
        ('Is a grown-up sitting with the child?', 'Yes'),
        ("What is your child's name?", 'Meena')],
        saves=[('lesson_felt', 'great'), ('parent_present', 'Yes'),
               ('child_name', 'Meena')])
    _model_writes_recipe(
        env,
        recipe=[{'steps': 'When the child is done, say Great job!',
                 'tool_name': '', 'generalized_functions': ''},
                {'steps': 'Yes! Say it one more time.',
                 'tool_name': '', 'generalized_functions': ''},
                {'steps': 'Say hi to Meena', 'tool_name': '',
                 'generalized_functions': ''}],
        scheduled_tasks=[{'cron_expression': '0 9 * * *',
                          'persona': 'Researcher', 'action_entry_point': 1,
                          'job_description': 'Ask Meena to practise; when '
                                             'done, say Great job!'}])
    _run(env, turns=6)
    raw, data = _step_one(env)
    assert '<saved_str>' not in raw and 'meena' not in raw.lower(), raw
    steps = [s['steps'] for s in data['recipe']]
    assert steps == ['When the child is done, say Great job!',
                     'Yes! Say it one more time.',
                     'Say hi to <child_name>'], steps
    assert (data['scheduled_tasks'][0]['job_description']
            == 'Ask <child_name> to practise; when done, say Great job!'), (
        data['scheduled_tasks'])


def test_a_number_in_a_job_the_model_wrote_stays_a_number(create_env,
                                                          monkeypatch):
    """Review of c72dc3443: "He is 4" saved as child_age, which this
    agent's plan writes as <child_age>.  The scheduler's job counts 4
    repeats and says "Practise 4 words": a number the model wrote is the
    agent's, a whole JSON value as in its text, so both stay 4 (the bare
    one became the text "<child_age>" while the one in text stayed)."""
    from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType
    env = create_env
    led = SmartLedger(agent_id=str(PROMPT_ID), session_id=f'{UP}_0',
                      ledger_dir=str(env.prompts.parent))
    led.tasks['action_1'] = Task(task_id='action_1',
                                 description='ask the parent for <child_age>',
                                 task_type=TaskType.PRE_ASSIGNED)
    led.tasks['action_1'].status = TaskStatus.IN_PROGRESS
    env.lh.register_ledger_for_session(UP, led)
    try:
        assert env.lh.record_user_answer(UP, 1, 'He is 4', 0)
        monkeypatch.setitem(env.cr.final_recipe, PROMPT_ID,
                            {'status': 'completed', 'actions': []})
        chat = SimpleNamespace(messages=[
            {'role': 'assistant', 'name': 'Assistant',
             'content': 'How old is your child?'},
            {'role': 'user', 'name': 'UserProxy', 'content': 'He is 4'},
            {'role': 'assistant', 'name': 'Assistant', 'content': None,
             'tool_calls': [{'id': 'call_age', 'type': 'function', 'function': {
                 'name': 'save_data_in_memory',
                 'arguments': json.dumps({'key': 'child_age', 'value': 4})}}]},
            {'role': 'tool', 'name': 'Helper', 'content': 'Saved',
             'tool_responses': [{'tool_call_id': 'call_age', 'role': 'tool',
                                 'content': 'Saved'}]},
            {'role': 'user', 'name': 'ChatInstructor',
             'content': 'Reflect on the sequence of tasks and create '
                        'scheduled_tasks'},
            {'role': 'user', 'name': 'Assistant', 'content': json.dumps({
                'status': 'completed', 'dependency': [], 'recipe': '',
                'scheduled_tasks': [{
                    'cron_expression': '0 9 * * *', 'persona': 'Researcher',
                    'action_entry_point': 1, 'action_exit_point': 1,
                    'repeat_count': 4,
                    'job_description': 'Practise 4 words'}],
                'visual_scheduled_tasks': []})}])
        with env.app.app_context():
            env.cr._save_flow_recipe(0, PROMPT_ID, UP, USER_ID, chat)
    finally:
        with env.lh._state_lock:
            env.lh._ledger_registry.pop(UP, None)
    data = json.loads((env.prompts / f'{PROMPT_ID}_0_recipe.json')
                      .read_text(encoding='utf-8'))
    assert data.get('scheduled_tasks'), f'no job reached the flow recipe: {data}'
    job = data['scheduled_tasks'][0]
    assert job['repeat_count'] == 4, job
    assert job['job_description'] == 'Practise 4 words', job


def test_a_recipe_saved_from_the_recovered_chat_keeps_no_name(create_env,
                                                             monkeypatch, caplog):
    """When state_transition did not save the recipe (here its tool-name
    check raises, as any error in that handler does), the loop saves the
    model's reply from the chat it recovered ([FALLBACK-SAVE]; in the
    scripted loop that is the last step's re-request): the person's answer
    is its blank there too."""
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)

    def _raise(*a, **k):
        raise RuntimeError('the handler failed before its save')
    monkeypatch.setattr(env.cr, '_drop_unregistered_tool_names', _raise)
    _person_answers_at_step_one(env, [("What is your child's name?", 'Meena')],
                                saves=[('child_name', 'Meena')])
    _model_writes_recipe(
        env, step=3, recipe=[{'steps': 'Say hi to Meena', 'tool_name': '',
                              'generalized_functions': ''}])
    with caplog.at_level(logging.INFO):
        _run(env, turns=2)
    assert any('[FALLBACK-SAVE]' in r.getMessage() and '_0_3.json' in r.getMessage()
               for r in caplog.records), (
        'the recovered-chat writer never saved step 3')
    raw, data = _step_one(env, step=3)
    assert 'meena' not in raw.lower(), raw
    assert data['recipe'][0]['steps'] == 'Say hi to <child_name>', data['recipe']


_NAME_Q = "What is your child's name?"
_HOME_Q = 'What are the home names for mother and father?'
_PHOTOS = "Show Lakshmi's photo and ask the child who it is; then Ravi's."


def _saved_mark():
    from core.constants import outline_marker
    return outline_marker('str')


@pytest.mark.parametrize('question, answer, key, value, step, blanked', [
    # The demo speech agent (1794540972213, "Home names and photos") saves
    # the parents' home names under family_members.
    (_HOME_Q, 'Amma is Lakshmi, Appa is Ravi', 'family_members',
     [{'relation': 'Amma', 'name': 'Lakshmi'},
      {'relation': 'Appa', 'name': 'Ravi'}],
     _PHOTOS, "Show <family_members_name>'s photo and ask the child who it "
              "is; then <family_members_name>'s."),
    (_NAME_Q, 'Meena', 'kid_name', 'Meena', 'Say hi to Meena',
     'Say hi to <kid_name>'),
    (_NAME_Q, 'Meena', 'child', {'first_name': 'Meena'}, 'Say hi to Meena',
     'Say hi to <child_first_name>'),
    # <name> and <mother> are no blank's shape (an identifier joined by
    # '_'): a send would pass them on, so the saved mark instead.
    (_NAME_Q, 'Meena', 'name', 'Meena', 'Say hi to Meena', 'Say hi to {mark}'),
    ("What is the mother's name?", 'Lakshmi', 'mother', 'Lakshmi',
     'Show the photo and ask: is this Lakshmi?',
     'Show the photo and ask: is this {mark}?'),
], ids=['family_members', 'kid_name', 'child.first_name', 'name', 'mother'])
def test_a_name_saved_under_a_key_that_names_a_person_is_blanked(
        create_env, monkeypatch, question, answer, key, value, step, blanked):
    """Review rv_66reuse of 9f5fcd0ca: with only the exact person blanks
    known, a name the agent saved under any other key stayed in the step
    the model wrote, and the recipe file leaves the node (push_recipe, the
    peer export), so REUSE would show "Lakshmi" to another family's child.
    A key that names a person or a name says its value is a person's
    detail: the name is the key's blank, or the saved mark when the key is
    no blank's shape."""
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    _person_answers_at_step_one(env, [(question, answer)], saves=[(key, value)])
    _model_writes_recipe(env, recipe=[{'steps': step, 'tool_name': '',
                                       'generalized_functions': ''}])
    _run(env, turns=6)
    raw, data = _step_one(env)
    for name in ('meena', 'lakshmi', 'ravi'):
        assert name not in raw.lower(), raw
    assert data['recipe'][0]['steps'] == blanked.format(mark=_saved_mark()), (
        data['recipe'])


def test_names_saved_as_one_list_are_each_blanked(create_env, monkeypatch):
    """"Lakshmi, Ravi" saved as one text under family_names was blanked only
    where the whole text stood, so a step naming one parent kept the name.
    A person's detail that is a list is blanked name by name."""
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    _person_answers_at_step_one(env, [(_HOME_Q, 'Lakshmi, Ravi')],
                                saves=[('family_names', 'Lakshmi, Ravi')])
    _model_writes_recipe(env, recipe=[{'steps': _PHOTOS, 'tool_name': '',
                                       'generalized_functions': ''}])
    _run(env, turns=6)
    raw, data = _step_one(env)
    assert 'lakshmi' not in raw.lower() and 'ravi' not in raw.lower(), raw
    assert data['recipe'][0]['steps'] == (
        "Show <family_names>'s photo and ask the child who it is; then "
        "<family_names>'s."), data['recipe']


def test_relation_words_the_agent_said_first_stay(create_env, monkeypatch):
    """The agent asked for Amma's and Appa's names, so "Amma" and "Appa"
    are the lesson's words, not this family's: saved inside family_members
    with the names ("Amma: Lakshmi, Appa: Ravi"), they stay, while each
    name in it is blanked."""
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    _person_answers_at_step_one(
        env, [("What are Amma's and Appa's names at home?",
               'Amma is Lakshmi, Appa is Ravi')],
        saves=[('family_members', 'Amma: Lakshmi, Appa: Ravi')])
    _model_writes_recipe(env, recipe=[{
        'steps': "Show Amma's photo: is this Lakshmi? Then Appa's: is this Ravi?",
        'tool_name': '', 'generalized_functions': ''}])
    _run(env, turns=6)
    raw, data = _step_one(env)
    assert 'lakshmi' not in raw.lower() and 'ravi' not in raw.lower(), raw
    assert data['recipe'][0]['steps'] == (
        "Show Amma's photo: is this <family_members>? Then Appa's: is this "
        "<family_members>?"), data['recipe']


def test_a_name_saved_under_a_key_the_plan_writes_is_its_blank(create_env,
                                                               monkeypatch):
    """A blank this agent's own plan writes is a person's key too, though
    no word of it names a person: the plan asks for <favourite_toy>, the
    parent says "Teddy", and the scheduler's job says "Give Teddy to the
    child".  (A change that dropped the plan's blanks passed every test
    here before; review rv_66reuse.)"""
    from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType
    env = create_env
    led = SmartLedger(agent_id=str(PROMPT_ID), session_id=f'{UP}_0',
                      ledger_dir=str(env.prompts.parent))
    led.tasks['action_1'] = Task(
        task_id='action_1', description='ask the parent for <favourite_toy>',
        task_type=TaskType.PRE_ASSIGNED)
    led.tasks['action_1'].status = TaskStatus.IN_PROGRESS
    env.lh.register_ledger_for_session(UP, led)
    try:
        assert env.lh.record_user_answer(UP, 1, 'Teddy', 0)
        monkeypatch.setitem(env.cr.final_recipe, PROMPT_ID,
                            {'status': 'completed', 'actions': []})
        chat = SimpleNamespace(messages=[
            {'role': 'assistant', 'name': 'Assistant',
             'content': 'Which toy does your child love most?'},
            {'role': 'user', 'name': 'UserProxy', 'content': 'Teddy'},
            {'role': 'assistant', 'name': 'Assistant', 'content': None,
             'tool_calls': [{'id': 'call_toy', 'type': 'function', 'function': {
                 'name': 'save_data_in_memory',
                 'arguments': json.dumps({'key': 'favourite_toy',
                                          'value': 'Teddy'})}}]},
            {'role': 'tool', 'name': 'Helper', 'content': 'Saved',
             'tool_responses': [{'tool_call_id': 'call_toy', 'role': 'tool',
                                 'content': 'Saved'}]},
            {'role': 'user', 'name': 'ChatInstructor',
             'content': 'Reflect on the sequence of tasks and create '
                        'scheduled_tasks'},
            {'role': 'user', 'name': 'Assistant', 'content': json.dumps({
                'status': 'completed', 'dependency': [], 'recipe': '',
                'scheduled_tasks': [{
                    'cron_expression': '0 9 * * *', 'persona': 'Researcher',
                    'action_entry_point': 1, 'action_exit_point': 1,
                    'job_description': 'Give Teddy to the child'}],
                'visual_scheduled_tasks': []})}])
        with env.app.app_context():
            env.cr._save_flow_recipe(0, PROMPT_ID, UP, USER_ID, chat)
    finally:
        with env.lh._state_lock:
            env.lh._ledger_registry.pop(UP, None)
    raw = (env.prompts / f'{PROMPT_ID}_0_recipe.json').read_text(encoding='utf-8')
    data = json.loads(raw)
    assert data.get('scheduled_tasks'), f'no job reached the flow recipe: {data}'
    assert 'teddy' not in raw.lower(), raw
    assert (data['scheduled_tasks'][0]['job_description']
            == 'Give <favourite_toy> to the child'), data['scheduled_tasks']
