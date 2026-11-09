"""A REUSE turn tells the agent who IT is, never that it is the person it
serves.

The REUSE assistant's prompt opened "You are a Helpful {role} Assistant",
where {role} is the persona of the person this turn serves.  Once a plan's
personas are people (494d5a4e1) that read "You are a Helpful the child
Assistant" on every turn; with agent names as personas it read "a Helpful
Executor Assistant" (owner 11-07: a user persona is never an agent name and
the reverse; found by hartos-16 in the sweep of that ruling).  The line now
takes the agent's own role from core.agent_personality.AGENT_ROLE, the one
source CREATE's personality uses too.

What must NOT change: {role} where it names the person served ("Only execute
actions where the persona is: {role}") and the action filter, which runs on
the same role.  Each case below asserts the actions the turn keeps, so a
change that moves the filter fails here, not on a live agent.

Real create_agents_for_user over saved files, one agent per shape seen on the
owner's desktop 11-07 (count only): Executor-only (536 agents declare it),
the failed-create fallback plan (persona 'Assistant', 422), people personas
(177 people-only), and an empty role on the turn.

    python -m pytest tests/unit/test_a_reuse_turn_never_tells_the_agent_it_is_the_person.py -q
"""
from __future__ import annotations

import ast
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401  (fixture)
    PROMPT_ID, USER_ID, create_env)

SELF_LINE = 'You are a helpful assistant. Your primary role is to assist the user'


def _action(i, persona, text):
    return {'action_id': i, 'action': text, 'persona': persona,
            'can_perform_without_user_input': 'yes',
            'recipe': [{'steps': 'step %d' % i, 'tool_name': 'google_search'}]}


AGENTS = {
    # name: (personas, flows, flow-0 actions, picked persona or None)
    'executor_only': (
        ['Executor'],
        [{'flow_name': 'main', 'persona': 'Executor', 'sub_goal': 'done'}],
        [_action(1, 'Executor', 'search'), _action(2, 'Executor', 'summarise'),
         _action(3, 'Executor', 'send')],
        None),
    'fallback_plan': (
        ['Assistant'],
        [{'flow_name': 'main', 'persona': 'Assistant', 'sub_goal': 'Help the user'}],
        [_action(1, 'Assistant', 'Respond to user')],
        None),
    'people': (
        ['the child', 'Parent', 'Therapist'],
        [{'flow_name': 'Name calling', 'persona': 'the child', 'sub_goal': 'turns when called'},
         {'flow_name': 'Progress', 'persona': 'Parent', 'sub_goal': 'gets the summary'},
         {'flow_name': 'Plan', 'persona': 'Therapist', 'sub_goal': 'moves the child'}],
        [_action(1, 'the child', 'call the name'), _action(2, 'the child', 'wait'),
         _action(3, 'Parent', 'send progress')],
        'the child'),
}
# Actions each turn keeps: the filter's own result before this fix, pinned.
KEPT = {'executor_only': 3, 'fallback_plan': 1, 'people': 2, 'empty_role': 3}


@pytest.fixture
def reuse_turn(create_env, tmp_path, monkeypatch):
    env = create_env
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'data'))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.reel_studio import jobs
    monkeypatch.setattr(jobs, '_SWEPT', True)
    import hartos.reuse_recipe as rr
    monkeypatch.setattr(rr, 'PROMPTS_DIR', str(env.prompts))
    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    caches = ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
              'user_agents', 'time_actions', 'user_ledgers', 'agents_session')
    for name in caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend', lambda *a, **k: InMemoryBackend())

    def run(shape):
        personas, flows, actions, picked = AGENTS['executor_only' if shape == 'empty_role' else shape]
        (env.prompts / f'{PROMPT_ID}.json').write_text(json.dumps({
            'personas': [{'name': n, 'description': 'saved persona'} for n in personas],
            'goal': 'Help a child respond to their name', 'agent_name': 'speech.local.coach',
            'flows': [dict(f, actions=[a['action'] for a in actions]) for f in flows],
        }), encoding='utf-8')
        (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(
            json.dumps({'status': 'done', 'actions': actions}), encoding='utf-8')
        if picked:
            from core.flow_progress import set_persona
            assert set_persona(USER_ID, PROMPT_ID, picked, personas) == picked
        if shape == 'empty_role':
            # No real caller hands an empty role today (get_role defaults to
            # 'user', get_flow_number turns falsy into personas[0]); pin that
            # the self line does not depend on it either.
            monkeypatch.setattr(rr, 'get_flow_number', lambda u, p: (0, ''))
        with env.app.app_context():
            assistant = rr.create_agents_for_user(USER_ID, PROMPT_ID)[0]
        msg = str(assistant.system_message)
        kept = ast.literal_eval(msg.split('<actionsStart>', 1)[1].split('<actionEnd>', 1)[0])
        return msg, kept
    yield run
    for name in caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    platform_paths.reset_cache()


@pytest.mark.parametrize('shape', ['executor_only', 'fallback_plan', 'people', 'empty_role'])
def test_the_turn_says_who_the_agent_is_and_keeps_its_actions(reuse_turn, shape):
    msg, kept = reuse_turn(shape)
    assert SELF_LINE in msg, msg[:400]
    first = msg.split('You are a helpful', 1)[1].split('.', 1)[0]
    for name in ('Executor', 'Assistant', 'the child', 'Parent', 'Therapist', 'user'):
        assert name not in first, first
    assert len(kept) == KEPT[shape], [a.get('action') for a in kept]


def test_the_person_served_is_still_named_where_the_actions_are_filtered(reuse_turn):
    msg, kept = reuse_turn('people')
    assert 'Only execute actions where the persona is: the child.' in msg
    assert {a['persona'] for a in kept} == {'the child'}


def test_the_role_comes_from_the_one_source_create_uses():
    from core.agent_personality import AGENT_ROLE
    assert SELF_LINE.startswith(f'You are a helpful {AGENT_ROLE}.')


def test_an_unreadable_profile_keeps_the_agents_personality(reuse_turn, monkeypatch):
    """REUSE caught only ImportError around the person's resonance profile;
    any other error fell to the outer guard and the turn lost the agent's
    whole personality block.  CREATE logs it and keeps the personality
    (hartos-16, 11-09)."""
    from unittest import mock
    from core import agent_personality as ap
    from core import resonance_profile as rp
    saved = ap.AgentPersonality(agent_name='speech.local.coach', role=ap.AGENT_ROLE,
                                persona_name='Coach', primary_traits=[], tone='warm-casual',
                                greeting_style='Hi!', self_awareness_prompt='')
    monkeypatch.setattr(ap, 'load_personality', lambda *a, **k: saved)
    monkeypatch.setattr(rp, 'get_or_create_profile',
                        mock.Mock(side_effect=RuntimeError('profile unreadable')))
    msg, kept = reuse_turn('executor_only')
    assert 'You are Coach, an assistant who embodies these cultural traditions:' in msg, msg[:600]
    assert len(kept) == KEPT['executor_only']
