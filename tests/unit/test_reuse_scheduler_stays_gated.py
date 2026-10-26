"""After any attach, the reuse agent's create_scheduled_jobs is still the gated one.

fe1100b5e made the reuse main leg's own create_scheduled_jobs refuse a
background turn: a non-empty request id that dispatch.is_genuine_user_request
rejects (a daemon_ id) may not start a recurring job.  Measured 2026-10-20 in
Nunba's gui_app.log: a daemon turn of agent 65708210992 scheduled '* * * * *'
and the job ran a time-agent group chat every minute, 240 runs, until the log
ended.

Its review found a way round it, and this file holds it shut.  Two paths put a
tool on the main leg BY NAME after the agents are built, and both read one
source, ``assistant._hart_core_tools`` (set in create_agents_for_user):

  * the per-action named attach (_attach_named_tools_for_action ->
    core.agent_tool_menu.attach_for_names), for an action whose recipe names
    create_scheduled_jobs; and
  * request_tools (discover_and_attach), for a need like "schedule a
    recurring job".

That source held every build_core_tool_closures closure, the factory's
create_scheduled_jobs among them: CREATE's stub, which schedules nothing and
answers "...you can go ahead and mark this action as completed".  The attach
ledger (_attached_names) holds only service tools, so either path registered
the stub under the gated tool's name, replacing it.  A daemon turn then got the
stub's line, which tool_reply_failed does not read as a failure, so the
fabrication gate counted it as the work done; and a person's request scheduled
nothing.

The agents are built by the REAL reuse_recipe.create_agents_for_user, as
test_reuse_vlm_learning_files_its_action builds them (the create-loop
fixture's scripted, no-LLM, no-network patches).  A call is made the way the
group makes it: arguments from the schema the Helper is offered, routed to
the agent _agent_that_executes picks, run by autogen's execute_function, with
the turn's id where chat_agent writes it (request_id_list).

    python -m pytest tests/unit/test_reuse_scheduler_stays_gated.py -q -p no:cacheprovider
"""
import json
from unittest import mock

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    PROMPT_ID, UP, USER_ID, create_env)

TOOL = 'create_scheduled_jobs'
TASK = 'Collect inference feedback logs from all distributed training nodes'
DAEMON_RID = 'daemon_85d091d3-dc4e-4a08-b0ca-403c487bf53f'
PERSON_RID = 'msg_1792513278'
# A value for every parameter either same-named tool takes: the reuse tool's
# (cron_expression, job_description) and the factory stub's interval_sec.
ARG_VALUES = {'cron_expression': '* * * * *', 'job_description': TASK,
              'interval_sec': 60}


def _offered_params(agent):
    """The parameter names ``agent`` is offered for TOOL, or None."""
    for entry in (agent.llm_config or {}).get('tools') or []:
        fn = entry.get('function') or {}
        if fn.get('name') == TOOL:
            return set((fn.get('parameters') or {}).get('properties') or {})
    return None


@pytest.fixture
def reuse_agents(create_env, monkeypatch):
    """The reuse agents for a saved flow whose action 1 names TOOL."""
    env = create_env
    import hartos.reuse_recipe as rr
    import core.llm_outbound_logger as lol
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    for name in ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
                 'user_agents', 'time_actions', 'user_ledgers'):
        cache = getattr(rr, name)
        if hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
        cache.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend',
                            lambda *a, **k: InMemoryBackend())
    # No llama-server here: the per-turn fit must not ask /props for its room.
    monkeypatch.setattr(lol, 'schema_token_room', lambda: 100_000)
    scheduler = mock.Mock(spec=BackgroundScheduler)
    scheduler.running = True
    monkeypatch.setattr(rr, 'scheduler', scheduler)
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [{
            'action_id': 1, 'action': 'Run the node health check every hour',
            'persona': 'Researcher', 'can_perform_without_user_input': 'yes',
            'recipe': [{'steps': 'schedule the health check to run hourly',
                        'tool_name': TOOL}]}]}), encoding='utf-8')

    with env.app.app_context():
        built = rr.create_agents_for_user(USER_ID, PROMPT_ID)
    rr.user_agents[UP] = built
    # What the agents were built with: the Helper is offered the gated tool.
    built_params = _offered_params(built[4])
    assert built_params, f'the built Helper is not offered {TOOL}'
    yield env, rr, built, scheduler, built_params
    rr.user_agents.pop(UP, None)
    rr.request_id_list.pop(UP, None)


def _named_attach(env, rr, built):
    assert rr._reuse_action_tool_names(UP, 1) == [TOOL]
    with env.app.app_context():
        attached = rr._attach_named_tools_for_action(UP)
    assert attached is not False, 'the named attach did not run'


def _request_tools(env, rr, built):
    with env.app.app_context():
        reply = built[0]._function_map['request_tools'](
            need='schedule a recurring job')
    assert TOOL in reply, reply


ATTACHES = {'named attach': _named_attach, 'request_tools': _request_tools}


def _call_as_the_group_does(env, rr, built, request_id):
    """Run TOOL for a turn whose id is ``request_id``; return its reply."""
    group_chat, helper = built[2], built[4]
    params = _offered_params(helper)
    assert params, f'the Helper is no longer offered {TOOL}'
    arguments = json.dumps({p: ARG_VALUES[p] for p in sorted(params)})
    agent, _ = rr._agent_that_executes(group_chat, {'tool_calls': [{
        'id': 'call_sched_1', 'type': 'function',
        'function': {'name': TOOL, 'arguments': arguments}}]})
    assert agent is not None, f'no agent in the group can execute {TOOL}'
    rr.request_id_list[UP] = request_id
    with env.app.app_context():
        _ok, result = agent.execute_function(
            {'name': TOOL, 'arguments': arguments})
    return result['content']


@pytest.mark.parametrize('attach', sorted(ATTACHES))
def test_a_daemon_turn_is_still_refused(reuse_agents, attach):
    from core.constants import tool_reply_failed
    env, rr, built, scheduler, _built_params = reuse_agents
    ATTACHES[attach](env, rr, built)
    out = _call_as_the_group_does(env, rr, built, DAEMON_RID)
    scheduler.add_job.assert_not_called()
    assert tool_reply_failed(out), (
        f'after the {attach}, a daemon turn got a reply the fabrication gate '
        f'reads as the work done: {out!r}')


@pytest.mark.parametrize('attach', sorted(ATTACHES))
def test_a_persons_request_still_schedules(reuse_agents, attach):
    env, rr, built, scheduler, _built_params = reuse_agents
    ATTACHES[attach](env, rr, built)
    out = _call_as_the_group_does(env, rr, built, PERSON_RID)
    assert out == 'Successfully created scheduler job', (
        f"after the {attach}, a person's request was answered {out!r}")
    scheduler.add_job.assert_called_once()
    args, kwargs = scheduler.add_job.call_args
    assert args[0] is rr.execute_python_file
    assert kwargs['args'] == [TASK, USER_ID, PROMPT_ID, 0]


def test_the_named_attach_offers_both_seats_the_tool_it_was_built_with(
        reuse_agents):
    """The action names the tool, so it reaches the seat that proposes its
    own calls (the Assistant) as well as the Helper, as attach_for_names
    documents -- with the schema of the tool the agent was built with, never
    the factory stub's."""
    env, rr, built, _scheduler, built_params = reuse_agents
    _named_attach(env, rr, built)
    for seat in (built[4], built[0]):
        assert _offered_params(seat) == built_params, (
            seat.name, _offered_params(seat), built_params)
