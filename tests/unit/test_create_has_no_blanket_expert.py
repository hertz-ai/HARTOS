"""CREATE puts no word-matched expert persona into an agent's prompt.

Measured live 2026-11-04 on the owner's desktop (Reel Studio, agent 342,
llm_outbound record kept at D:/hartos94_evidence/reel_1104/
llm_outbound_342_1104.jsonl):

* instantiate_assistant_agent appended "[Expert Guidance: STEM Education
  Expert] ... math: Teach mathematics ..." to the agent's system prompt, and
* every step message ended "[Expert Tip from STEM Education Expert]: Focus on
  math, science",

because the registry's word match found "each" (from "from each clip")
inside "teaching".  Owner 2026-11-04: an expert is opted into by the agent,
when needed (its consult_expert tool), never put on every agent.

These drive the real builders with autogen's agent class replaced, and read
what the model would be sent.

    python -m pytest tests/unit/test_create_has_no_blanket_expert.py -q
"""
import json
from unittest.mock import patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from hartos import create_recipe as cr  # noqa: E402
from hartos.helper import Action  # noqa: E402
# The scripted whole-loop harness (no LLM); its fixture is used by name.
from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401
    PROMPT_ID, _MARKER, _content, _run, create_env)

#: Reel Studio's approved plan, first two steps (prompts/342.json).
REEL_PLAN = [
    'Verify the 5 clips are accessible in this chat session and confirm their exact order.',
    'Extract the key visual elements and transitions from each clip to ensure smooth '
    'continuity between them.',
]
KEY = '10202_342'


def _assistant_system_message(actions):
    with patch.object(cr, 'user_tasks', {KEY: Action(actions)}), \
            patch.object(cr, 'config_list', [{'model': 'test', 'api_key': 'test'}]), \
            patch.object(cr.autogen, 'AssistantAgent') as agent_cls:
        cr.instantiate_assistant_agent(['MUA'], KEY)
    return agent_cls.call_args.kwargs['system_message']


def test_the_registry_still_matches_an_expert_for_this_plan():
    """The precondition: the word match would pick an expert here, so the
    next test proves the builder no longer puts it on the agent."""
    from integrations.expert_agents import match_expert_for_context
    match = match_expert_for_context(str(REEL_PLAN))
    assert match is not None and match['name'] == 'STEM Education Expert'


def test_the_agent_prompt_carries_no_matched_expert():
    system_message = _assistant_system_message(REEL_PLAN)
    assert '[Expert Guidance' not in system_message
    assert 'STEM Education Expert' not in system_message
    # The plan itself is still given to the agent, once.
    assert REEL_PLAN[1] in system_message


def test_a_step_line_is_the_step_and_her_words_only():
    line = cr.step_instruction(2, REEL_PLAN[1], 'Make a 20 second Instagram reel from my clips')
    assert line == ('Execute Action 2: ' + REEL_PLAN[1]
                    + ' ,Latest User message: Make a 20 second Instagram reel from my clips')
    assert '[Expert Tip' not in line


def test_every_step_the_loop_posts_is_the_step_and_her_words_only(create_env):
    """The create loop's own step lines, not just the builder: the real loop
    runs her plan (whose words match the STEM expert) with scripted agents,
    and no step it posts carries an expert tip.  Review of 5b7539dc8:
    mutant M1b (the main loop's line regains the tip) survived the builder
    test above."""
    env = create_env
    cfg_path = env.prompts / f'{PROMPT_ID}.json'
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    cfg['flows'][0]['actions'] = list(REEL_PLAN)
    cfg_path.write_text(json.dumps(cfg), encoding='utf-8')
    replies = _run(env, turns=1)
    steps = [_content(m) for m in env.script.gc.messages if _MARKER.search(_content(m))]
    assert steps, f'no step was posted; replies {replies!r}'
    assert not [t for t in steps if '[Expert Tip' in t], steps
    assert steps[0].startswith(cr.step_instruction(1, REEL_PLAN[0], '')[:-1]), steps[0]
