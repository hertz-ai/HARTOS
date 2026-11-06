"""An agent the person already approved is finished with the AUTONOMOUS prompt.

chat()'s config-without-recipe branch (hart_intelligence_entry, steward rule
2026-08-22) "finishes the recipe AUTONOMOUSLY".  Measured live 2026-11-04 on
the owner's desktop: Reel Studio (agent 342) went down that branch on her own
request id, CREATE's only autonomy test was the daemon_ prefix, so the
assistant was told "INTERACTIVE MODE: You may ask the user clarifying
questions" and the approved plan stalled on step 2 asking her for clips it
already had.  Owner 2026-11-04: "approved plan shd run autonomously".

The flag travels recipe() -> get_response_group() -> create_agents() ->
instantiate_assistant_agent(autonomous=...).  The first tests drive the real
functions with autogen's classes replaced (the seam
tests/unit/test_agent_creation.py uses, with its prompt-config fixtures) and
read what the assistant is built with.

The last two run the whole create loop with scripted agents and no LLM
(tests/unit/test_create_loop_end_to_end.py's harness) and read each
verifier the run builds.  Review of ed427a072, probe P2: the next flow's
agents were rebuilt INTERACTIVE.  Probe P1 (a daemon step that needs a
person is retried 3 times before the owner is asked) is what the owner
chose on 2026-11-05: "Retry first, then ask".

    python -m pytest tests/unit/test_an_approved_plan_runs_autonomously.py -q
"""
import faulthandler
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from hartos import create_recipe as cr  # noqa: E402
from hartos.helper import Action  # noqa: E402
# The scripted whole-loop harness; its fixture is used by name below.
from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401
    ACTIONS, PROMPT_ID, UP, USER_ID, _MARKER, _content, create_env)

PLAN = ['Verify the clips are accessible.', 'Make the reel from the clips she sent.']


@pytest.fixture
def assistant_mode(test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
    """create_agents(..., autonomous=?) -> the ``autonomous`` it hands
    instantiate_assistant_agent."""
    up = f'{test_user_id}_{test_prompt_id}'

    def run(autonomous):
        task = Action(PLAN)
        with patch.object(cr, 'user_tasks', {up: task}), \
                patch.object(cr, 'config_list', [{'model': 'test', 'api_key': 'test'}]), \
                patch('hartos.create_recipe.autogen.AssistantAgent', return_value=MagicMock()), \
                patch('hartos.create_recipe.autogen.UserProxyAgent', return_value=MagicMock()), \
                patch('hartos.create_recipe.autogen.GroupChat', return_value=MagicMock()), \
                patch('hartos.create_recipe.autogen.GroupChatManager', return_value=MagicMock()), \
                patch.object(cr, 'instantiate_assistant_agent',
                             return_value=MagicMock()) as built:
            cr.create_agents(test_user_id, task, test_prompt_id, autonomous=autonomous)
        return built.call_args.kwargs['autonomous']
    return run


def test_an_approved_plan_gets_the_autonomous_assistant(assistant_mode):
    assert assistant_mode(True) is True


def test_a_person_turn_without_the_flag_stays_interactive(assistant_mode):
    # No daemon_ request id in this thread: the existing rule still decides.
    assert assistant_mode(False) is False


def test_get_response_group_hands_the_flag_to_create_agents(mock_flask_app):
    seen = {}

    class _Built(Exception):
        pass

    def _create(user_id, task, prompt_id, autonomous=False):
        seen['autonomous'] = autonomous
        raise _Built()

    with patch.object(cr, 'user_tasks', {'10202_342': Action(PLAN)}), \
            patch.object(cr, 'user_agents', {}), \
            patch.object(cr, 'create_agents', side_effect=_create):
        try:
            cr.get_response_group('10202', 'make my reel', '342', autonomous=True)
        except _Built:
            pass
    assert seen == {'autonomous': True}


# ── the whole loop ────────────────────────────────────────────────────────────

def _verifier_hard_rules(env, monkeypatch):
    """One entry per verifier the run builds: is the HARD RULE in its prompt?"""
    built = []
    real = env.cr.instantiate_status_verifier_agent

    def _build(user_prompt, autonomous=False):
        agent = real(user_prompt, autonomous=autonomous)
        built.append(env.cr._USER_INPUT_HARD_RULE in agent.system_message)
        return agent
    monkeypatch.setattr(env.cr, 'instantiate_status_verifier_agent', _build)
    return built


def _finish_approved_plan(env, request_id, turns=3):
    """The turns of a caller finishing an approved plan:
    recipe(..., autonomous=True), as chat() calls it."""
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id(request_id)
    faulthandler.dump_traceback_later(300, exit=True)
    replies = []
    with env.app.app_context():
        for _ in range(turns):
            replies.append(env.cr.recipe(USER_ID, 'Build the agent now', PROMPT_ID,
                                         None, request_id, autonomous=True))
            if env.cr.scheduler_check.get(UP):
                break
    return replies


def _two_flow_plan(env):
    """The harness's one-flow plan with a second flow (same steps, another
    persona) after it."""
    cfg_path = env.prompts / f'{PROMPT_ID}.json'
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    cfg['personas'].append({'name': 'Reviewer', 'description': 'Reviews the summary.'})
    cfg['flows'].append({'persona': 'Reviewer', 'sub_goal': 'review the summary',
                         'actions': list(ACTIONS)})
    cfg_path.write_text(json.dumps(cfg), encoding='utf-8')


def test_every_flow_of_a_plan_she_approved_is_built_freed(create_env, monkeypatch):
    """A two-flow plan she approved, finished on her request id: flow 1's
    agents are built as flow 0's were, with no HARD RULE in the verifier."""
    env = create_env
    _two_flow_plan(env)
    hard_rule = _verifier_hard_rules(env, monkeypatch)

    replies = _finish_approved_plan(env, 'her-request-1')

    assert (env.prompts / f'{PROMPT_ID}_1_recipe.json').exists(), (
        f'the run never finished flow 1; replies {replies!r}')
    assert len(hard_rule) >= 2, f'one verifier per flow expected: {hard_rule!r}'
    assert not any(hard_rule), (
        f'a flow of her approved plan was built with the HARD RULE: {hard_rule!r}')


def test_a_plan_resumed_after_a_restart_moves_to_its_next_flow_freed(
        create_env, monkeypatch):
    """After a restart the run resumes where its files say.  Flow 0's three
    steps are saved but its flow recipe is not, so the turn starts past flow
    0's last step and the boundary check (safe_action_boundary_check) moves
    the run to flow 1 and rebuilds its agents.  They are built freed, as
    flow 0's were.  Rework of ed427a072, mutants M4c/M4d (the boundary check
    not given the flag, or dropping it on its way to the next flow) survived
    the test above, which ends flow 0 on the loop's own path.

    The run is stopped where recipe() turns to the timer agents, after every
    flow ran: that step opens each flow's recipe, and the boundary check
    moved on without writing flow 0's (measured: FileNotFoundError on
    424201_0_recipe.json, with or without this change).  That is reported
    on its own; this test is about how flow 1 was built."""
    env = create_env
    _two_flow_plan(env)
    for n, action in enumerate(ACTIONS, start=1):
        (env.prompts / f'{PROMPT_ID}_0_{n}.json').write_text(json.dumps({
            'status': 'done', 'action_id': n, 'action': action,
            'recipe': [{'steps': f'search step {n}', 'tool_name': 'google_search'}]}),
            encoding='utf-8')
    hard_rule = _verifier_hard_rules(env, monkeypatch)

    class _EveryFlowRan(Exception):
        pass

    def _timer_step(flows, number_of_flows, *a, **k):
        raise _EveryFlowRan(number_of_flows)
    monkeypatch.setattr(env.cr, 'create_time_agents_and_create_scheduled_jobs', _timer_step)

    with pytest.raises(_EveryFlowRan):
        _finish_approved_plan(env, 'her-request-2', turns=1)

    assert (env.prompts / f'{PROMPT_ID}_1_recipe.json').exists(), 'flow 1 never finished'
    assert len(hard_rule) >= 2, f'flow 1 was never built: {hard_rule!r}'
    assert not any(hard_rule), (
        f'the resumed plan moved to flow 1 with the HARD RULE: {hard_rule!r}')


def test_a_daemon_step_that_needs_a_person_is_retried_then_handed_to_the_owner(
        create_env, monkeypatch):
    """Owner 2026-11-05 ("Retry first, then ask"): a daemon run is freed
    too.  Its verifier has no HARD RULE, and a step it holds for a person
    goes back to the agent before the owner is asked, once, with the
    attempts as the reason (ruling 2026-10-31: the ask reaches the owner)."""
    env = create_env
    env.script.pending_ids = {2}          # the verifier: action 2 needs a person
    asks = []
    import integrations.agent_engine.goal_manager as gm
    monkeypatch.setattr(gm.GoalManager, 'escalate_goal', staticmethod(
        lambda db, goal_id, escalation: asks.append(escalation) or {'success': True}))
    import integrations.social.models as models

    @contextmanager
    def _no_db(commit=False):
        yield None
    monkeypatch.setattr(models, 'db_session', _no_db)
    hard_rule = _verifier_hard_rules(env, monkeypatch)

    replies = _finish_approved_plan(env, 'daemon_e2e_create', turns=1)

    posts = [int(n) for m in env.script.gc.messages
             for n in _MARKER.findall(_content(m))]
    assert posts.count(2) > 1, f'action 2 was not retried: {posts!r}'
    assert [e.get('action_id') for e in asks] == [2], (asks, replies)
    assert 'attempts' in asks[0].get('reason', ''), asks
    assert 'Paused for help' in str(replies), replies
    assert hard_rule and not any(hard_rule), hard_rule
