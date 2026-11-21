"""core.flow_progress: a flow missing only what it reports is done for now.

Live 2026-11-21, the speech agent (prompt 1794540972213): its Parent flow
"Home names and photos" asks the parent for the home Tamil names and photos,
saves family_members, then reads name_response and family_association_result
to send a progress note.  Only the Child flows write those two keys, so
while the child had not practised the judge said met=False, "Nothing is
saved at name_response", and the parent was asked for the names every turn.

hartos-52's rule, read off the run's own tool calls (the model names keys as
it goes, so never the steps' text): a read before or between the flow's own
saves is a WORK input, a read after its last save a REPORT input, and a key
no recorded run of this flow saved is FOREIGN.  A finished run judged not
met is done for now when its saves all held and every key it found nothing
at is a foreign report input; it then moves as a met goal does.  The judge
runs as before and a met verdict is never touched.

The real store on a temp data dir and the real not-found answer of
get_data_by_key; the judge is a stub that answers not met unless told.

    python -m pytest tests/unit/test_a_flow_missing_only_what_it_reports_is_done_for_now.py -q
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

FLOWS = [
    {'flow_name': 'Name calling response', 'persona': 'Child',
     'sub_goal': 'Confirm Arjun responds to his name in Tamil.'},
    {'flow_name': 'Family member association', 'persona': 'Child',
     'sub_goal': "Test Arjun's ability to associate family photos with names."},
    {'flow_name': 'Home names and photos', 'persona': 'Parent',
     'sub_goal': "Update parent with Arjun's current naming knowledge."},
    {'flow_name': 'Progress review', 'persona': 'Therapist',
     'sub_goal': 'Provide therapist with progress data and next step selection.'},
]
U, P = 'u-family', 1794540972213
UNMET = 'Nothing is saved at name_response'
REPORTS_MISSING = 'done for now: only report inputs missing (name_response, family_association_result)'
_ids = itertools.count(1)


@pytest.fixture
def fp(tmp_path, monkeypatch):
    import core.platform_paths as platform_paths
    import core.flow_progress as flow_progress
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    return flow_progress


class Judge:
    """The node's model: not met unless told, with the evidence given."""

    def __init__(self, met=False, evidence=UNMET, weakest=''):
        self.met, self.evidence, self.weakest, self.asked = met, evidence, weakest, []

    def __call__(self, content, schema, name):
        self.asked.append(name)
        return {'flow_goal_checkable': {'checkable': True},
                'flow_goal_met': {'met': self.met, 'evidence': self.evidence},
                'flow_weakest': {'flow': self.weakest}}[name]


def dispatch(action, retry=False):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': ('[retry: the step is not done] ' if retry else '')
            + f'Execute Action {action}: step {action} of the flow'}


def tool(name, args, answer):
    """One call as autogen keeps it: the proposal, then the tool's answer."""
    call_id = f'call_{next(_ids)}'
    return [{'role': 'assistant', 'name': 'Assistant', 'content': None,
             'tool_calls': [{'id': call_id, 'type': 'function',
                             'function': {'name': name, 'arguments': json.dumps(args)}}]},
            {'role': 'tool', 'name': 'Helper', 'content': answer,
             'tool_responses': [{'tool_call_id': call_id, 'role': 'tool', 'content': answer}]}]


def save(key, value='x', ok=True):
    """save_data_in_memory's own answers: "Saved at <key>: <value>", or its
    failure (core.agent_tools)."""
    return tool('save_data_in_memory', {'key': key, 'value': value},
                f'Saved at {key}: {json.dumps(value)}' if ok
                else 'Error: Unexpected error saving data: disk full - Data not saved')


def read(key, value=None, name='get_data_by_key'):
    """get_data_by_key's real answer: the value, or its own not-found reply."""
    from core.agent_tools import key_not_found_reply
    return tool(name, {'key': key},
                str(value) if value is not None else key_not_found_reply({}, key))


def send(text):
    return tool('send_message_to_user', {'message': text}, f'Message sent: {text}')


def run(*parts):
    return [m for part in parts for m in (part if isinstance(part, list) else [part])]


def parent_run(saved_ok=True):
    """The live Parent run: ask, the parent's reply turn, save, read, report."""
    return run(dispatch(1), send('What do you call Amma and Appa at home? Show me their photos.'),
               dispatch(1),
               dispatch(2), save('family_members', {'Amma': 'photo_1', 'Appa': 'photo_2'}, saved_ok),
               dispatch(3), read('name_response'),
               read('family_association_result', name='get_data_from_memory'),
               send('Arjun has not practised yet; we will start with Amma and Appa.'))


PARENT_OBSERVED = ('person answered at step 1: Amma and Appa, photos attached\n'
                   'step 3 result: Key not found in stored data. Nothing is saved at name_response.')


def finish(fp, flow, persona, messages, run_id, observed='step 1 result: sent'):
    fp.record_flow_finished(U, P, flow, persona, run_id, observed, 'steps',
                            messages=[messages])


def verdict(fp, flow):
    return fp.read(U, P)['verdicts'][str(flow)][-1]


# 1. The live case.

def test_a_parent_flow_waiting_on_the_childs_results_is_done_for_now(fp):
    finish(fp, 2, 'Parent', parent_run(), 'run-parent', PARENT_OBSERVED)
    judge = Judge()
    assert fp.settle(U, P, FLOWS, 'Parent', judge) == 2
    assert judge.asked == ['flow_goal_checkable', 'flow_goal_met']      # the judge still runs
    assert verdict(fp, 2)['met'] is True
    assert verdict(fp, 2)['evidence'] == REPORTS_MISSING
    place = fp.read(U, P)['places']['parent']
    assert (place['flow'], place['by']) == (2, 'goal_met')              # its only flow: re-entered later
    assert fp.read(U, P)['unjudged'] == []


def test_done_for_now_moves_on_exactly_as_a_met_goal_does(fp):
    flows = FLOWS + [{'flow_name': 'Home routine', 'persona': 'Parent',
                      'sub_goal': 'The parent names the daily routine words used at home.'}]
    finish(fp, 2, 'Parent', parent_run(), 'run-parent', PARENT_OBSERVED)
    assert fp.settle(U, P, flows, 'Parent', Judge()) == 4
    assert fp.current_flow(U, P, flows, 'Parent') == 4


def test_the_record_keeps_what_the_run_read_and_saved_in_order(fp):
    finish(fp, 2, 'Parent', parent_run(), 'run-parent')
    record = fp.read(U, P)['unjudged'][-1]
    assert record['memory'] == [{'save': 'family_members', 'ok': True},
                                {'read': 'name_response', 'ok': False},
                                {'read': 'family_association_result', 'ok': False}]
    assert fp.read(U, P)['saved_keys'] == {'2': ['family_members']}


# 2. A missing WORK input keeps the flow unmet.

def test_a_child_flow_missing_what_it_works_from_stays_unmet(fp):
    """The Child reads family_members at its start, finds nothing, then saves
    name_response: what it worked from was missing.  The read sits in the
    run's first turn, before the reply turn dispatches action 1 again."""
    messages = run(dispatch(1), read('family_members'), send('Arjun!'),
                   dispatch(1),
                   dispatch(2), send('Arjun, look here!'),
                   dispatch(3), save('name_response', 'turned'),
                   read('family_association_result'))
    finish(fp, 0, 'Child', messages, 'run-child')
    fp.settle(U, P, FLOWS, 'Child', Judge(evidence='nothing at family_members'))
    assert verdict(fp, 0)['met'] is False
    assert verdict(fp, 0)['evidence'] == 'nothing at family_members'
    assert fp.current_flow(U, P, FLOWS, 'Child') is None


# 3. The flow's own outcome is the judge's.

def test_a_childs_own_poor_result_stays_unmet(fp):
    messages = run(dispatch(1), read('child_name', 'Arjun'), send('Arjun!'),
                   dispatch(2), send('Arjun, look here!'),
                   dispatch(3), send('Arjun, here!'),
                   dispatch(4), save('name_response', 'no response'))
    finish(fp, 0, 'Child', messages, 'run-child')
    fp.settle(U, P, FLOWS, 'Child', Judge(evidence='no response'))
    assert verdict(fp, 0)['met'] is False
    assert verdict(fp, 0)['evidence'] == 'no response'
    assert fp.current_flow(U, P, FLOWS, 'Child') is None


# 4. Whose key it is: this flow's, not this persona's.

def test_a_key_the_same_personas_other_flow_saves_is_still_foreign(fp):
    first = run(dispatch(1), send('Arjun!'), dispatch(2), save('name_response', 'turned'))
    finish(fp, 0, 'Child', first, 'run-child-0')
    fp.settle(U, P, FLOWS, 'Child', Judge(evidence='turned once'))
    assert verdict(fp, 0)['met'] is False
    second = run(dispatch(1), read('family_members', {'Amma': 'photo_1'}),
                 dispatch(2), send('Arjun, who is this? Amma?'),
                 dispatch(3), send('Super!'),
                 dispatch(4), save('family_association_result', 'Amma'), read('name_response'))
    finish(fp, 1, 'Child', second, 'run-child-1')
    fp.settle(U, P, FLOWS, 'Child', Judge(weakest='Name calling response'))
    assert verdict(fp, 1)['evidence'] == 'done for now: only report inputs missing (name_response)'
    assert fp.current_flow(U, P, FLOWS, 'Child') == 0               # back to the weakest earlier flow
    assert fp.read(U, P)['saved_keys'] == {'0': ['name_response'], '1': ['family_association_result']}


def test_a_key_this_flow_saved_in_an_earlier_run_is_its_own(fp):
    earlier = run(dispatch(1), send('Names?'), dispatch(2), save('family_members', {'Amma': 'p1'}),
                  save('home_names', ['Amma', 'Appa']), dispatch(3), send('Thank you'))
    finish(fp, 2, 'Parent', earlier, 'run-parent-1')
    fp.settle(U, P, FLOWS, 'Parent', Judge(evidence='no progress yet'))
    later = run(dispatch(1), send('Names?'), dispatch(2), save('family_members', {'Amma': 'p1'}),
                dispatch(3), read('home_names'), send('Progress note'))
    finish(fp, 2, 'Parent', later, 'run-parent-2')
    fp.settle(U, P, FLOWS, 'Parent', Judge(evidence='no progress yet'))
    assert verdict(fp, 2)['met'] is False
    assert 'places' not in fp.read(U, P)


# 5. Its own saves must hold.

def test_a_failed_save_of_its_own_keeps_it_unmet(fp):
    finish(fp, 2, 'Parent', parent_run(saved_ok=False), 'run-parent', PARENT_OBSERVED)
    fp.settle(U, P, FLOWS, 'Parent', Judge())
    assert verdict(fp, 2)['met'] is False
    assert verdict(fp, 2)['evidence'] == UNMET
    assert 'places' not in fp.read(U, P)


def test_a_run_with_no_save_is_judged_as_before(fp):
    messages = run(dispatch(1), send('Names?'), dispatch(2), read('name_response'))
    finish(fp, 2, 'Parent', messages, 'run-parent')
    fp.settle(U, P, FLOWS, 'Parent', Judge())
    assert verdict(fp, 2)['met'] is False


# 6. A met verdict stays met.

def test_a_met_verdict_stays_met_and_keeps_its_evidence(fp):
    finish(fp, 2, 'Parent', parent_run(), 'run-parent', PARENT_OBSERVED)
    fp.settle(U, P, FLOWS, 'Parent', Judge(met=True, evidence='Amma and Appa, photos attached'))
    assert verdict(fp, 2)['met'] is True
    assert verdict(fp, 2)['evidence'] == 'Amma and Appa, photos attached'


def test_a_met_verdict_with_a_missing_work_input_stays_met(fp):
    messages = run(dispatch(1), read('family_members'), send('Arjun!'),
                   dispatch(2), save('name_response', 'turned'))
    finish(fp, 0, 'Child', messages, 'run-child', 'person answered at step 1: he turned')
    fp.settle(U, P, FLOWS, 'Child', Judge(met=True, evidence='he turned'))
    assert verdict(fp, 0)['met'] is True
    assert fp.current_flow(U, P, FLOWS, 'Child') == 1


# Which part of the messages is the run.

def test_a_run_whose_start_is_not_in_the_messages_is_judged_as_before(fp):
    """A session rebuilt mid-run holds only its later steps: what the run
    read before is unknown, so nothing is concluded from it."""
    messages = run(dispatch(2), save('family_members', {'Amma': 'p1'}),
                   dispatch(3), read('name_response'), read('family_association_result'))
    finish(fp, 2, 'Parent', messages, 'run-parent')
    assert fp.read(U, P)['unjudged'][-1]['memory'] is None
    fp.settle(U, P, FLOWS, 'Parent', Judge())
    assert verdict(fp, 2)['met'] is False


def test_the_latest_run_in_the_session_is_the_one_read(fp):
    """A warm session holds an earlier run of the same flow; only the run
    that just finished counts, from its first dispatch of action 1 on."""
    earlier = run(dispatch(1), read('child_name'), send('Names?'),
                  dispatch(2), save('family_members', {'Amma': 'p1'}),
                  dispatch(3), send('Progress note'))
    latest = run(dispatch(1), send('Names?'), dispatch(1, retry=True),
                 dispatch(2), save('family_members', {'Amma': 'p1'}),
                 dispatch(3), read('name_response'), read('family_association_result'))
    finish(fp, 2, 'Parent', earlier + latest, 'run-parent')
    fp.settle(U, P, FLOWS, 'Parent', Judge())
    assert verdict(fp, 2)['evidence'] == REPORTS_MISSING


def test_a_call_in_the_group_log_and_a_buffer_counts_once(fp):
    """The group log holds a call whose answer only a participant's buffer
    holds (hartos-52, F1 of e6808a611): both are read, each call once."""
    buffer = parent_run()
    log = [dict(m, content=None, tool_responses=[dict(r, content=None) for r in m['tool_responses']])
           if m.get('role') == 'tool' else m for m in buffer]
    assert fp.run_memory([log, buffer]) == [{'save': 'family_members', 'ok': True},
                                            {'read': 'name_response', 'ok': False},
                                            {'read': 'family_association_result', 'ok': False}]


# The wiring: REUSE hands the finished run's messages over.

@pytest.fixture
def rr(fp, monkeypatch):
    from flask import Flask
    from hartos import reuse_recipe as rr
    monkeypatch.setattr(rr, 'request_id_list', {}, raising=False)
    with Flask('done-for-now-test').app_context():
        yield rr


def test_reuse_keeps_what_the_finished_run_read_and_saved(fp, rr, monkeypatch):
    import hartos.lifecycle_hooks as lifecycle_hooks
    key = f'{U}_{P}'
    chat = SimpleNamespace(messages=parent_run(), agents=[])
    monkeypatch.setattr(lifecycle_hooks, 'get_registered_groupchat', lambda _up: chat)
    monkeypatch.setattr(rr, '_reuse_person_turn', lambda _up: True)
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda _up, _aid: 2)
    monkeypatch.setattr(rr, '_reuse_flow_persona', lambda _pid, _flow: 'Parent')
    monkeypatch.setattr(rr, '_reuse_flow_evidence', lambda _up, _flow: (PARENT_OBSERVED, 'steps'))
    rr.request_id_list[key] = 'msg_1_parent'
    rr._reuse_note_flow_finished(key, P)
    record = fp.read(U, P)['unjudged'][-1]
    assert (record['flow'], record['persona'], record['run']) == (2, 'Parent', 'msg_1_parent')
    assert record['memory'] == [{'save': 'family_members', 'ok': True},
                                {'read': 'name_response', 'ok': False},
                                {'read': 'family_association_result', 'ok': False}]
