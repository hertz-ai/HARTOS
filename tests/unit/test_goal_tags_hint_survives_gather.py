"""The phone's creation hint outlives the gather turns and is stamped on save.

Plan WS-8a step 2 (critique finding 2).  The phone's Reel Studio entry sends
``goal_tags: ["reel_studio"]`` with a create turn, but the agent record is
written only when gathering ends -- turns later, by _save_and_enter_review --
and her instructions need not contain a single reel phrase.  So /chat holds
the hint per '{user_id}_{prompt_id}' (the gather-turn key) until the save, and
the save stamps ``goal_tags`` = the user-assignable tags of (hint plus what
the saved goal itself says).  A reel agent's record is then not synced to the
cloud prompt list (decision U4(c)).  Any other agent's record is written
exactly as before.

Driven through the REAL /chat (app.test_client) with the body Nunba's
hevolve_chat forwards for a phone turn.  Boundaries replaced: gather_info
(the gather LLM), the HTTP pool, TTS, memory and the chat mirror.
"""
import json
import os
import sys
import tempfile
import uuid

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from tests.conftest import refuse_all_network  # noqa: E402

PLAIN = '{"status": "pending", "question": "What should your agent do?"}'
REVIEW = '{"status": "pending", "review_details": "Here is the agent as I have it."}'


def _completed(goal, actions, extra=''):
    return json.dumps({
        'status': 'completed', 'name': 'Studio', 'agent_name': 'studio.helper.one',
        'goal': goal, 'broadcast_agent': 'no',
        'personas': [{'name': 'Artist', 'description': 'the owner'}],
        'flows': [{'flow_name': 'main', 'persona': 'Artist', 'actions': actions,
                   'sub_goal': goal}],
        'extra_information': extra,
    })


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def chat(hie, tmp_path, monkeypatch):
    """POST phone-shaped create turns to the real /chat.  ``gather`` is the
    list of gather_info answers, one per turn, in order."""
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    import core.cache_loaders as cache_loaders
    import core.recipe_sync as recipe_sync
    from core import user_file_store as ufs
    from integrations.social import chat_messages, rate_limiter
    refuse_all_network(monkeypatch)
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(rate_limiter._limiter, 'check', lambda *a, **k: True)
    monkeypatch.setattr(recipe_sync, 'pull_recipe', lambda *a, **k: False)
    monkeypatch.setattr(chat_messages, 'persist_and_publish_async', MagicMock())
    monkeypatch.setattr(ufs, '_STORE', ufs.UserFileStore(
        str(tmp_path / 'user_files'), disk_free=lambda _p: 1 << 40))
    for name in ('_persist_language', '_cleanup_stale_agents', '_record_lifecycle',
                 '_tts_synthesize_and_publish', 'publish_async'):
        monkeypatch.setattr(hie, name, lambda *a, **k: None)
    monkeypatch.setattr(hie, 'get_memory', lambda **k: None)
    monkeypatch.setattr(hie, '_get_or_create_graph', lambda *a, **k: None)
    monkeypatch.setattr(hie, 'pooled_get', MagicMock(side_effect=ConnectionError('no cloud')))
    posts = MagicMock(name='pooled_post')
    monkeypatch.setattr(hie, 'pooled_post', posts)

    answers = []

    def gather_info(user_id, prompt, prompt_id, autonomous=False):
        return answers.pop(0)
    monkeypatch.setitem(sys.modules, 'hartos.gather_agentdetails',
                        SimpleNamespace(gather_info=gather_info))

    pid = str(uuid.uuid4().int % 10 ** 11)
    user = 'phone-user-%s' % pid[-6:]
    client = hie.app.test_client()

    def turn(text, goal_tags=None):
        body = {'prompt': text, 'user_id': user, 'prompt_id': int(pid),
                'request_id': 'rid-%s' % uuid.uuid4().hex[:12],
                'casual_conv': False, 'probe': False, 'intermediate': False,
                'create_agent': True, 'file_id': 0, 'preferred_lang': 'en',
                'tools': None, 'video_req': False, 'media_request': False,
                'media_mode': 'text', 'autonomous': False,
                'agentic_execute': False, 'device_id': 'phone-1'}
        if goal_tags is not None:
            body['goal_tags'] = goal_tags
        r = client.post('/chat', json=body)
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()

    def saved():
        with open(prompts / f'{pid}.json', encoding='utf-8') as f:
            return json.load(f)

    def cloud_syncs():
        return [c for c in posts.call_args_list
                if 'createpromptlist' in str(c.args[0] if c.args else '')]

    yield SimpleNamespace(turn=turn, answers=answers, saved=saved, pid=pid,
                          user=user, cloud_syncs=cloud_syncs, hie=hie)
    key = f'{user}_{pid}'
    for table in (hie.review_agents, hie.conversation_agent, hie._gather_turn_counts,
                  hie._gather_reviews_shown):
        table.pop(key, None)
    if pid in hie.first_promts:
        hie.first_promts.remove(pid)


def test_a_hint_on_the_first_turn_is_stamped_when_gathering_ends(chat):
    """Her instructions carry no reel phrase; only the hint says it."""
    chat.answers += [PLAIN, REVIEW,
                     _completed('Help my salon with weekend bookings',
                                ['Edit the clips she sends', 'Write a caption'])]
    chat.turn('I want help with my salon', goal_tags=['reel_studio'])
    chat.turn('weekend bookings, captions')            # no hint on later turns
    done = chat.turn('yes, build it')
    assert done['Agent_status'] == 'Review Mode'
    assert chat.saved()['goal_tags'] == ['reel_studio'], (
        'the hint sent on gather turn 1 was gone by the turn that saved the '
        'record, so her agent was saved without its tag')
    assert chat.cloud_syncs() == [], (
        "a reel_studio record must not be posted to the cloud's createpromptlist")


def test_no_hint_but_the_saved_goal_names_it(chat):
    chat.answers += [REVIEW, _completed('Make bridal makeup reels from my clips',
                                        ['Cut the clips to the beat'])]
    chat.turn('make my reels')
    chat.turn('yes')
    assert chat.saved()['goal_tags'] == ['reel_studio']
    assert chat.cloud_syncs() == []


def test_a_hint_that_is_not_user_assignable_is_ignored(chat):
    chat.answers += [REVIEW, _completed('Plan my week', ['List my tasks'])]
    chat.turn('plan my week', goal_tags=['marketing', 'coding'])
    chat.turn('yes')
    assert 'goal_tags' not in chat.saved()


def test_any_other_agents_record_is_written_as_before(chat):
    """Byte-for-byte what the save wrote before this change: the gathered
    config plus prompt_id and creator_user_id, and the cloud sync posted."""
    answer = _completed('Plan my week and compose a jingle', ['List my tasks'],
                        extra='weekdays only')
    chat.answers += [REVIEW, answer]
    chat.turn('plan my week')
    chat.turn('yes')
    expected = dict(json.loads(answer), prompt_id=chat.pid, creator_user_id=chat.user)
    assert chat.saved() == expected
    assert len(chat.cloud_syncs()) == 1


def test_the_hint_is_dropped_once_the_record_is_saved(chat):
    from integrations.agent_engine.marketing_tools import held_goal_tag_hint
    chat.answers += [REVIEW, _completed('Help my salon', ['Edit the clips'])]
    chat.turn('help my salon', goal_tags=['reel_studio'])
    assert held_goal_tag_hint(f'{chat.user}_{chat.pid}') == ['reel_studio']
    chat.turn('yes')
    assert held_goal_tag_hint(f'{chat.user}_{chat.pid}') == []
