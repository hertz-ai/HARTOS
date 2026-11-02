"""An autonomous creation never saves, as built, a config that cannot be built.

#206, and F2 of the cdeca7500 review (ran by its reviewer, 2026-11-02): once
_autonomous_gather_info finalized an approved plan in code instead of asking
the model to re-emit it, nothing between the approval and the save checked
that the plan could be built.  A plan with no `flows` (an oscillation from 4+
steps to 0, or a reviewer that approves on failure) was saved with status
'completed', and recipe() then fails on config['flows']
(hartos/create_recipe.py:1097, :7131).  The legacy loop's save had no check
either.

The interactive path already has the rule (#205/#718): the config's list
fields written as JSON text are decoded (_decode_config_lists), and a
'completed' config that is not _config_is_buildable is never saved as built.
Both autonomous save sites now apply the same two helpers; a config that
cannot be built is treated as not completed, so creation ends in the existing
failed-creation save (status 'pending', #718) unless a later answer can be
built.

    python -m pytest tests/unit/test_autonomous_creation_saves_only_buildable.py -q
"""
import json
import logging

# The harness that drives the real _autonomous_gather_info with a fake
# gather_info and a fake reviewer: one copy, owned by the approved-plan tests.
from tests.unit.test_the_approved_plan_is_the_saved_plan import (  # noqa: F401
    CREATOR, PID, USER, _actions, _plan, hie, run,
)

NOTHING = 'I could not finish the plan.'


def test_an_approved_plan_with_no_flows_is_not_saved_as_built(run, caplog):
    run.answers = [_plan('proposed_plan', CREATOR, flows=[])] + [NOTHING] * 5
    run.verdicts = [('approved', '')]
    with caplog.at_level(logging.WARNING):
        saved = run.go()
    assert saved['status'] != 'completed', (
        'a plan with no flows was saved as a built agent; recipe() fails on it')
    assert saved['status'] == 'pending'
    assert any('[EMPTY-BUILD]' in r.getMessage() and str(PID) in r.getMessage()
               for r in caplog.records), 'the refusal is not logged'


def test_flows_written_as_json_text_are_saved_as_a_list(run):
    flows = [{'flow_name': 'Syllable exercise ball', 'persona': 'Speech Companion',
              'actions': CREATOR, 'sub_goal': 'Arjun says ball'}]
    run.answers = [_plan('proposed_plan', CREATOR, flows=json.dumps(flows))]
    run.verdicts = [('approved', '')]
    saved = run.go()
    assert saved['status'] == 'completed'
    assert isinstance(saved['flows'], list), (
        'flows was saved as JSON text, which no reader can build from')
    assert _actions(saved) == CREATOR


def test_a_completed_answer_with_an_empty_step_waits_for_one_that_builds(run):
    """The legacy loop's save: the first answer is 'completed' with a step
    that is no step; it is not saved, and the next answer that can be built
    is."""
    broken = CREATOR[:2] + ['   ']
    run.answers = [_plan('completed', broken), _plan('completed', CREATOR)]
    run.verdicts = []
    saved = run.go()
    assert _actions(saved) == CREATOR, (
        'the config with an empty step was saved as built')
    assert saved['status'] == 'completed'
    assert 'proceed' in run.sent


def test_a_plan_that_can_be_built_is_saved_as_before(run):
    run.answers = [_plan('proposed_plan', CREATOR)]
    run.verdicts = [('approved', '')]
    saved = run.go()
    assert saved['status'] == 'completed'
    assert _actions(saved) == CREATOR
    assert (saved['prompt_id'], saved['creator_user_id']) == (PID, USER)
    assert run.sent == ['Create an agent named "Syllable Coach"']
