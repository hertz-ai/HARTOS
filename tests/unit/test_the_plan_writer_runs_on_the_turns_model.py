"""The plan writer of a turn runs on that turn's model.

Measured on central 2026-11-20, goal 5ba50f98 (rice order desk), build
e2bf4f9: its daemon turn ran on the expert.  The agent matcher (12:14:48Z) and
the CREATE group chat (12:15:08Z) both called claude-haiku-5-5 and got 200.
The plan writer between them, gather_info (12:14:50Z to 12:15:00Z), called
openrouter/free 18 times and got 18 answers of 429.  gather_info built its
agent on get_autogen_config_list(), the node's own model.  Every other CREATE
agent reads hartos.helper.get_llm_config, which puts the turn's model
(/chat's model_config override) first.  With no plan, the turn saved the
"Respond to user" placeholder, and the goal was completed with nothing done.

These build the real gather agents (create_agents_for_user) and read the
model each one was given.
"""
import os
import tempfile

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')

EXPERT = {'model': 'claude-haiku-5-5', 'api_key': 'k-test',
          'base_url': 'https://api.anthropic.com/v1'}


@pytest.fixture
def turn():
    """Sets this thread's turn model the way /chat does, and clears it."""
    from hartos.threadlocal import thread_local_data
    yield thread_local_data.set_model_config_override
    thread_local_data.clear_model_config_override()


def _models(agent):
    return [c.get('model') for c in agent.llm_config['config_list']]


@pytest.mark.parametrize('autonomous', [True, False])
def test_the_plan_writer_is_built_on_the_turns_model(turn, autonomous):
    """FAILS BEFORE THE FIX: the writer got the node's own model."""
    from hartos.gather_agentdetails import create_agents_for_user
    turn([dict(EXPERT)])
    assistant, _proxy = create_agents_for_user(
        'system_daemon', autonomous=autonomous,
        initial_description='find rice buyers' if autonomous else None,
        goal_tags=['outreach', 'sales'])
    assert _models(assistant) == ['claude-haiku-5-5']


def test_with_no_turn_model_the_plan_writer_runs_on_the_nodes_own(turn):
    """A turn that names no model keeps the node's own, as before (passes
    before the fix too)."""
    from core.autogen_config import get_autogen_config_list
    from hartos.gather_agentdetails import create_agents_for_user
    turn(None)
    assistant, _proxy = create_agents_for_user(
        'u-person', autonomous=True, initial_description='plan a lesson')
    assert _models(assistant) == [c.get('model') for c in get_autogen_config_list()]
