"""core.flow_progress: a person moves to their next flow when its goal is met.

Owner, 2026-11-03: "When its goal is met".  Owner rulings, 2026-11-04: the
last flow of a persona met -> the weakest earlier flow per the progress
record; a goal that cannot be checked moves after one finished run.  Owner,
2026-11-06: the persona is asked at a person's first use of the agent and
kept with its flow; persons join each other's chats by consent.  Before this
nothing kept a person's flow or even their persona past 2 h in memory
(core.persona_registry), so REUSE always ran a persona's first flow.

The independent review of the first version (f9951c0d2, 11-06) found three
defects, each pinned here: a finished run was dropped unjudged when the next
turn's persona was not known (every one-persona agent after a restart); a
"met" that quoted the agent's own step text passed; and any name was kept as
a person's pick forever.

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
PERSONAS = ['Child', 'Parent', 'Therapist']
U, P = 'u-child', 'p-speech'
OBSERVED = 'step 1 result: sent: Arjun!\nperson answered at step 2: Turned when called'
STEPS = 'step 1: call Arjun by name\nstep 2: check that Arjun turns when called by name'


@pytest.fixture
def fp(tmp_path, monkeypatch):
    import core.platform_paths as platform_paths
    import core.flow_progress as flow_progress
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    return flow_progress


def finish(fp, flow=0, persona='Child', run='run-1', observed=OBSERVED, steps=STEPS):
    fp.record_flow_finished(U, P, flow, persona, run, observed, steps)


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
    finish(fp, observed='step 1 result: called "Arjun!"\nperson answered at step 2: turned and looked')
    fp._locks.clear()                                # a restart between the two turns
    model = Model(met=True, quote='turned and looked')
    fp.settle(U, P, FLOWS, 'Child', model)
    assert fp.current_flow(U, P, FLOWS, 'Child') == 2
    assert 'turned and looked' in model.contents[-1]
    assert 'Arjun turns when called by name' in model.contents[-1]


def test_a_finished_run_is_judged_when_no_persona_is_known_yet(fp):
    """Review of f9951c0d2, the critical finding: a one-persona agent never
    asks the persona, so after a restart the next turn's persona was 'user'
    and the finished run was dropped unjudged.  The record carries its own
    persona, so it is judged whoever's turn comes next."""
    finish(fp)
    fp._locks.clear()
    assert fp.settle(U, P, FLOWS, None, Model(met=True)) is None
    assert fp.current_flow(U, P, FLOWS, 'Child') == 2
    assert fp.read(U, P)['unjudged'] == []


def test_a_childs_run_is_judged_on_the_parents_turn(fp):
    finish(fp)
    assert fp.settle(U, P, FLOWS, 'Parent', Model(met=True)) is None      # the parent's place
    assert fp.current_flow(U, P, FLOWS, 'Child') == 2                     # the child moved


def test_a_met_that_quotes_the_agents_own_step_text_moves_no_one(fp):
    """Review of f9951c0d2: the step text restates the goal; a child who
    never answered passed when the model quoted it.  Only what the run
    observed counts."""
    finish(fp, observed='step 1 result: sent: Arjun!\nstep 2 result: no response')
    model = Model(met=True, quote='Arjun turns when called by name')
    fp.settle(U, P, FLOWS, 'Child', model)
    assert fp.current_flow(U, P, FLOWS, 'Child') is None
    verdict = fp.read(U, P)['verdicts']['0'][-1]
    assert verdict['met'] is False and verdict['evidence'].startswith('quote not in what the run observed')


def test_a_met_whose_quote_the_record_does_not_hold_moves_no_one(fp):
    """A small model's made-up quote must not move a child on unseen
    (hartos-7a, 11-06): "met" counts only when its quote is in the record."""
    finish(fp)
    fp.settle(U, P, FLOWS, 'Child', Model(met=True, quote='said ball clearly'))
    assert fp.current_flow(U, P, FLOWS, 'Child') is None
    verdict = fp.read(U, P)['verdicts']['0'][-1]
    assert verdict['met'] is False and verdict['evidence'].startswith('quote not in what the run observed')
    assert fp.read(U, P)['unjudged'] == []


def test_a_move_made_after_the_run_finished_is_not_undone(fp):
    """A therapist moved the child after the run that finished flow 0: the
    verdict on that older run keeps their move (hartos-7a, 11-06)."""
    import time
    finish(fp)
    time.sleep(0.01)
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True)) == 3
    assert fp.read(U, P)['verdicts']['0'][-1]['met'] is True


def test_a_move_made_while_the_run_was_going_on_outlives_its_verdict(fp):
    """hartos-16, 11-07: the therapist moves the child mid-session; the
    child's run of flow 0 finishes AFTER the move.  Its verdict must not
    move the child back onto flow 0's next flow."""
    import time
    fp.note_run_started(U, P, 'Child', 0)            # the session for flow 0 is built
    time.sleep(0.01)
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    time.sleep(0.01)
    finish(fp)                                       # the run finishes after the move
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True)) == 3
    assert fp.read(U, P)['verdicts']['0'][-1]['met'] is True


def test_a_verdicts_own_earlier_move_never_blocks_the_next_verdict(fp):
    """hartos-16, 11-07: on a warm session the run-start stamp can be older
    than the agent's own 'goal_met' move; only a person's move may keep a
    place, or the child would stop progressing."""
    import time
    fp.note_run_started(U, P, 'Child', 2)
    time.sleep(0.01)
    fp.set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')    # the last verdict's own move
    time.sleep(0.01)
    finish(fp, flow=2, observed='person answered at step 1: Amma')
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True, quote='Amma')) == 3


def test_a_last_flow_that_stays_put_still_moves_on_a_later_verdict(fp):
    """The last flow met and the weakest-flow question names nothing it can
    match: the child stays (a fresh 'goal_met' place).  The next verdict
    that names a flow still moves them."""
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    fp.note_run_started(U, P, 'Child', 3)
    finish(fp, flow=3, run='run-1')
    assert fp.settle(U, P, FLOWS, 'Child', Model(weakest='no such flow')) == 3
    finish(fp, flow=3, run='run-2')
    assert fp.settle(U, P, FLOWS, 'Child', Model(weakest='Family member association')) == 2


def test_a_held_first_step_that_resumes_keeps_the_runs_first_start(fp):
    """hartos-16, 11-07: step 1 asks the child and waits; the therapist moves
    the child meanwhile; the child answers and step 1 starts again.  The
    run's start stays the first one, so the move stands."""
    import time
    fp.note_run_started(U, P, 'Child', 0)
    time.sleep(0.01)
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    time.sleep(0.01)
    fp.note_run_started(U, P, 'Child', 0)            # the held step resumes
    finish(fp)
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True)) == 3


def test_a_finished_run_clears_its_start_so_the_next_run_stamps_fresh(fp):
    import time
    fp.note_run_started(U, P, 'Child', 0)
    first = fp.read(U, P)['running']['child']['at']
    finish(fp)
    assert 'child' not in fp.read(U, P).get('running', {})
    time.sleep(0.01)
    fp.note_run_started(U, P, 'Child', 0)
    assert fp.read(U, P)['running']['child']['at'] > first


def test_a_run_start_of_another_flow_is_not_this_runs_start(fp):
    """The stamp belongs to the flow the session was built for."""
    import time
    fp.note_run_started(U, P, 'Child', 2)
    time.sleep(0.01)
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    time.sleep(0.01)
    finish(fp, flow=0)                               # a run of flow 0, started now
    assert fp.read(U, P)['unjudged'][-1]['started'] == fp.read(U, P)['unjudged'][-1]['at']


def test_with_no_record_the_builders_keep_the_first_flow(fp):
    assert fp.current_flow(U, P, FLOWS, 'Child') is None
    model = Model()
    assert fp.settle(U, P, FLOWS, 'Child', model) is None
    assert model.asked == []                         # most turns pay nothing


def test_a_met_goal_moves_the_child_to_their_next_flow_not_the_parents(fp):
    finish(fp)
    model = Model(met=True)
    assert fp.settle(U, P, FLOWS, 'Child', model) == 2   # skips the parent's flow 1
    assert model.asked == ['flow_goal_checkable', 'flow_goal_met']
    assert fp.read(U, P)['unjudged'] == []
    assert fp.read(U, P)['places']['child']['by'] == 'goal_met'


def test_a_goal_not_met_stays_and_its_verdict_is_kept(fp):
    finish(fp)
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=False)) is None
    assert fp.read(U, P)['verdicts']['0'][-1]['met'] is False
    assert fp.read(U, P)['unjudged'] == []


def test_a_model_that_cannot_be_asked_leaves_the_run_for_next_time(fp):
    finish(fp)
    assert fp.settle(U, P, FLOWS, 'Child', Model(down=True)) is None
    assert [r['run'] for r in fp.read(U, P)['unjudged']] == ['run-1']
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True)) == 2


def test_a_met_question_the_model_cannot_answer_leaves_the_run_unjudged(fp):
    """The goal was found checkable, then the model went down before saying
    "met": nothing is recorded as not met, the run waits for next time."""
    finish(fp)
    assert fp.settle(U, P, FLOWS, 'Child', Model(down_for={'flow_goal_met'})) is None
    assert [r['run'] for r in fp.read(U, P)['unjudged']] == ['run-1']
    assert 'verdicts' not in fp.read(U, P)
    assert fp.settle(U, P, FLOWS, 'Child', Model(met=True)) == 2


def test_checkable_is_decided_once_per_flow_and_kept(fp):
    finish(fp)
    fp.settle(U, P, FLOWS, 'Child', Model(met=False))
    finish(fp, run='run-2')
    later = Model(checkable=False, met=False)        # a later "no" changes nothing
    assert fp.settle(U, P, FLOWS, 'Child', later) is None
    assert later.asked == ['flow_goal_met']
    assert fp.read(U, P)['checkable'] == {'0': True}


def test_a_goal_that_cannot_be_checked_moves_after_one_finished_run(fp):
    finish(fp)
    model = Model(checkable=False)
    assert fp.settle(U, P, FLOWS, 'Child', model) == 2
    assert model.asked == ['flow_goal_checkable']    # never asked "met"


def test_an_empty_goal_moves_after_one_run_without_asking(fp):
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    finish(fp, flow=3)
    model = Model(weakest='Family member association')
    assert fp.settle(U, P, FLOWS, 'Child', model) == 2
    assert model.asked == ['flow_weakest']           # last flow: back to the weakest


def test_the_last_flow_met_goes_back_to_the_weakest_earlier_flow(fp):
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    finish(fp, flow=3)
    assert fp.settle(U, P, FLOWS, 'Child', Model(weakest='name calling response')) == 0


def test_a_weakest_flow_that_is_not_theirs_keeps_them_where_they_are(fp):
    fp.set_person_flow(U, P, FLOWS, 'Child', 3, 'therapist-7')
    finish(fp, flow=3)
    assert fp.settle(U, P, FLOWS, 'Child', Model(weakest='Family photos')) == 3


def test_a_run_whose_flow_left_its_persona_is_closed_with_a_verdict(fp):
    """The agent was re-created and flow 1 is no longer the child's: the run
    is closed with a verdict that says so, never silently dropped."""
    finish(fp, flow=1)
    model = Model(met=True)
    assert fp.settle(U, P, FLOWS, 'Child', model) is None
    assert model.asked == []
    assert fp.read(U, P)['unjudged'] == []
    assert 'no longer' in fp.read(U, P)['verdicts']['1'][-1]['evidence']


def test_the_pick_and_the_place_survive_a_restart(fp):
    fp.set_persona(U, P, 'Child', PERSONAS)
    fp.set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')
    fp._locks.clear()                                # a new process: nothing in memory
    assert fp.stored_persona(U, P, PERSONAS) == 'Child'
    assert fp.current_flow(U, P, FLOWS, 'Child') == 2


def test_a_name_that_is_not_one_of_the_agents_personas_is_not_kept(fp):
    """Review of f9951c0d2: any name the model passed was kept forever and
    the persona question never came back."""
    assert fp.set_persona(U, P, 'Mother', PERSONAS) is None
    assert fp.stored_persona(U, P, PERSONAS) is None
    assert fp.set_persona(U, P, ' child ', PERSONAS) == 'Child'     # spelled as the agent spells it
    assert fp.stored_persona(U, P, PERSONAS) == 'Child'


def test_a_kept_pick_the_agent_no_longer_has_reads_as_none(fp):
    fp.set_persona(U, P, 'Parent', PERSONAS)
    assert fp.stored_persona(U, P, ['Child', 'Therapist']) is None   # the question comes back


def test_another_personas_flow_is_refused(fp):
    assert fp.set_person_flow(U, P, FLOWS, 'Child', 1, 'therapist-7') is False
    assert fp.current_flow(U, P, FLOWS, 'Child') is None


def test_each_persona_keeps_its_own_place(fp):
    fp.set_person_flow(U, P, FLOWS, 'Child', 2, 'goal_met')
    assert fp.current_flow(U, P, FLOWS, 'Parent') is None
    fp.set_person_flow(U, P, FLOWS, 'Parent', 1, 'therapist-7')
    assert fp.current_flow(U, P, FLOWS, 'Child') == 2
    assert fp.current_flow(U, P, FLOWS, 'Parent') == 1


def test_a_pending_join_is_kept_until_cleared(fp):
    assert fp.pending_join(U, P) is None
    fp.set_pending_join(U, P, 'u-other', 'Parent', 'ask-1')
    fp._locks.clear()
    join = fp.pending_join(U, P)
    assert (join['creator'], join['persona'], join['ask_id']) == ('u-other', 'Parent', 'ask-1')
    fp.clear_pending_join(U, P)
    assert fp.pending_join(U, P) is None


def test_two_writers_in_the_same_moment_both_land(fp):
    fp.set_persona(U, P, 'Child', PERSONAS)
    barrier = threading.Barrier(2)

    def finish_run():
        barrier.wait()
        finish(fp, run='run-a')

    def move():
        barrier.wait()
        fp.set_person_flow(U, P, FLOWS, 'Child', 2, 'therapist-7')

    threads = [threading.Thread(target=finish_run), threading.Thread(target=move)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = fp.read(U, P)
    assert data['places']['child']['flow'] == 2 and [r['run'] for r in data['unjudged']] == ['run-a']
    assert data['persona'] == 'Child'
