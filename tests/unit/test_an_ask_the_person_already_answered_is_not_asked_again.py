"""An ask the person already answered is not asked again, when its agent
chose "once" for it (hartos-52, with hartos-73's refinements).

The defect: the speech agent's Parent flow (1794540972213, flow 2) starts
with "ask the parent for home Tamil names and photos" and the next step saves
them under family_members.  Every time the parent came back, the walk started
at that ask and asked for the names already saved.

Owner, 2026-11-16: "to a kid asking repeatedly is a feature to a parent it's
spam ... the framework shd be supporting both mechanisms ... controlled by the
flow action in agents"; the repeat point is the agent's, decided at CREATE
and stored with the recipe.  Owner, 2026-11-19: "if the new request answers
the old question or agent got the response by some other means do not ask
the question again ... no false positives or false negatives".

So a recipe step may carry repeat "once" or "every_run" (CREATE's model picks
it).  A "once" ask whose following save step's keys all hold an answer in
THIS person's memory, read as get_data_by_key reads it, skips its question;
the save step still runs with the person's words and the known values, so a
correction is saved.  No field, or "every_run", asks exactly as before.
Agents built before the field get a sidecar classification.

One gate, HEVOLVE_ASK_REPEAT_ONCE, off by default, decides whether any "once"
is honoured, authored or from the sidecar: scored blind on 20 live ask steps
the 4B's answers were right on 9, and all 11 errors were a "once" that would
drop a question still needed.  Off, an authored "once" asks exactly as today.
The tests of the designed behaviour run with the gate on (the fixture).
"""
import json
from types import SimpleNamespace

import pytest

import hartos.lifecycle_hooks as lh
from core import ask_repeat
from core.ask_repeat import EVERY_RUN, ONCE

P = '1794540972213'
PARENT = f'parenta_{P}'
OTHER_PARENT = f'parentb_{P}'
CHILD = f'childx_{P}'
ASK_TEXT = ('send_message_to_user to parent asking for home Tamil names for '
            'mother/father and request to show photos')
NAMES = ['Amma', 'Appa']
TOOLS = ['send_message_to_user', 'save_data_in_memory', 'get_data_by_key']
#: The one gate of every "once" (core.ask_repeat.ONCE_FLAG).
GATE = 'HEVOLVE_ASK_REPEAT_ONCE'


def _save_step(*keys):
    return {'action': f"save_data_in_memory key '{keys[0]}'",
            'can_perform_without_user_input': 'yes',
            'recipe': [{'steps': f'save_data_in_memory({json.dumps({"key": k, "value": [], "for_everyone": False})})',
                        'tool_name': 'save_data_in_memory',
                        'generalized_functions': '',
                        'agent_to_perform_this_action': 'Helper'} for k in keys]}


def _ask_step(**fields):
    step = {'action': ASK_TEXT,
            'can_perform_without_user_input': 'no - the person answers this step',
            'recipe': [{'steps': 'send_message_to_user({"text": "What do you call mother and father at home?"})',
                        'tool_name': 'send_message_to_user',
                        'generalized_functions': '',
                        'agent_to_perform_this_action': 'Helper'}]}
    step.update(fields)
    return step


NOTE = {'action': 'send_message_to_user a progress note',
        'can_perform_without_user_input': 'yes',
        'recipe': [{'steps': 'send_message_to_user({"text": "progress"})',
                    'tool_name': 'send_message_to_user'}]}


def _flow(ask=None, save=None):
    return [ask if ask is not None else _ask_step(repeat='once'),
            save if save is not None else _save_step('family_members'), dict(NOTE)]


class _Agent:
    def __init__(self, name):
        self.name = name
        self._function_map = {t: (lambda **kw: None) for t in TOOLS}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in TOOLS]}
        self._oai_messages = {}


class _Chat:
    def __init__(self):
        self.messages = []
        self.agents = [_Agent('Helper'), _Agent('Assistant')]


class _Task:
    def __init__(self, actions, current=1):
        self.actions = list(actions)
        self.current_action = current
        self.evidence_seen_call_ids = set()

    def get_action(self, idx):
        return self.actions[idx]


@pytest.fixture
def reuse(monkeypatch, tmp_path):
    """One person's REUSE session on a three-step flow, with an in-test
    action state machine and memory store, so nothing reaches a ledger, a
    model or the disk outside tmp_path."""
    flask = pytest.importorskip('flask')
    import hartos.reuse_recipe as rr
    from core import platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    monkeypatch.setenv(GATE, '1')
    app = flask.Flask('ask_once')
    store = {}
    monkeypatch.setattr(rr, 'agent_data', store)
    monkeypatch.setattr(rr.helper_fun, 'load_agent_data_from_file', lambda *a, **k: None)
    chat = _Chat()
    monkeypatch.setattr(rr, 'get_registered_groupchat', lambda _s: chat)
    monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda *a, **k: 0)
    opened = {}
    monkeypatch.setattr(rr, 'ledger_open_ask', lambda up, aid, flow: opened.get((up, aid)))
    for name in ('_reuse_camera_window', '_reuse_voice_window'):
        monkeypatch.setattr(rr, name, lambda *a, **k: '')
    monkeypatch.setattr(rr, '_reuse_request_is_a_person', lambda _r: True)
    states, writes, starts = {}, [], []

    def _get(up, aid):
        return states.get((up, aid), lh.ActionState.ASSIGNED)

    def _set(up, aid, state, why='', result=None):
        writes.append((up, aid, state, result))
        states[(up, aid)] = state
        return True

    def _start(up, aid, why=''):
        starts.append((up, aid))
        states[(up, aid)] = lh.ActionState.IN_PROGRESS
        return True
    monkeypatch.setattr(rr, 'get_action_state', _get)
    monkeypatch.setattr(rr, 'safe_set_state', _set)
    monkeypatch.setattr(rr, '_start_reuse_action', _start)
    monkeypatch.setattr(rr, '_stamp_action_evidence_watermark', lambda *a, **k: None)

    def _session(key, actions, current=1, rid='turn_1'):
        monkeypatch.setitem(rr.recipes, key, {'actions': actions})
        monkeypatch.setitem(rr.user_tasks, key, _Task(actions, current))
        monkeypatch.setitem(rr.request_id_list, key, rid)
        rr._reuse_passed_asks.pop(key, None)
        rr._reuse_pending_reply.pop(key, None)
        states[(key, current)] = lh.ActionState.IN_PROGRESS

    def _seed(key, words):
        with app.app_context():
            return rr._reuse_seed_message(key, words, P)

    def _own(key):
        from core.agent_tools import person_store_id
        return store.setdefault(person_store_id(key), {})
    return SimpleNamespace(rr=rr, app=app, store=store, own=_own, chat=chat,
                           states=states, writes=writes, starts=starts,
                           opened=opened, session=_session, seed=_seed)


def _dispatched(text):
    return lh.dispatch_action_id(text)


def _asked(run, key=PARENT):
    """The ask step's question went out: its dispatch was the turn's."""
    return run.rr.user_tasks[key].current_action == 1


# -- the "once" decision -----------------------------------------------------

def test_once_with_every_key_held_completes_as_already_known_and_sends_nothing(reuse):
    reuse.session(PARENT, _flow())
    reuse.own(PARENT)['family_members'] = list(NAMES)
    seed = reuse.seed(PARENT, 'hello again')
    assert _dispatched(seed) == 2, seed
    assert reuse.rr.user_tasks[PARENT].current_action == 2
    closed = [(s.name, r) for up, aid, s, r in reuse.writes if aid == 1]
    assert closed == [('STATUS_VERIFICATION_REQUESTED', None),
                      ('COMPLETED', 'already known (family_members)'),
                      ('TERMINATED', None)], closed
    assert reuse.states[(PARENT, 1)] == lh.ActionState.TERMINATED
    assert (PARENT, 2) in reuse.starts
    # Nothing was sent and no question is in the conversation: the only
    # thing going out is action 2's dispatch.
    assert reuse.chat.messages == []
    assert ASK_TEXT not in seed


def test_the_save_step_runs_with_the_persons_words_and_the_known_values(reuse):
    reuse.session(PARENT, _flow())
    reuse.own(PARENT)['family_members'] = list(NAMES)
    seed = reuse.seed(PARENT, 'add Paati too')
    head = seed.split('\n\n')
    assert head[0].startswith('Perform this action -> Action #2:'), seed
    assert 'add Paati too' in seed
    assert 'family_members = ["Amma", "Appa"]' in seed
    assert 'step 1 was not asked again' in seed


def test_a_correction_while_already_known_ends_with_the_new_value_saved(reuse):
    """hartos-73, refinement 5: when already known, only the question is
    skipped.  The turn's words reach the step that saves, which is started,
    so the agent's save of the correction lands."""
    reuse.session(PARENT, _flow())
    reuse.own(PARENT)['family_members'] = list(NAMES)
    seed = reuse.seed(PARENT, 'actually her name is Meena, not Amma')
    assert _dispatched(seed) == 2 and 'actually her name is Meena' in seed
    assert reuse.states[(PARENT, 2)] == lh.ActionState.IN_PROGRESS
    # The save step's own call, as the agent makes it on that dispatch.
    reuse.own(PARENT)['family_members'] = ['Meena', 'Appa']
    assert reuse.own(PARENT)['family_members'] == ['Meena', 'Appa']
    # The next run sees the corrected value as the answer on record.
    reuse.session(PARENT, _flow())
    seed = reuse.seed(PARENT, 'hi')
    assert '"Meena"' in seed and _dispatched(seed) == 2


def test_an_open_question_of_the_step_takes_the_words_as_its_answer(reuse):
    """The person's words answer an open ask of this step: processed as the
    answer, never passed for the stale memory."""
    reuse.session(PARENT, _flow(), rid='reply_turn')
    reuse.own(PARENT)['family_members'] = list(NAMES)
    reuse.opened[(PARENT, 1)] = {'request_id': 'ask_turn', 'via': 'send',
                                 'question': 'What do you call them?',
                                 'asked': 'What do you call them?'}
    seed = reuse.seed(PARENT, 'add Paati too')
    assert _dispatched(seed) == 1, seed
    assert 'add Paati too' in seed and 'What do you call them?' in seed
    assert not [w for w in reuse.writes if w[1] == 1]


def test_once_with_one_of_two_keys_missing_asks(reuse):
    reuse.session(PARENT, _flow(save=_save_step('family_members', 'family_photos')))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    seed = reuse.seed(PARENT, 'hello again')
    assert _dispatched(seed) == 1 and _asked(reuse), seed
    assert not reuse.writes


def test_once_with_both_keys_held_is_known(reuse):
    reuse.session(PARENT, _flow(save=_save_step('family_members', 'family_photos')))
    reuse.own(PARENT).update(family_members=list(NAMES), family_photos=['p1.jpg'])
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 2


@pytest.mark.parametrize('value', [
    None, '', '   ', [], {}, '[]', '{}', 'null', 'unknown', 'Unknown.', 'N/A', 'na',
    'TBD', 'pending', 'later', 'skip', 'not provided', '-', '?', "I don't know",
    'I don’t know', 'தெரியாது', 'அப்புறம்', '<name>', '{name}', "user's name",
    ASK_TEXT, 'Key not found in stored data.', 'key not found',
    'Saved at family_members: Amma', 'Error: refused', ['unknown', ''],
    {'mother': '', 'father': 'n/a'},
])
def test_once_with_a_placeholder_value_asks(reuse, value):
    reuse.session(PARENT, _flow())
    reuse.own(PARENT)['family_members'] = value
    seed = reuse.seed(PARENT, 'hello again')
    assert _dispatched(seed) == 1 and _asked(reuse), (value, seed)


def test_none_is_a_placeholder_only_for_a_name_key(reuse):
    assert ask_repeat.is_placeholder('none', 'child_name')
    assert ask_repeat.is_placeholder('None', 'familyNames')
    assert not ask_repeat.is_placeholder('none', 'allergies')
    reuse.session(PARENT, _flow(save=_save_step('allergies')))
    reuse.own(PARENT)['allergies'] = 'none'
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 2
    reuse.session(PARENT, _flow(save=_save_step('child_name')))
    reuse.own(PARENT)['child_name'] = 'none'
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 1


def test_every_run_asks_even_with_the_keys_held(reuse):
    reuse.session(PARENT, _flow(ask=_ask_step(repeat='every_run')))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 1 and _asked(reuse)


@pytest.mark.parametrize('fields', [{}, {'repeat': ''}, {'repeat': None}, {'repeat': 'yes'}])
def test_no_field_or_any_other_value_asks_as_before(reuse, fields):
    reuse.session(PARENT, _flow(ask=_ask_step(**fields)))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    seed = reuse.seed(PARENT, 'hello again')
    assert _dispatched(seed) == 1 and not reuse.writes, (fields, seed)


def test_once_is_read_whatever_its_case(reuse):
    reuse.session(PARENT, _flow(ask=_ask_step(repeat=' Once ')))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 2


def test_a_step_that_does_not_wait_for_the_person_is_never_passed(reuse):
    reuse.session(PARENT, _flow(ask=_ask_step(repeat='once',
                                              can_perform_without_user_input='yes')))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 1


def test_an_ask_followed_by_no_save_step_asks(reuse):
    reuse.session(PARENT, [_ask_step(repeat='once'), dict(NOTE), dict(NOTE)])
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 1


def test_a_save_whose_key_is_still_a_template_asks(reuse):
    reuse.session(PARENT, _flow(save=_save_step('family.<child_name>')))
    reuse.own(PARENT)['family'] = {'x': 'Amma'}
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 1


def test_one_unreadable_save_key_beside_a_held_one_asks(reuse):
    """What the ask collects is not known when any of its save's keys cannot
    be read, so the keys that can be read are not enough."""
    reuse.session(PARENT, _flow(save=_save_step('family_members', 'family.<child_name>')))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 1
    save = _save_step('family_members')
    save['recipe'].append({'steps': 'save_data_in_memory(the photos)',
                           'tool_name': 'save_data_in_memory'})
    reuse.session(PARENT, _flow(save=save))
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 1


def test_a_step_naming_another_tool_is_never_passed(reuse):
    ask = _ask_step(repeat='once')
    ask['recipe'].append({'steps': 'get_data_by_key({"key": "family_members"})',
                          'tool_name': 'get_data_by_key'})
    reuse.session(PARENT, _flow(ask=ask))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 1


# -- whose memory counts -------------------------------------------------------

def test_what_the_parent_saved_does_not_count_on_the_childs_turn(reuse):
    """'This person' is the account on the turn: the session key
    "{user_id}_{prompt_id}" and its own store (person_store_id).  The child's
    account has its own store; the parent's saves are not in it."""
    reuse.own(PARENT)['family_members'] = list(NAMES)
    reuse.session(CHILD, _flow())
    seed = reuse.seed(CHILD, 'hi')
    assert _dispatched(seed) == 1 and _asked(reuse, CHILD), seed


def test_what_one_parent_saved_does_not_count_for_another_parent(reuse):
    reuse.own(PARENT)['family_members'] = list(NAMES)
    reuse.session(OTHER_PARENT, _flow())
    assert _dispatched(reuse.seed(OTHER_PARENT, 'hello')) == 1


def test_memory_is_read_the_way_get_data_by_key_reads_it(reuse):
    """One read path (core.agent_tools.memory_view): the person's own entry
    over what the agent holds for everyone, key by key."""
    reuse.store[P] = {'family_members': list(NAMES)}
    reuse.session(PARENT, _flow())
    assert _dispatched(reuse.seed(PARENT, 'hello')) == 2
    reuse.session(PARENT, _flow())
    reuse.own(PARENT)['family_members'] = 'unknown'
    assert _dispatched(reuse.seed(PARENT, 'hello')) == 1


# -- the advance path ------------------------------------------------------------

def test_an_advance_onto_a_known_ask_passes_it_to_the_save_step(reuse, monkeypatch):
    rr = reuse.rr
    actions = [dict(NOTE, action='send_message_to_user a greeting')] + _flow()
    reuse.session(PARENT, actions)
    reuse.own(PARENT)['family_members'] = list(NAMES)
    monkeypatch.setattr(rr, '_reuse_person_turn', lambda up: True)
    monkeypatch.setattr(rr, '_reuse_fabricated_tools', lambda *a, **k: [])
    monkeypatch.setattr(rr, '_reuse_ask_only_step', lambda *a, **k: False)
    monkeypatch.setattr(rr, '_reuse_action_declares_tool', lambda *a, **k: True)
    monkeypatch.setattr(rr, '_reuse_completion_evidence',
                        lambda *a, **k: {'kind': 'tool_receipt', 'message_index': 0})
    monkeypatch.setattr(rr, 'verdict_names_another_action', lambda *a, **k: None)
    monkeypatch.setattr(rr, '_reuse_steers_spent', lambda *a, **k: False)
    monkeypatch.setattr(rr, '_reuse_early_send_terms', lambda *a, **k: {})
    monkeypatch.setattr(rr, '_stamp_action_result_vacuity', lambda *a, **k: None)

    def _commit(up, aid, ev, why, **k):
        reuse.states[(up, aid)] = lh.ActionState.COMPLETED
        return True
    monkeypatch.setattr(rr, 'commit_verified_action_completion', _commit)
    with reuse.app.app_context():
        got = rr._advance_reuse_action(PARENT, 1, 'test', P)
        message = rr._build_reuse_action_message(PARENT, got[0])
    assert got == (3, True)
    assert rr.user_tasks[PARENT].current_action == 3
    assert reuse.states[(PARENT, 2)] == lh.ActionState.TERMINATED
    assert _dispatched(message) == 3 and 'family_members = ["Amma", "Appa"]' in message


def test_a_dispatch_of_any_other_step_is_unchanged(reuse):
    reuse.session(PARENT, _flow())
    with reuse.app.app_context():
        message = reuse.rr._build_reuse_action_message(PARENT, 2)
    assert 'Already saved' not in message


# -- the one gate -------------------------------------------------------------------

@pytest.mark.parametrize('value', [None, '', '0', 'false', 'off', 'junk'])
def test_an_authored_once_with_the_gate_off_asks_exactly_as_today(reuse, monkeypatch, value):
    """Gate off (unset, falsy or junk): the turn is byte-for-byte the turn of
    the same step with no repeat field, nothing is closed and nothing of the
    person's memory is read."""
    if value is None:
        monkeypatch.delenv(GATE)
    else:
        monkeypatch.setenv(GATE, value)
    reuse.session(PARENT, _flow(ask=_ask_step()))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    today = reuse.seed(PARENT, 'hello again')
    reads = []
    monkeypatch.setattr(ask_repeat, 'held_answers',
                        lambda *a, **k: reads.append(a) or {'family_members': NAMES})
    reuse.session(PARENT, _flow())
    seed = reuse.seed(PARENT, 'hello again')
    assert seed == today and _dispatched(seed) == 1 and _asked(reuse)
    assert not reuse.writes and not reads
    assert not reuse.rr._reuse_passed_asks.get(PARENT)
    with reuse.app.app_context():
        assert reuse.rr._reuse_pass_known_ask(PARENT, 1, P) == 1


def test_there_is_one_gate_and_it_is_off_by_default(monkeypatch):
    assert ask_repeat.ONCE_FLAG == GATE
    monkeypatch.delenv(GATE, raising=False)
    assert ask_repeat.once_enabled() is False
    assert not [n for n in dir(ask_repeat) if n.endswith('_FLAG') and n != 'ONCE_FLAG']


def test_the_same_once_is_honoured_once_the_gate_is_on(reuse, monkeypatch):
    monkeypatch.setenv(GATE, 'true')
    reuse.session(PARENT, _flow())
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello again')) == 2


def test_an_advance_onto_an_authored_once_asks_while_the_gate_is_off(reuse, monkeypatch):
    monkeypatch.delenv(GATE)
    reuse.session(PARENT, _flow())
    reuse.own(PARENT)['family_members'] = list(NAMES)
    with reuse.app.app_context():
        assert reuse.rr._reuse_known_ask(PARENT, 1, P) is None


# -- the sidecar (agents built before the field) -----------------------------------

def _sidecar(tmp_path, entry):
    path = tmp_path / 'ask_repeat' / f'{P}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'0': {'1': entry}}), encoding='utf-8')


def test_the_sidecar_is_not_read_while_it_is_off(reuse, tmp_path, monkeypatch):
    monkeypatch.delenv(GATE)
    _sidecar(tmp_path, {'repeat': ONCE})
    reads = []
    real = ask_repeat._sidecar_read
    monkeypatch.setattr(ask_repeat, '_sidecar_read', lambda pid: reads.append(pid) or real(pid))
    reuse.session(PARENT, _flow(ask=_ask_step()))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello')) == 1
    # Its reader keeps the gate too, for any caller that is not REUSE.
    assert ask_repeat.sidecar_repeat(P, 0, 1) is None
    assert reads == []


def test_the_sidecar_once_is_honoured_while_it_is_on(reuse, tmp_path, monkeypatch):
    _sidecar(tmp_path, {'repeat': ONCE})
    monkeypatch.setenv(GATE, '1')
    reuse.session(PARENT, _flow(ask=_ask_step()))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello')) == 2
    reuse.session(PARENT, _flow(ask=_ask_step()))
    reuse.own(PARENT)['family_members'] = 'unknown'
    assert _dispatched(reuse.seed(PARENT, 'hello')) == 1


def test_the_recipe_field_wins_over_the_sidecar(reuse, tmp_path, monkeypatch):
    _sidecar(tmp_path, {'repeat': ONCE})
    monkeypatch.setenv(GATE, '1')
    reuse.session(PARENT, _flow(ask=_ask_step(repeat='every_run')))
    reuse.own(PARENT)['family_members'] = list(NAMES)
    assert _dispatched(reuse.seed(PARENT, 'hello')) == 1


@pytest.mark.parametrize('reply, expected', [
    ({'repeat': 'once', 'reason': 'names do not change'}, ONCE),
    ({'repeat': 'every_run', 'reason': 'a drill'}, EVERY_RUN),
    ({'repeat': 'uncertain', 'reason': 'cannot tell'}, EVERY_RUN),
    ({'repeat': 'sometimes', 'reason': ''}, EVERY_RUN),
    ({}, EVERY_RUN),
    (None, EVERY_RUN),
])
def test_only_the_models_once_is_once(reply, expected):
    judged = ask_repeat.classify_step(ASK_TEXT, 'Update the parent', ask=lambda *a: reply)
    assert judged['repeat'] == expected


def test_the_backfill_writes_only_the_sidecar(tmp_path, monkeypatch):
    from core import platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    flows = [{'sub_goal': 'Update parent with the naming knowledge.'}]
    recipe = {0: [_ask_step(), _save_step('family_members'),
                  _ask_step(repeat='every_run'), dict(NOTE)]}
    before = json.dumps(recipe, sort_keys=True)
    asked = []
    n = ask_repeat.backfill(P, flows, recipe,
                            ask=lambda content, schema, name: asked.append(content)
                            or {'repeat': 'once', 'reason': 'kept'})
    assert n == 1 and len(asked) == 1 and 'Update parent' in asked[0]
    assert json.dumps(recipe, sort_keys=True) == before
    saved = json.loads((tmp_path / 'ask_repeat' / f'{P}.json').read_text(encoding='utf-8'))
    assert saved['0']['1']['repeat'] == ONCE and set(saved['0']) == {'1'}
    assert ask_repeat.backfill(P, flows, recipe, ask=lambda *a: 1 / 0) == 0


# -- CREATE --------------------------------------------------------------------------

@pytest.fixture
def create(monkeypatch):
    import hartos.create_recipe as cr
    key = f'creator_{P}'

    class _CreateTask:
        current_action = 1
        recipe = fallback = False

        def get_action(self, idx):
            return 'Ask the parent what they call mother and father at home'
    monkeypatch.setitem(cr.user_tasks, key, _CreateTask())
    monkeypatch.setattr(cr, 'safe_set_state', lambda *a, **k: True)
    import core.agent_tools as at
    monkeypatch.setattr(at, 'memory_view', lambda *a, **k: {})
    return SimpleNamespace(cr=cr, key=key)


def test_create_offers_the_repeat_field_with_its_meaning(create):
    field = f'"repeat":"{ask_repeat.REPEAT_FIELD_MEANING}"'
    first = create.cr.request_recipe_for_action(1, P, 'Parent', create.key)
    last = create.cr.request_recipe_for_action_last(1, P, 'Parent', create.key)
    assert field in first and field in last
    # The field is offered, never chosen for the model.
    for text in (first, last):
        assert 'repeat' not in text.replace(field, '')


def test_an_authored_action_carries_repeat_and_its_absence_is_tolerated(create, tmp_path, monkeypatch):
    cr = create.cr
    authored = dict(_ask_step(repeat='once'), action_id=1, status='done')
    plain = dict(_save_step('family_members'), action_id=2, status='done')
    for i, action in ((1, authored), (2, plain)):
        (tmp_path / f'{P}_0_{i}.json').write_text(json.dumps(action), encoding='utf-8')
    monkeypatch.setattr(cr.helper_fun, 'safe_prompt_path',
                        lambda pid, flow=None, n=None: str(tmp_path / f'{pid}_{flow}_{n}.json'))

    class _Two:
        actions = [authored['action'], plain['action']]
    monkeypatch.setitem(cr.user_tasks, create.key, _Two())
    out = []
    import flask
    with flask.Flask('ask_once_create').app_context():
        cr.set_individual_recipes(0, out, P, create.key)
    assert [a.get('repeat') for a in out] == ['once', None]
    assert ask_repeat.authored_repeat(out[0]) == ONCE
    assert ask_repeat.authored_repeat(out[1]) is None
