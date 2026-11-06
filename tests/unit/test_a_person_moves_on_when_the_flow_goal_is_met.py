"""core.flow_progress: a person moves to their next flow when its goal is met.

Owner, 2026-11-03: "When its goal is met".  Owner rulings, 2026-11-04: the
last flow of a persona met -> the weakest earlier flow per the progress
record; a goal that cannot be checked moves after one finished run.  Before
this nothing kept a person's flow or even their persona past 2 h in memory
(core.persona_registry), so REUSE always ran a persona's first flow.

The real store on a temp data dir; the model is a recorded fake, so each
test shows which questions were asked.

    python -m pytest tests/unit/test_a_person_moves_on_when_the_flow_goal_is_met.py -q
"""
from __future__ import annotations

import os
import sys
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

FLOWS = [
    {'flow_name': 'Name calling response', 'persona': 'Child', 'sub_goal': 'Arjun turns when called by name'},
    {'flow_name': 'Family photos', 'persona': 'Parent', 'sub_goal': 'the parent sent three family photos'},
    {'flow_name': 'Family member association', 'persona': 'Child', 'sub_goal': 'Arjun names Amma in a photo'},
    {'flow_name': 'Flash cards', 'persona': 'Child', 'sub_goal': ''},
]
U, P = 'u-child', 'p-speech'
RUN = 'step 1 (call Arjun by name): sent: Arjun!\nstep 2 (wait for him): child: Turned when called'


@pytest.fixture
def fp(tmp_path, monkeypatch):
    import core.platform_paths as platform_paths
    import core.flow_progress as flow_progress
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    return flow_progress


class Model:
    """The node's model: answers by the question's schema name, records each."""

    def __init__(self, checkable=True, met=True, weakest='', down=False, quote='turned when called',
                 down_for=()):
        self.asked, self.checkable, self.met, self.weakest, self.down = [], checkable, met, weakest, down
        self.contents, self.quote, self.down_for = [], quote, set(down_for)

    def __call__(self, content, schema, name):
        self.asked.append(name)
        self.contents.append(content)
        if self.down or name in self.down_for:
            return None
        return {'flow_goal_checkable': {'checkable': self.checkable},
                'flow_goal_met': {'met': self.met, 'evidence': f'"{self.quote}"'},
                'flow_weakest': {'flow': self.weakest}}[name]


def test_the_judge_reads_what_the_finished_run_recorded(fp):
    """The evidence is taken when the flow finishes and kept with the record,
    so a restart before the next turn loses nothing (hartos-7a)."""
    fp.record_flow_finished(U, P, 0, 'run-1', evidence='step 1 result: called "Arjun!"\n'
                                                       'person answered at step 2: turned and looked')
    fp._locks.clear()                                # a restart between the two turns
    model = Model(met=True, quote='turned and looked')
    assert fp.settle(U, P, FLOWS, 'Child', model) == 2
    assert 'turned and looked' in model.contents[-1]
    assert 'Arjun turns when called by name' in model.contents[-1]


def test_a_met_whose_quote_the_record_does_not_hold_moves_no_one(fp):
    """A small model's made-up quote must not move a child on unseen
    (hartos-7a, 11-06): "met" counts only when its quote is in the record."""
    fp.record_flow_finished(U, P, 0, 'run-1', RUN)
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True, quote='said ball clearly')) is None
    verdict = fp.read(U, P)['verdicts']['0'][-1]
    assert verdict['met'] is False and verdict['evidence'].startswith('quote not in the record')
    assert fp.read(U, P)['unjudged'] == []


def test_a_move_made_after_the_run_finished_is_not_undone(fp):
    """A therapist moved the child after the run that finished flow 0: the
    verdict on that older run keeps their move (hartos-7a, 11-06)."""
    import time
    fp.record_flow_finished(U, P, 0, 'run-1', RUN)
    time.sleep(0.01)
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True)) == 3
    assert fp.read(U, P)['verdicts']['0'][-1]['met'] is True


def test_with_no_record_the_builders_keep_the_first_flow(fp):
    assert fp.current_flow(U, P, FLOWS, 'Child') is None
    model = Model()
    assert fp.settle(U, P, FLOWS, 'Child',model) is None
    assert model.asked == []                         # most turns pay nothing


def test_a_met_goal_moves_the_child_to_their_next_flow_not_the_parents(fp):
    fp.record_flow_finished(U, P, 0, 'run-1', RUN)
    model = Model(met=True)
    assert fp.settle(U, P, FLOWS, 'Child',model) == 2   # skips the parent's flow 1
    assert model.asked == ['flow_goal_checkable', 'flow_goal_met']
    assert fp.read(U, P)['unjudged'] == []
    assert fp.read(U, P)['by'] == 'goal_met'


def test_a_goal_not_met_stays_and_its_verdict_is_kept(fp):
    fp.record_flow_finished(U, P, 0, 'run-1', RUN)
    assert fp.settle(U, P, FLOWS, 'Child',Model(met=False)) is None
    assert fp.read(U, P)['verdicts']['0'][-1]['met'] is False
    assert fp.read(U, P)['unjudged'] == []


def test_a_model_that_cannot_be_asked_leaves_the_run_for_next_time(fp):
    fp.record_flow_finished(U, P, 0, 'run-1', RUN)
    assert fp.settle(U, P, FLOWS, 'Child',Model(down=True)) is None
    assert [r['run'] for r in fp.read(U, P)['unjudged']] == ['run-1']
    assert fp.settle(U, P, FLOWS, 'Child',Model(met=True)) == 2


def test_a_met_question_the_model_cannot_answer_leaves_the_run_unjudged(fp):
    """The goal was found checkable, then the model went down before saying
    "met": nothing is recorded as not met, the run waits for next time."""
    fp.record_flow_finished(U, P, 0, 'run-1', RUN)
    assert fp.settle(U, P, FLOWS, 'Child', Model(down_for={'flow_goal_met'})) is None
    assert [r['run'] for r in fp.read(U, P)['unjudged']] == ['run-1']
    assert 'verdicts' not in fp.read(U, P)
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True)) == 2


def test_checkable_is_decided_once_per_flow_and_kept(fp):
    fp.record_flow_finished(U, P, 0, 'run-1', RUN)
    fp.settle(U, P, FLOWS, 'Child',Model(met=False))
    fp.record_flow_finished(U, P, 0, 'run-2', RUN)
    later = Model(checkable=False, met=False)        # a later "no" changes nothing
    assert fp.settle(U, P, FLOWS, 'Child',later) is None
    assert later.asked == ['flow_goal_met']
    assert fp.read(U, P)['checkable'] == {'0': True}


def test_a_goal_that_cannot_be_checked_moves_after_one_finished_run(fp):
    fp.record_flow_finished(U, P, 0, 'run-1', RUN)
    model = Model(checkable=False)
    assert fp.settle(U, P, FLOWS, 'Child',model) == 2
    assert model.asked == ['flow_goal_checkable']    # never asked "met"


def test_an_empty_goal_moves_after_one_run_without_asking(fp):
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    fp.record_flow_finished(U, P, 3, 'run-1')
    model = Model(weakest='Family member association')
    assert fp.settle(U, P, FLOWS, 'Child',model) == 2
    assert model.asked == ['flow_weakest']           # last flow: back to the weakest


def test_the_last_flow_met_goes_back_to_the_weakest_earlier_flow(fp):
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    fp.record_flow_finished(U, P, 3, 'run-1')
    assert fp.settle(U, P, FLOWS, 'Child',Model(weakest='name calling response')) == 0


def test_a_weakest_flow_that_is_not_theirs_keeps_them_where_they_are(fp):
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    fp.record_flow_finished(U, P, 3, 'run-1')
    assert fp.settle(U, P, FLOWS, 'Child',Model(weakest='Family photos')) == 3


def test_the_pointer_and_persona_survive_a_restart(fp):
    fp.set_persona(U, P, 'Child')
    fp.set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')
    fp._locks.clear()                                # a new process: nothing in memory
    assert fp.stored_persona(U, P) == 'Child'
    assert fp.current_flow(U, P, FLOWS, 'Child') == 2


def test_another_personas_flow_is_refused(fp):
    assert fp.set_person_flow(U, P, FLOWS, 'Child', 1, 'therapist-7') is False
    assert fp.current_flow(U, P, FLOWS, 'Child') is None


def test_a_stored_flow_of_another_persona_is_not_theirs(fp):
    fp.set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')
    assert fp.current_flow(U, P, FLOWS, 'Parent') is None


def test_two_writers_in_the_same_moment_both_land(fp):
    fp.set_persona(U, P, 'Child')
    barrier = threading.Barrier(2)

    def finish():
        barrier.wait()
        fp.record_flow_finished(U, P, 0, 'run-a')

    def move():
        barrier.wait()
        fp.set_person_flow(U, P, FLOWS, 'Child', 2, 'therapist-7')

    threads = [threading.Thread(target=finish), threading.Thread(target=move)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = fp.read(U, P)
    assert data['flow'] == 2 and [r['run'] for r in data['unjudged']] == ['run-a']
    assert data['persona'] == 'Child'
