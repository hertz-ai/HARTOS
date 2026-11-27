"""A finished build writes no recipe file for a flow the plan does not have.

MEASURED 2026-11-26 on the owner's desktop: 56 of 1274 agents with a plan
have <prompt>_<flow count>_recipe.json (dated 2026-09-10 to today), one past
the last flow, a copy of the last flow's recipe written about 1 s after it.
create_recipe.recipe()'s build end wrote it: [ALL-FLOWS-DONE] moves the flow
index past the last flow, and the block then saved the last flow's recipe,
just read from its own file, under that index.  REUSE never reads it; the
A2A registry advertised it as a "Flow N" agent, the home recipe cards showed
it twice, and recipe_sync carried it to central.

    python -m pytest tests/unit/test_a_finished_build_writes_no_extra_flow_recipe.py -q -p no:cacheprovider --noconftest
"""
import json

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    PROMPT_ID, _run, create_env)


@pytest.fixture
def idle_desktop(monkeypatch):
    """tests/conftest.py's idle_desktop, here so the file also runs under
    --noconftest: nobody is at this computer, so no background gate says stop."""
    from core import resource_governor
    monkeypatch.setattr(resource_governor, 'get_physical_input_state',
                        lambda **_kw: (0, None))
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, 'person_using_the_machine', lambda: None)
    monkeypatch.setattr(dispatch, 'should_yield_to_user', lambda: False)


def test_a_finished_build_saves_one_recipe_per_flow_and_no_more(create_env, monkeypatch):
    env = create_env
    import core.llm_json
    # The node's model, asked by the flow end (dependencies, schedule):
    # nothing depends on anything and no job is set.
    monkeypatch.setattr(core.llm_json, 'ask_model_json',
                        lambda content, schema, name, **kw:
                        {key: [] for key in schema['properties']})
    flows = json.loads((env.prompts / f'{PROMPT_ID}.json').read_text(
        encoding='utf-8'))['flows']

    replies = _run(env, turns=1)

    assert replies == ['Agent Created Successfully'], replies
    for n in range(len(flows)):
        assert (env.prompts / f'{PROMPT_ID}_{n}_recipe.json').exists(), n
    extra = env.prompts / f'{PROMPT_ID}_{len(flows)}_recipe.json'
    assert not extra.exists(), (
        f'the build end wrote {extra.name}, a recipe for a flow the plan '
        f'does not have')
