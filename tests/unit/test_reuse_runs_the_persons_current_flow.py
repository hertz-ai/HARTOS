"""REUSE runs the flow a person is on, and moves them when its goal is met.

Owner, 2026-11-03: "When its goal is met".  Before this, nothing kept a
person's place: get_flow_number always took the persona's FIRST flow, the
persona pick lived only in 2 h caches, and a warm session (user_agents hit)
never re-read anything, so a child replayed flow 0 from action 1 every day.
hartos-7a's review (11-04) named each wiring point pinned here.

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
U, P, KEY = 'u-child', 77, 'u-child_77'
CACHES = ('user_agents', 'user_tasks', 'user_ledgers', 'recipes', 'time_actions')


@pytest.fixture
def rr(monkeypatch, tmp_path):
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    from hartos import reuse_recipe as rr
    for name in CACHES + ('request_id_list', 'agents_session', 'chat_joinees'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    prompt = tmp_path / f'{P}.json'
    prompt.write_text(json.dumps({'personas': [{'name': 'Child'}, {'name': 'Parent'}],
                                  'flows': FLOWS}), encoding='utf-8')
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


def test_a_persons_pick_beats_the_list_a_create_registered(rr):
    from core.flow_progress import set_persona
    # After a CREATE in this process the creator holds the whole persona list.
    rr.agents_session[KEY] = [{'user_id': U, 'role': 'Parent'}, {'user_id': U, 'role': 'Child'}]
    assert rr.get_role(U, P) == 'Parent'
    set_persona(U, P, 'Child')
    assert rr.get_role(U, P) == 'Child'


def test_a_pick_survives_the_caches(rr):
    from core.flow_progress import set_persona
    set_persona(U, P, 'Parent')
    assert rr.get_role(U, P) == 'Parent'             # nothing in agents_session at all


def test_the_builders_read_the_persons_place(rr):
    from core.flow_progress import set_persona, set_person_flow
    set_persona(U, P, 'Child')
    assert rr.get_flow_number(U, P) == (0, 'Child')
    set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')
    assert rr.get_flow_number(U, P) == (2, 'Child')


def test_a_met_goal_rebuilds_the_warm_session_for_the_next_flow(rr, monkeypatch):
    from core.flow_progress import set_persona, record_flow_finished, current_flow
    set_persona(U, P, 'Child')
    record_flow_finished(U, P, 0, 'run-1', 'step 2 (call Arjun): he turned')
    _warm(rr, monkeypatch, loaded_flow=0)
    asked = _model(monkeypatch, met=True)
    rr._reuse_settle_person_flow(U, P, KEY)
    assert asked == ['flow_goal_checkable', 'flow_goal_met']
    assert current_flow(U, P, FLOWS, 'Child') == 2
    assert all(KEY not in getattr(rr, name) for name in CACHES)


def test_a_goal_not_met_keeps_the_warm_session(rr, monkeypatch):
    from core.flow_progress import set_persona, record_flow_finished
    set_persona(U, P, 'Child')
    record_flow_finished(U, P, 0, 'run-1', 'nothing yet')
    _warm(rr, monkeypatch, loaded_flow=0)
    _model(monkeypatch, met=False)
    rr._reuse_settle_person_flow(U, P, KEY)
    assert all(KEY in getattr(rr, name) for name in CACHES)


def test_a_move_by_someone_else_reaches_a_warm_session_without_asking(rr, monkeypatch):
    from core.flow_progress import set_persona, set_person_flow
    set_persona(U, P, 'Child')
    set_person_flow(U, P, FLOWS, 'Child', 2, 'therapist-7')
    _warm(rr, monkeypatch, loaded_flow=0)
    asked = _model(monkeypatch)
    rr._reuse_settle_person_flow(U, P, KEY)
    assert asked == []                                # nothing to judge: no model call
    assert all(KEY not in getattr(rr, name) for name in CACHES)


def test_a_daemon_turn_neither_judges_nor_rebuilds(rr, monkeypatch):
    from core.flow_progress import set_persona, record_flow_finished, read
    set_persona(U, P, 'Child')
    record_flow_finished(U, P, 0, 'run-1', 'he turned')
    _warm(rr, monkeypatch, loaded_flow=0, person=False)
    asked = _model(monkeypatch)
    rr._reuse_settle_person_flow(U, P, KEY)
    assert asked == [] and len(read(U, P)['unjudged']) == 1
    assert all(KEY in getattr(rr, name) for name in CACHES)


def test_a_finished_flow_is_kept_only_from_a_persons_turn(rr, monkeypatch):
    from core.flow_progress import read
    rr.request_id_list[KEY] = 'msg_1_child'
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda _up, _aid: 0)
    monkeypatch.setattr(rr, '_reuse_flow_evidence', lambda _up, flow: f'evidence of flow {flow}')
    monkeypatch.setattr(rr, '_reuse_person_turn', lambda _up: False)
    rr._reuse_note_flow_finished(KEY, P)
    assert 'unjudged' not in read(U, P)
    monkeypatch.setattr(rr, '_reuse_person_turn', lambda _up: True)
    rr._reuse_note_flow_finished(KEY, P)
    assert read(U, P)['unjudged'][-1]['run'] == 'msg_1_child'
    assert read(U, P)['unjudged'][-1]['evidence'] == 'evidence of flow 0'


def test_the_evidence_is_the_runs_own_steps_in_that_flow(rr, monkeypatch):
    ledger = SimpleNamespace(tasks={
        'action_2': SimpleNamespace(description='wait for Arjun to turn', result='child: turned'),
        'action_1': SimpleNamespace(description='call Arjun by name', result='sent: Arjun!'),
        'action_3': SimpleNamespace(description="ask for the parent's photos", result='photos?'),
        'action_4': SimpleNamespace(description='not done yet', result=None),
    })
    monkeypatch.setattr(rr, '_reuse_session_ledger', lambda _up: ledger)
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda _up, aid: 1 if aid == 3 else 0)
    text = rr._reuse_flow_evidence(KEY, 0)
    assert text.splitlines() == ['step 1 (call Arjun by name): sent: Arjun!',
                                 'step 2 (wait for Arjun to turn): child: turned']


def test_a_step_filed_after_a_move_lands_in_the_flow_that_made_it(rr, monkeypatch):
    from core.flow_progress import set_persona, set_person_flow
    set_persona(U, P, 'Child')
    set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')
    _warm(rr, monkeypatch, loaded_flow=0)
    assert rr._reuse_run_flow(U, P) == 0              # the run, not the new place
    monkeypatch.setattr(rr, '_reuse_session_ledger', lambda _up: None)
    assert rr._reuse_run_flow(U, P) == 2              # no run: the person's place
