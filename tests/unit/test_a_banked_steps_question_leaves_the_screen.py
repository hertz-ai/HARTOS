"""A CREATE step's question leaves the screen once the step is banked.

LIVE 2026-11-25: during the speech agent's CREATE, action 4 (06:51 IST)
opened an ask card, "What works best for your child?".  The action was
banked and the build moved on, but the card stayed open on the owner's
desktop, asking nothing current.  Asks of a build closed only when a run
answered that the agent was built (person_ask.create_run).

Now every place create_recipe banks or saves an action's recipe closes the
build's still-open asks through person_ask.close_create_run_asks, the path a
finished build already uses.  Its key is this build only (agent, user the
build is for): another agent's build, another user's build of the same
agent, and a REUSE ask for the same person stay open.

The harness of test_a_finished_create_run_closes_its_asks.py.

    python -m pytest tests/unit/test_a_banked_steps_question_leaves_the_screen.py -q --noconftest
"""
from __future__ import annotations

import os
import sys

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.unit.test_person_ask import (  # noqa: E402,F401
    POST, _answered_events, _ask, _no_open_asks, _row, _settle, legs)
from tests.unit.test_a_finished_create_run_closes_its_asks import (  # noqa: E402
    _create_run, _q)


def _create_recipe():
    import hartos.create_recipe  # noqa: F401
    return sys.modules['hartos.create_recipe']


def test_a_banked_actions_question_closes_and_no_other_ask_does(legs):
    from integrations.agent_engine import person_ask
    cr = _create_recipe()
    # A REUSE turn's ask of the same agent for the same person.
    reuse = _ask(component=dict(POST), agent_id='342', prompt_id='342')['ask_id']
    # Another agent's build for the same person, still running.
    other_agent, other_agent_held = _create_run(
        [_q('pick_day', 'Which day suits you?')], reply='Which day?', prompt_id='999')
    other_agent('10202', 'make me a planner', '999', None, 'msg_O')
    # Another user's build of the same agent, still running.
    other_user, other_user_held = _create_run(
        [_q('name_it', 'Call it Reely?')], reply='Call it Reely?')
    other_user('777', 'make me a reel agent', '342', None, 'msg_U')

    # The build asks during action 4, and action 4 is banked in the run.
    closed = []
    recipe, held = _create_run(
        [_q('how_child', 'What works best for your child?')],
        reply='Which sounds does she find hard?',      # the build goes on
        during=lambda: closed.append(
            cr._close_finished_action_asks('10202_342', '342', 4)))
    recipe('10202', 'build the speech agent', '342', None, 'msg_T1')
    _settle()

    assert closed == [None]
    (ask_id,) = held
    assert person_ask.ask_state(ask_id, '10202') == {'status': 'expired', 'answer': None}
    assert len(_answered_events(legs, ask_id)) == 1      # every surface drops it
    assert _row(ask_id)['is_read'] is True               # the bell stops listing it
    for other, uid in ((reuse, '10202'), (other_agent_held[0], '10202'),
                       (other_user_held[0], '777')):
        assert person_ask.ask_state(other, uid) == {'status': 'open', 'answer': None}
        assert _answered_events(legs, other) == []
        assert _row(other)['is_read'] is False


def test_a_user_id_with_an_underscore_closes_that_users_build_only(legs):
    from integrations.agent_engine import person_ask
    cr = _create_recipe()
    recipe, held = _create_run(
        [_q('how_child', 'What works best for your child?')],
        reply='Next?',
        during=lambda: cr._close_finished_action_asks('10202_342', '342', 4))
    recipe('10202', 'build the speech agent', '342', None, 'msg_T1')
    assert person_ask.ask_state(held[0], '10202')['status'] == 'expired'
    # '7_10202_342' is user '7_10202', not user '7' nor user '10202'.
    other, other_held = _create_run(
        [_q('name_it', 'Call it Reely?')], reply='Next?',
        during=lambda: cr._close_finished_action_asks('7_10202_342', '342', 4))
    other('10202', 'build the speech agent', '342', None, 'msg_T2')
    assert person_ask.ask_state(other_held[0], '10202') == {'status': 'open', 'answer': None}


def test_every_place_create_banks_an_action_closes_its_asks():
    """The wiring: each trace bank that succeeds, and each save of the
    model's recipe, is followed in its branch by the close."""
    path = os.path.join(ROOT, 'hartos', 'create_recipe.py')
    lines = open(path, encoding='utf-8').read().splitlines()
    sites = [i for i, line in enumerate(lines)
             if '_bank_action_recipe_from_trace(' in line
             and not line.lstrip().startswith('def ')]
    sites += [i for i, line in enumerate(lines)
              if "logger.info(f'Saved Individual recipe" in line]
    assert len(sites) == 5, sites
    for i in sites:
        assert any('_close_finished_action_asks(' in line
                   for line in lines[i:i + 25]), (i + 1, lines[i])
