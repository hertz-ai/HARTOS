"""The agent's personality is the agent's own, never one of the people it
talks with.

494d5a4e1 made a plan's personas the PEOPLE its flows talk with (the child, a
parent, a therapist); its review (REJECTED 11-04, must-fix 1) measured that
create_recipe built the agent's OWN personality from personas[0], so the line
every CREATE and REUSE turn carries read "You are speech.local.coach, a the
child who embodies these cultural traditions:".  The role is now the agent's
(AGENT_ROLE, 'assistant'), and the article follows the role.  Live 11-07 on build67 (agent
1794031134533): no saved role is a person yet only because the 4B still
copies 'Executor'.

Real create_agents and create_agents_for_user (the CREATE assistant, then a
REUSE turn that loads the personality CREATE saved), real agent_personality.

    python -m pytest tests/unit/test_the_agent_personality_is_the_agents_own.py -q
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401  (fixture)
    ACTIONS, PROMPT_ID, UP, USER_ID, create_env)
from core import agent_personality as _personality  # noqa: E402
from core import resonance_profile as _resonance  # noqa: E402

PEOPLE = ['the child', 'Parent', 'Therapist']

# create_env stubs the personality store (load gives None, save does nothing)
# and makes the resonance profile raise.  This file is about what CREATE
# saves and REUSE loads, so the pipelines fixture puts the real ones back;
# they are taken here, at import, before any fixture has patched them.
_REAL_STORE = ((_personality, 'load_personality', _personality.load_personality),
               (_personality, 'save_personality', _personality.save_personality),
               (_resonance, 'get_or_create_profile', _resonance.get_or_create_profile))


def _line(text):
    """The personality's 'You are ...' line from a system message."""
    for line in str(text or '').splitlines():
        if line.startswith('You are ') and 'cultural traditions' in line:
            return line
    raise AssertionError('no personality line in the system message')


# -- the line itself -------------------------------------------------------

@pytest.mark.parametrize('role, expected', [
    ('assistant', 'You are Coach, an assistant who embodies these cultural traditions:'),
    ('Executor', 'You are Coach, an Executor who embodies these cultural traditions:'),
    ('Researcher', 'You are Coach, a Researcher who embodies these cultural traditions:'),
    ('', 'You are Coach who embodies these cultural traditions:'),
])
def test_the_personality_line_reads_right_for_every_saved_role(role, expected):
    """A saved personality keeps its role; the article follows it, and an
    empty role (one exists on the owner's desktop) gets none."""
    from core.agent_personality import AgentPersonality, build_personality_prompt
    p = AgentPersonality(agent_name='Coach', role=role, persona_name='Coach',
                         primary_traits=[], tone='warm-casual',
                         greeting_style='Hi!', self_awareness_prompt='')
    assert _line(build_personality_prompt(p)) == expected


@pytest.mark.parametrize('role, expected', [
    ('assistant', 'Coach, an assistant'), ('Executor', 'Coach, an Executor'),
    ('Researcher', 'Coach, a Researcher'), ('', 'Coach'), ('  ', 'Coach'), (None, 'Coach'),
])
def test_describe_agent_never_leaves_an_article_before_nothing(role, expected):
    from core.agent_personality import describe_agent
    assert describe_agent(SimpleNamespace(persona_name='Coach', role=role)) == expected


# -- CREATE then REUSE -----------------------------------------------------

@pytest.fixture
def pipelines(create_env, tmp_path, monkeypatch):
    """The plan names people; build the CREATE agents, then a REUSE turn's,
    the way tests/unit/test_reel_tools_wired_both_pipelines.py does."""
    env = create_env
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'data'))
    from core import platform_paths
    platform_paths.reset_cache()
    # The real store, under this test's data dir (NUNBA_DATA_DIR above).
    for module, name, real in _REAL_STORE:
        monkeypatch.setattr(module, name, real)
    from integrations.reel_studio import jobs
    monkeypatch.setattr(jobs, '_SWEPT', True)
    import hartos.reuse_recipe as rr
    monkeypatch.setattr(rr, 'PROMPTS_DIR', str(env.prompts))
    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    caches = ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
              'user_agents', 'time_actions', 'user_ledgers')
    for name in caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend', lambda *a, **k: InMemoryBackend())
    (env.prompts / f'{PROMPT_ID}.json').write_text(json.dumps({
        'personas': [{'name': n, 'description': 'a person this agent talks with'} for n in PEOPLE],
        'goal': 'Help a child respond to their name', 'agent_name': 'speech.local.coach',
        'flows': [{'flow_name': 'Name calling', 'persona': 'the child',
                   'sub_goal': 'the child turns when called', 'actions': list(ACTIONS)}],
    }), encoding='utf-8')
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [
            {'action_id': i, 'action': text, 'persona': 'the child',
             'can_perform_without_user_input': 'yes',
             'recipe': [{'steps': 'step %d' % i, 'tool_name': 'google_search'}]}
            for i, text in enumerate(ACTIONS, 1)]}), encoding='utf-8')

    def create():
        from hartos.helper import Action
        with env.app.app_context():
            env.cr.user_tasks[UP] = Action(list(ACTIONS))
            return env.cr.create_agents(USER_ID, env.cr.user_tasks[UP], PROMPT_ID)[6]['assistant']

    def reuse():
        with env.app.app_context():
            return rr.create_agents_for_user(USER_ID, PROMPT_ID)[0]
    yield SimpleNamespace(create=create, reuse=reuse)
    for name in caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    platform_paths.reset_cache()


def test_a_plan_that_names_people_gives_the_agent_its_own_role(pipelines):
    line = _line(pipelines.create().system_message)
    assert line.startswith('You are speech.local.coach, an assistant who embodies'), line
    for person in PEOPLE:
        assert person.lower() not in line.lower(), line


def test_a_reuse_turn_carries_the_same_role_create_saved(pipelines):
    created = _line(pipelines.create().system_message)
    saved = _personality.load_personality(str(PROMPT_ID))
    assert saved is not None, 'CREATE saved no personality for REUSE to load'
    assert saved.role == 'assistant', saved.role
    assert _line(pipelines.reuse().system_message) == created
    from core.agent_personality import AGENT_ROLE
    assert saved.role == AGENT_ROLE
