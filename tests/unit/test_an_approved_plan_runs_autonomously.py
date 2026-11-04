"""An agent the person already approved is finished with the AUTONOMOUS prompt.

chat()'s config-without-recipe branch (hart_intelligence_entry, steward rule
2026-08-22) "finishes the recipe AUTONOMOUSLY".  Measured live 2026-11-04 on
the owner's desktop: Reel Studio (agent 342) went down that branch on her own
request id, CREATE's only autonomy test was the daemon_ prefix, so the
assistant was told "INTERACTIVE MODE: You may ask the user clarifying
questions" and the approved plan stalled on step 2 asking her for clips it
already had.  Owner 2026-11-04: "approved plan shd run autonomously".

The flag travels recipe() -> get_response_group() -> create_agents() ->
instantiate_assistant_agent(autonomous=...).  These drive the real functions
with autogen's classes replaced (the seam tests/unit/test_agent_creation.py
uses, with its prompt-config fixtures) and read what the assistant is built
with.

    python -m pytest tests/unit/test_an_approved_plan_runs_autonomously.py -q
"""
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from hartos import create_recipe as cr  # noqa: E402
from hartos.helper import Action  # noqa: E402

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
