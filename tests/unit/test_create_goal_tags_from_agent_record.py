"""CREATE builds an agent with the goal tags its saved record carries.

Plan C7 / WS-8a step 5.  create_agents resolved its goal tags as
``resolve_goal_tags(None, task)``, where ``task`` is the session's
``helper.Action``: it has no __str__, so the text scanned is a repr and
nothing is detected, and the record's stored ``goal_tags`` were never read.
A goal-gated Tier-2 family therefore never reached an agent while its recipe
was being built -- e.g. the news family for a news agent, and (once the
record is stamped) the reel family for her Reel Studio agent.

The narrowed fix: CREATE also unions the STORED tags from the record it
already loads.  The keyword leg is unchanged (plan D6 withdrawn), so a record
without stored tags resolves exactly as before.

Driven through the REAL create_agents with autogen's constructors replaced by
recording stand-ins -- the seam tests/unit/test_create_gate_stops_the_chat.py
uses -- so the tools asserted are the ones the real Tier-2 block registered.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from hartos import create_recipe as cr  # noqa: E402
from hartos.helper import Action  # noqa: E402
from hartos.lifecycle_hooks import clear_action_states  # noqa: E402
from integrations.agent_engine import marketing_tools  # noqa: E402

PID = 98765


def _named(*_args, **kwargs):
    agent = MagicMock()
    agent.name = kwargs.get('name', 'agent')
    return agent


def _llm_names(agent):
    return {c.kwargs.get('name') for c in agent.register_for_llm.call_args_list}


@pytest.fixture
def build(temp_prompts_dir, test_user_id, mock_flask_app):
    """Write ``record`` as the agent's prompts/{PID}.json and build its agents."""
    up = f'{test_user_id}_{PID}'

    def _build(record):
        (temp_prompts_dir / f'{PID}.json').write_text(json.dumps(record))
        clear_action_states(up)
        task = Action(record['flows'][0]['actions'])
        seen = []
        real_resolve = marketing_tools.resolve_goal_tags

        def spy(stored, text):
            seen.append((stored, text))
            return real_resolve(stored, text)

        patches = [
            patch.object(cr, 'user_tasks', {up: task}),
            patch('hartos.create_recipe.config_list',
                  [{'model': 'test', 'api_key': 'test'}]),
            patch.object(marketing_tools, 'resolve_goal_tags', spy),
        ]
        for name in ('AssistantAgent', 'UserProxyAgent', 'GroupChat',
                     'GroupChatManager'):
            patches.append(patch(f'hartos.create_recipe.autogen.{name}',
                                 side_effect=_named))
        for p in patches:
            p.start()
        try:
            out = cr.create_agents(test_user_id, task, PID)
        finally:
            for p in reversed(patches):
                p.stop()
            clear_action_states(up)
        agents = out[6]
        return SimpleNamespace(task=task, resolved=seen,
                               helper=agents['helper'],
                               assistant=agents['assistant'])
    return _build


def _record(goal_tags=None, goal='Keep me posted on the world', actions=None):
    record = {
        'name': 'Herald', 'goal': goal,
        'personas': [{'name': 'Reader', 'description': 'reads'}],
        'flows': [{'flow_name': 'main', 'persona': 'Reader',
                   'actions': actions or ['Read the morning headlines'],
                   'sub_goal': 'headlines'}],
    }
    if goal_tags is not None:
        record['goal_tags'] = goal_tags
    return record


def test_stored_news_tag_brings_the_news_family_into_create(build):
    built = build(_record(goal_tags=['news']))
    assert 'fetch_news_feeds' in _llm_names(built.helper), (
        "the record says goal_tags=['news'] and CREATE still built the agent "
        "without the news family: create_agents ignored the stored tags")
    assert 'news' in built.assistant._hart_unlocked_tags


def test_a_record_without_stored_tags_builds_exactly_as_before(build):
    built = build(_record())
    # The constructor's own resolution is the one over the session's task.
    # The build may ask resolve_goal_tags other questions -- the reel family
    # asks whether reel_studio is inert on this node (amendment A8 d,
    # integrations.reel_studio.tools) -- but this agent's tags are resolved
    # once, from the same inputs as before the fix.
    own = [(stored, text) for stored, text in built.resolved
           if text is built.task]
    assert len(own) == 1
    stored, text = own[0]
    assert stored is None and text is built.task, (
        'a legacy record must resolve with the same inputs as before the fix')
    assert 'fetch_news_feeds' not in _llm_names(built.helper)


def test_her_reel_studio_record_unlocks_only_reel_studio(build):
    built = build(_record(goal_tags=['reel_studio'],
                          goal='Edit my bridal makeup reels with Tamil songs',
                          actions=['Make a reel from the clips she sends']))
    assert built.assistant._hart_unlocked_tags == {'reel_studio'}


def test_on_central_a_stored_reel_studio_unlocks_nothing(build, monkeypatch):
    """Amendment A8 d: central's own goals keep their families and get no
    reel tools, whatever a record carries."""
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    built = build(_record(goal_tags=['reel_studio', 'news'],
                          goal='Edit my reels with Tamil songs'))
    assert built.assistant._hart_unlocked_tags == {'news'}
    assert 'fetch_news_feeds' in _llm_names(built.helper)


def test_a_junk_stored_value_is_not_read_as_tags(build):
    """The record is user-writable JSON: a bare string is not a tag list
    (iterating it would unlock one tag per character)."""
    built = build(_record(goal_tags='news'))
    assert built.assistant._hart_unlocked_tags == set()
    assert 'fetch_news_feeds' not in _llm_names(built.helper)


# --- CREATE's end: a local-only agent's bundle stays on this node -----------
# update_agent_creation_to_db marks the cloud row created, then pushes the
# whole prompts/{pid}* bundle -- goal, goal_tags, flows, extra_information,
# recipes -- to central's /prompts/sync (core.recipe_sync.push_recipe).
# Decision U4(c): her Reel Studio agent's goal is not synced to the cloud, so
# its bundle is not pushed either.  Every other agent's still is, and on the
# central node the tag is inert (amendment A8 d).

@pytest.fixture
def finish(temp_prompts_dir, mock_flask_app, monkeypatch):
    """Write the record, then run CREATE's completion step on it."""
    import core.recipe_sync as recipe_sync
    pushes, marks = [], []
    monkeypatch.setattr(recipe_sync, 'push_recipe',
                        lambda prompts_dir, pid, user_id='', **_k:
                        pushes.append((prompts_dir, str(pid))) or True)
    monkeypatch.setattr(cr, 'pooled_patch', lambda url, **_k: marks.append(url))

    def _finish(record):
        (temp_prompts_dir / f'{PID}.json').write_text(json.dumps(record))
        del pushes[:], marks[:]
        cr.update_agent_creation_to_db(PID)
        return SimpleNamespace(pushes=list(pushes), marks=list(marks))
    return _finish


def test_her_reel_agents_bundle_is_not_pushed_when_create_completes(finish):
    done = finish(_record(goal_tags=['reel_studio']))
    assert done.pushes == [], (
        "a reel_studio agent's goal, flows and recipes were pushed to the cloud")
    assert len(done.marks) == 1, "the cloud row's created flag is still set, as before"


def test_any_other_agents_bundle_is_pushed_as_before(finish, temp_prompts_dir):
    for tags in (None, ['news'], 'reel_studio'):
        done = finish(_record(goal_tags=tags))
        assert done.pushes == [(str(temp_prompts_dir), str(PID))], tags
        assert len(done.marks) == 1


def test_on_central_a_reel_record_is_pushed_as_before(finish, monkeypatch):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    assert len(finish(_record(goal_tags=['reel_studio'])).pushes) == 1
