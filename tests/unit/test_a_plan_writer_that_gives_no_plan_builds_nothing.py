"""A plan writer that gives no plan builds nothing: no placeholder is saved.

Measured on central 2026-11-20, goal 5ba50f98 (rice order desk), build
e2bf4f9.  Every gather_info call from 12:14:50Z to 12:15:00Z failed (the
planner's model answered 429).  _autonomous_gather_info sent 'proceed' five
more times, then saved a placeholder as the agent (status 'pending', one flow
'main', one step "Respond to user", the whole sales prompt as its goal) to
/app/data/data/prompts/89682852097.json at 12:15:00Z.  The daemon's /chat
built whatever config file existed, so CREATE ran "Respond to user" and the
turn answered 'Agent Created Successfully'.  The hive task was recorded done,
and at 12:16:03Z the goal was COMPLETED (completion_grounding
'ledger_tasks_complete').  No outreach and no draft was made.

_buildable_completed_config is the one judge of whether a plan can be built.
A run that never gets a plan it accepts now saves nothing.  It answers the
failed-turn sentence that the hive worker and dispatch_goal read
(core.agent_tools.is_user_facing_error).  The worker releases the task for a
retry instead of recording a result.  The next claim finds no config, so it
plans again.  A failed model answer also ends the asking at once.

The run drives the real _autonomous_gather_info with a fake gather_info and a
fake reviewer (the approved-plan tests' harness).
"""
# The harness: one copy, owned by the approved-plan tests.
from tests.unit.test_the_approved_plan_is_the_saved_plan import (  # noqa: F401
    hie, run,
)

# What gather_info answered on central: get_agent_response turns the
# planner's RateLimitError into the failed-turn sentence.
MODEL_FAILED = ("I couldn't finish that: Error code: 429 - Rate limit exceeded: "
                "free-models-per-day")
NO_PLAN = 'I could not finish the plan.'


def _no_plan_reply(reply):
    from core.agent_tools import is_user_facing_error
    return is_user_facing_error(reply) and 'nothing was built' in reply


def test_a_planner_whose_model_fails_saves_no_placeholder(run):
    """FAILS BEFORE THE FIX: the placeholder 'Respond to user' was saved."""
    run.answers = [MODEL_FAILED] * 6
    reply = run.call(user='system_daemon', background=True,
                     request_id='daemon_5ba50f98')
    assert not run.saved_path.exists(), (
        'a placeholder was saved as the agent: the daemon builds it and the '
        'goal completes with nothing done')
    assert _no_plan_reply(reply), reply


def test_a_failed_model_answer_is_not_asked_again_in_the_same_turn(run):
    """FAILS BEFORE THE FIX: 'proceed' went to a model that had just failed,
    five times (central 12:14:52Z to 12:15:00Z, every call 429)."""
    run.answers = [MODEL_FAILED] * 6
    run.call()
    assert run.sent == ['Create an agent named "Syllable Coach"']


def test_a_planner_that_answers_no_plan_saves_no_placeholder(run):
    """An answer with no plan in it (not a failure sentence) is still asked
    to proceed, as before.  After the last try nothing is saved (FAILS BEFORE
    THE FIX)."""
    run.answers = [NO_PLAN] * 6
    reply = run.call()
    assert run.sent.count('proceed') == 5
    assert not run.saved_path.exists()
    assert _no_plan_reply(reply), reply
