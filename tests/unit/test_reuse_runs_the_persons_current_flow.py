"""REUSE runs the flow a person is on, and moves them when its goal is met.

Owner, 2026-11-03: "When its goal is met".  Owner, 2026-11-06: the persona is
asked at a person's first use of the agent "so that appropriate persona and
corresponding flow gets associated at first time agent use".  Before this,
nothing kept a person's place: get_flow_number always took the persona's
FIRST flow, the persona pick lived only in 2 h caches, and a warm session
(user_agents hit) never re-read anything, so a child replayed flow 0 from
action 1 every day.  hartos-7a's review (11-04) named each wiring point; the
independent review of the first version (f9951c0d2, 11-06) found that four
of them had no test that fails without them, and that a one-persona agent
never progressed.  The four call sites are driven through their real callers
here: _advance_reuse_action, chat_agent (twice) and the role group's
update_persona.

The real reuse_recipe helpers and the real core.flow_progress store on a temp
data dir; the model and the ledger are stand-ins.

    python -m pytest tests/unit/test_reuse_runs_the_persons_current_flow.py -q
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

FLOWS = [
    {'flow_name': 'Name calling response', 'persona': 'Child', 'sub_goal': 'Arjun turns when called'},
    {'flow_name': 'Family photos', 'persona': 'Parent', 'sub_goal': 'three photos sent'},
    {'flow_name': 'Family member association', 'persona': 'Child', 'sub_goal': 'Arjun names Amma'},
]
PERSONAS = ['Child', 'Parent']
U, P, KEY = 'u-child', 77, 'u-child_77'
CACHES = ('user_agents', 'user_tasks', 'user_ledgers', 'recipes', 'time_actions')


class _Stop(Exception):
    """Where a driven caller is stopped once the part under test has run."""


def _prompt(tmp_path, personas=PERSONAS, flows=FLOWS):
    prompt = tmp_path / f'{P}.json'
    prompt.write_text(json.dumps({'personas': [{'name': n} for n in personas], 'flows': flows}),
                      encoding='utf-8')
    return prompt


@pytest.fixture
def rr(monkeypatch, tmp_path):
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    from hartos import reuse_recipe as rr
    for name in CACHES + ('request_id_list', 'agents_session', 'chat_joinees', 'user_journey',
                          'user_delegation_bridges', 'llm_call_track', 'role_agents'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    prompt = _prompt(tmp_path)
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    with Flask('reuse-flow-test').app_context():
        yield rr


def _warm(rr, monkeypatch, loaded_flow=0, person=True):
    """A warm session built for ``loaded_flow`` on a person's (or a daemon's) turn."""
    for name in CACHES:
        getattr(rr, name)[KEY] = object()
    monkeypatch.setattr(rr, '_reuse_person_turn', lambda _up: person)
    monkeypatch.setattr(rr, '_reuse_session_ledger', lambda _up: object())
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda _up, _aid: loaded_flow)


def _model(monkeypatch, met=True):
    import core.llm_json as llm_json
    asked = []

    def ask(content, schema, name, **_k):
        asked.append(name)
        return {'flow_goal_checkable': {'checkable': True},
                'flow_goal_met': {'met': met, 'evidence': 'he turned'},
                'flow_weakest': {'flow': ''}}[name]
    monkeypatch.setattr(llm_json, 'ask_model_json', ask)
    return asked


def _finish(flow=0, persona='Child', observed='step 2 result: he turned'):
    from core.flow_progress import record_flow_finished
    record_flow_finished(U, P, flow, persona, 'run-1', observed, 'step 2: call Arjun')


def test_a_persons_pick_beats_the_list_a_create_registered(rr):
    from core.flow_progress import set_persona
    # After a CREATE in this process the creator holds the whole persona list.
    rr.agents_session[KEY] = [{'user_id': U, 'role': 'Parent'}, {'user_id': U, 'role': 'Child'}]
    assert rr.get_role(U, P) == 'Parent'
    set_persona(U, P, 'Child', PERSONAS)
    assert rr.get_role(U, P) == 'Child'


def test_a_pick_survives_the_caches(rr):
    from core.flow_progress import set_persona
    set_persona(U, P, 'Parent', PERSONAS)
    assert rr.get_role(U, P) == 'Parent'             # nothing in agents_session at all


def test_the_builders_read_the_persons_place(rr):
    from core.flow_progress import set_persona, set_person_flow
    set_persona(U, P, 'Child', PERSONAS)
    assert rr.get_flow_number(U, P) == (0, 'Child')
    set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')
    assert rr.get_flow_number(U, P) == (2, 'Child')


def test_a_one_persona_agent_progresses_after_a_restart(rr, monkeypatch, tmp_path):
    """Review of f9951c0d2, the critical finding: a one-persona agent never
    asks the persona, so get_role reads 'user'; its finished run was dropped
    unjudged and its place never read."""
    from core.flow_progress import current_flow
    flows = [dict(FLOWS[0]), dict(FLOWS[2])]
    prompt = _prompt(tmp_path, personas=['Child'], flows=flows)
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    _finish(flow=0)
    _warm(rr, monkeypatch, loaded_flow=0)
    asked = _model(monkeypatch, met=True)
    rr._reuse_settle_person_flow(U, P, KEY)            # no pick, empty agents_session
    assert asked == ['flow_goal_checkable', 'flow_goal_met']
    assert current_flow(U, P, flows, 'Child') == 1
    assert rr.get_flow_number(U, P)[0] == 1


def test_a_met_goal_rebuilds_the_warm_session_for_the_next_flow(rr, monkeypatch):
    from core.flow_progress import set_persona, current_flow
    set_persona(U, P, 'Child', PERSONAS)
    _finish()
    _warm(rr, monkeypatch, loaded_flow=0)
    rr.user_delegation_bridges[KEY] = object()
    rr._reuse_pending_counts[(KEY, 1)] = 2
    rr.user_journey[KEY] = 'UseBot'
    asked = _model(monkeypatch, met=True)
    rr._reuse_settle_person_flow(U, P, KEY)
    assert asked == ['flow_goal_checkable', 'flow_goal_met']
    assert current_flow(U, P, FLOWS, 'Child') == 2
    assert all(KEY not in getattr(rr, name) for name in CACHES + ('user_delegation_bridges',))
    assert (KEY, 1) not in rr._reuse_pending_counts
    assert rr.user_journey[KEY] == 'UseBot'           # the persona is not asked again


def test_a_goal_not_met_keeps_the_warm_session(rr, monkeypatch):
    from core.flow_progress import set_persona
    set_persona(U, P, 'Child', PERSONAS)
    _finish(observed='step 2 result: no response')
    _warm(rr, monkeypatch, loaded_flow=0)
    _model(monkeypatch, met=False)
    rr._reuse_settle_person_flow(U, P, KEY)
    assert all(KEY in getattr(rr, name) for name in CACHES)


def test_a_move_by_someone_else_reaches_a_warm_session_without_asking(rr, monkeypatch):
    from core.flow_progress import set_persona, set_person_flow
    set_persona(U, P, 'Child', PERSONAS)
    set_person_flow(U, P, FLOWS, 'Child', 2, 'therapist-7')
    _warm(rr, monkeypatch, loaded_flow=0)
    asked = _model(monkeypatch)
    rr._reuse_settle_person_flow(U, P, KEY)
    assert asked == []                                # nothing to judge: no model call
    assert all(KEY not in getattr(rr, name) for name in CACHES)


def test_a_daemon_turn_neither_judges_nor_rebuilds(rr, monkeypatch):
    from core.flow_progress import set_persona, read
    set_persona(U, P, 'Child', PERSONAS)
    _finish()
    _warm(rr, monkeypatch, loaded_flow=0, person=False)
    asked = _model(monkeypatch)
    rr._reuse_settle_person_flow(U, P, KEY)
    assert asked == [] and len(read(U, P)['unjudged']) == 1
    assert all(KEY in getattr(rr, name) for name in CACHES)


def test_a_finished_flow_is_kept_only_from_a_persons_turn(rr, monkeypatch):
    from core.flow_progress import read
    rr.request_id_list[KEY] = 'msg_1_child'
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda _up, _aid: 2)
    monkeypatch.setattr(rr, '_reuse_flow_evidence',
                        lambda _up, flow: (f'seen in flow {flow}', f'steps of flow {flow}'))
    monkeypatch.setattr(rr, '_reuse_person_turn', lambda _up: False)
    rr._reuse_note_flow_finished(KEY, P)
    assert 'unjudged' not in read(U, P)
    monkeypatch.setattr(rr, '_reuse_person_turn', lambda _up: True)
    rr._reuse_note_flow_finished(KEY, P)
    record = read(U, P)['unjudged'][-1]
    assert (record['run'], record['flow'], record['persona']) == ('msg_1_child', 2, 'Child')
    assert (record['observed'], record['steps']) == ('seen in flow 2', 'steps of flow 2')


def test_the_evidence_keeps_what_happened_apart_from_what_the_agent_meant_to_do(rr, monkeypatch):
    import hartos.lifecycle_hooks as lifecycle_hooks
    ledger = SimpleNamespace(tasks={
        'action_2': SimpleNamespace(description='check that Arjun turns when called', result='no response'),
        'action_1': SimpleNamespace(description='call Arjun by name', result='sent: Arjun!'),
        'action_3': SimpleNamespace(description="ask for the parent's photos", result='photos?'),
        'action_4': SimpleNamespace(description='not done yet', result=None),
    })
    monkeypatch.setattr(rr, '_reuse_session_ledger', lambda _up: ledger)
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda _up, aid: 1 if aid == 3 else 0)
    monkeypatch.setattr(lifecycle_hooks, 'recorded_answers',
                        lambda _up, aid, flow: ['he looked away'] if aid == 2 else [])
    observed, steps = rr._reuse_flow_evidence(KEY, 0)
    assert observed.splitlines() == ['step 1 result: sent: Arjun!',
                                     'person answered at step 2: he looked away',
                                     'step 2 result: no response']
    assert 'turns when called' not in observed
    assert steps.splitlines() == ['step 1: call Arjun by name',
                                  'step 2: check that Arjun turns when called',
                                  'step 4: not done yet']


def test_a_step_filed_after_a_move_lands_in_the_flow_that_made_it(rr, monkeypatch):
    from core.flow_progress import set_persona, set_person_flow
    set_persona(U, P, 'Child', PERSONAS)
    set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')
    _warm(rr, monkeypatch, loaded_flow=0)
    assert rr._reuse_run_flow(U, P) == 0              # the run, not the new place
    monkeypatch.setattr(rr, '_reuse_session_ledger', lambda _up: None)
    assert rr._reuse_run_flow(U, P) == 2              # no run: the person's place


# The four call sites, each through its real caller (review of f9951c0d2).

def test_the_last_action_completed_keeps_the_finished_flow(rr, monkeypatch):
    """_advance_reuse_action past the last action records the finished flow."""
    import hartos.lifecycle_hooks as lifecycle_hooks
    recipe = {'actions': [{'action': 'Call Arjun by name', 'recipe': [{'steps': 'call', 'tool_name': ''}]},
                          {'action': 'Tell Arjun well done', 'recipe': [{'steps': 'say', 'tool_name': ''}]}]}
    chat = SimpleNamespace(agents=[], messages=[
        {'role': 'user', 'name': 'Assistant',
         'content': ("Perform this action -> Action #2:Tell Arjun well done.\n follow these "
                     "steps: [{'say': {'tool_name': '', 'code': ''}}]")},
        {'role': 'assistant', 'name': 'Assistant', 'content': 'Arjun, well done for turning round!'},
        {'role': 'user', 'name': 'StatusVerifier',
         'content': ('{"status":"completed","action":"Tell Arjun well done","action_id":2,'
                     '"message":"Praised Arjun after he turned."}')},
    ])
    monkeypatch.setattr(rr, '_reuse_fabricated_tools', lambda *a, **k: [])
    monkeypatch.setattr(lifecycle_hooks, 'get_registered_groupchat', lambda _s: chat)
    rr.recipes[KEY] = recipe
    rr.user_tasks[KEY] = SimpleNamespace(actions=list(recipe['actions']), current_action=2,
                                         get_action=lambda i: recipe['actions'][i])
    for name in ('force_state_through_valid_path', 'safe_set_state', 'commit_verified_action_completion'):
        monkeypatch.setattr(rr, name, lambda *a, **k: True)
    monkeypatch.setattr(rr, '_stamp_action_evidence_watermark', lambda *a, **k: None)
    noted = []
    monkeypatch.setattr(rr, '_reuse_note_flow_finished', lambda up, pid: noted.append((up, pid)))
    rr._reuse_resteer_counts.pop((KEY, 2), None)
    rr._reuse_fab_pending.pop((KEY, 2), None)
    assert rr._advance_reuse_action(KEY, 2, 'test', P) == (None, False)
    assert noted == [(KEY, P)]


def _chat_agent_until(rr, monkeypatch):
    """chat_agent with the run-boundary resets that are not under test made
    no-ops, a warm run stopped at the held-step gate, and a cold one at the
    schedule builder."""
    monkeypatch.setattr(rr, 'clear_action_states', lambda *a, **k: None)
    monkeypatch.setattr(rr, '_reuse_forget_reasks', lambda *a, **k: None)
    monkeypatch.setattr(rr, '_reuse_held_step_gate', lambda *a, **k: 'warm run went on')

    def stop(*_a, **_k):
        raise _Stop()
    monkeypatch.setattr(rr, 'create_schedule', stop)
    built = []
    monkeypatch.setattr(rr, 'create_agents_for_role', lambda *a, **k: built.append(a) or stop())
    return built


def test_a_turn_whose_session_was_built_for_another_flow_is_rebuilt(rr, monkeypatch):
    """chat_agent settles at the run start: a person moved since (here by a
    therapist) gets a session for their new flow, not the old one."""
    from core.flow_progress import set_persona, set_person_flow
    set_persona(U, P, 'Child', PERSONAS)
    set_person_flow(U, P, FLOWS, 'Child', 2, 'therapist-7')
    _warm(rr, monkeypatch, loaded_flow=0)
    rr.user_journey[KEY] = 'UseBot'
    _chat_agent_until(rr, monkeypatch)
    with pytest.raises(_Stop):                        # the cold builder, not the warm run
        rr.chat_agent(U, 'hello', P, None, 'msg_2_child')
    assert all(KEY not in getattr(rr, name) for name in CACHES)


def test_a_returning_person_with_a_kept_pick_is_not_asked_again(rr, monkeypatch):
    """chat_agent, cold (2 h idle or a restart): the pick from their first
    use skips the persona question (owner 2026-11-06)."""
    from core.flow_progress import set_persona
    set_persona(U, P, 'Child', PERSONAS)
    built = _chat_agent_until(rr, monkeypatch)
    with pytest.raises(_Stop):
        rr.chat_agent(U, 'hello', P, None, 'msg_3_child')
    assert built == [] and rr.user_journey[KEY] == 'UseBot'


def test_a_first_use_asks_the_persona(rr, monkeypatch):
    built = _chat_agent_until(rr, monkeypatch)
    with pytest.raises(_Stop):
        rr.chat_agent(U, 'hello', P, None, 'msg_1_child')
    assert built == [(U, P)]


@pytest.fixture
def role_group(rr, monkeypatch, tmp_path):
    """The real role group of a two-persona agent (as in
    test_role_group_runs_update_persona.py)."""
    import integrations.channels.memory.shared_history as sh

    class _History:
        def __init__(self):
            self.messages = []

        def add_message(self, msg, metadata=None):
            self.messages.append(msg)

        def search_by_metadata(self, **_kw):
            return []
    monkeypatch.setattr(rr, 'get_coding_workspace_dir', lambda: str(tmp_path))
    monkeypatch.setattr(sh, '_get_persistent_history', lambda user_id: _History())
    return rr.create_agents_for_role(U, P)


def _picks(name):
    def reply(recipient, messages=None, sender=None, config=None):
        return True, {'content': None, 'tool_calls': [{
            'id': 'call_persona_1', 'type': 'function',
            'function': {'name': 'update_persona', 'arguments': json.dumps({
                'name': name, 'description': '', 'new': True, 'contact_number': ''})}}]}
    return reply


def test_the_persona_picked_in_the_role_group_is_kept(rr, role_group):
    import autogen
    from core.flow_progress import stored_persona
    assistant, user_proxy, group_chat, manager, _helper, stop = role_group
    assert stop is False
    assistant.register_reply([autogen.Agent, None], _picks('child'), position=0)
    user_proxy.initiate_chat(manager, message='hello', speaker_selection={'speaker': 'assistant'},
                             clear_history=False)
    assert stored_persona(U, P, PERSONAS) == 'Child'  # kept, spelled as the agent spells it


def test_a_name_that_is_not_a_persona_is_not_kept_and_is_asked_again(rr, role_group):
    """The role group's own update_persona, as Helper executes it."""
    from core.flow_progress import stored_persona
    _assistant, _user_proxy, _group_chat, _manager, helper, _stop = role_group
    reply = helper.function_map['update_persona'](name='Mother', description='', new=True,
                                                  contact_number='')
    assert "'Mother' is not one of this agent's personas (Child, Parent)" in reply
    assert stored_persona(U, P, PERSONAS) is None
    assert KEY not in rr.agents_session


@pytest.fixture
def asks(monkeypatch):
    """person_ask as the join sees it; the child's account is answered by
    its guardian (core.event_attribution.person_for)."""
    from integrations.agent_engine import person_ask
    import core.event_attribution as event_attribution
    sent = []

    def ask(user_id, agent_id, component, *, prompt_id=None, **_k):
        sent.append((user_id, component))
        return {'ok': True, 'ask_id': f'ask-{len(sent)}', 'refused': None, 'card': True}
    monkeypatch.setattr(person_ask, 'ask', ask)
    monkeypatch.setattr(person_ask, 'ask_state', lambda *_a: {'status': 'open', 'answer': None})
    monkeypatch.setattr(event_attribution, 'person_for',
                        lambda uid, *_a, **_k: 'u-guardian' if str(uid) == 'u-other' else str(uid))
    return sent


def test_a_join_from_the_role_group_asks_the_childs_side_first(rr, role_group, asks):
    """update_persona(new=False), as Helper executes it: nobody joins until
    the child's side allows it (owner 2026-11-06)."""
    from core.flow_progress import pending_join
    rr.agents_session['u-other_77'] = [{'user_id': 'u-other', 'role': 'Child'}]
    _a, _u, _g, _m, helper, _s = role_group
    reply = helper.function_map['update_persona'](name='parent', description='', new=False,
                                                  contact_number='u-other')
    assert 'asked the account holder of u-other' in reply
    assert [who for who, _card in asks] == ['u-guardian']
    assert pending_join(U, P)['persona'] == 'Parent'
    assert [m['user_id'] for m in rr.agents_session['u-other_77']] == ['u-other']


def test_a_turn_with_a_join_waiting_says_so_and_builds_nothing(rr, monkeypatch, asks):
    """chat_agent answers a pending join before anything else runs."""
    from core.flow_progress import set_pending_join
    set_pending_join(U, P, 'u-other', 'Parent', 'ask-9')
    built = _chat_agent_until(rr, monkeypatch)
    reply = rr.chat_agent(U, 'hello', P, None, 'msg_4_parent')
    assert reply.startswith('Still waiting for the account holder of u-other')
    assert built == [] and KEY not in rr.user_journey


def test_every_run_of_a_flow_stamps_its_own_start(rr, monkeypatch):
    """_start_reuse_action, the one place a step starts (cold and warm),
    stamps the run's start at its first step, once per run (a step 1 that
    resumes keeps it), so a warm session that runs flow 2 twice stamps each run (hartos-16, 11-07: a stamp taken when
    the session was built went stale on warm runs)."""
    import time
    from core.flow_progress import read
    monkeypatch.setattr(rr, 'safe_set_state', lambda *a, **k: True)
    monkeypatch.setattr(rr, '_reuse_session_ledger', lambda _up: None)
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda _up, _aid: 2)
    rr._start_reuse_action(KEY, 1, 'reuse: first action')
    first = read(U, P)['running']['child']
    assert first['flow'] == 2
    time.sleep(0.01)
    rr._start_reuse_action(KEY, 2, 'reuse: next')       # a later step: same run
    rr._start_reuse_action(KEY, 1, 'reuse: after the held-step gate')  # step 1 resumes
    assert read(U, P)['running']['child']['at'] == first['at']
    from core.flow_progress import record_flow_finished
    record_flow_finished(U, P, 2, 'Child', 'run-1')     # the run ends
    time.sleep(0.01)
    rr._start_reuse_action(KEY, 1, 'reuse: warm run')   # the next run on the warm session
    assert read(U, P)['running']['child']['at'] > first['at']