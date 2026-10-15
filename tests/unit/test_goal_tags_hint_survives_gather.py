"""The phone's creation hint survives the gather: the turn that ends it carries it.

Plan WS-8a step 2 (critique finding 2), amendment A19.  The agent record is
written only when gathering ends -- turns later, by _save_and_enter_review --
and her instructions need not contain a single reel phrase.  The phone sends
her agent's ``goal_tags: ["reel_studio"]`` on EVERY turn of it
(ReelInbox.markTurn), so the save stamps ``goal_tags`` = the user-assignable
tags of the hint the saving turn itself carries -- ONLY the explicit hint: the
saved goal's own words never stamp it (amendment A8 c).  Nothing holds a hint
between turns.  The in-memory hold this replaced
(marketing_tools._creation_hints, 2 h) lost it when this node restarted
mid-gather or a gather ran longer, and her agent was then saved without
reel_studio for good (A19); and no gather state this node keeps outlives a
restart either (review_agents and the turn counts are in memory and expire
after an idle hour).  So a gather across a restart, or over hours, still
stamps it, and a hint that came only on an earlier turn stamps nothing.  A
reel agent's record is not synced to the cloud prompt list (decision U4(c)).
Any other agent's record is written exactly as before, and on the CENTRAL
node the hint stamps nothing (amendment A8 d).

Driven through the REAL /chat (app.test_client) with the body Nunba's
hevolve_chat forwards for a phone turn.  Boundaries replaced: gather_info
(the gather LLM), the HTTP pool, TTS, memory and the chat mirror.
"""
import json
import os
import sys
import tempfile
import time
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
    from integrations.agent_engine import dispatch
    from integrations.social import chat_messages, rate_limiter
    refuse_all_network(monkeypatch)
    # A genuine request id marks the person active for the daemon's yield
    # gate; leave that gate as found (test_flagship_agents' tutor defers
    # behind it otherwise).
    monkeypatch.setattr(dispatch, '_last_user_chat_at', dispatch._last_user_chat_at)
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
    real_cleanup = hie._cleanup_stale_agents
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

    def saved_bytes():
        return (prompts / f'{pid}.json').read_bytes()

    def cloud_syncs():
        return [c for c in posts.call_args_list
                if 'createpromptlist' in str(c.args[0] if c.args else '')]

    key = f'{user}_{pid}'

    def forget():
        """Every per-agent table chat() keeps in memory loses this agent."""
        for table in (hie.review_agents, hie.conversation_agent, hie._gather_turn_counts,
                      hie._gather_reviews_shown, hie._agent_timestamps):
            table.pop(key, None)
        if pid in hie.first_promts:
            hie.first_promts.remove(pid)

    def idle(hours):
        """``hours`` pass with no turn, and the node's own stale-state sweep
        (_cleanup_stale_agents, run at the start of each /chat) runs."""
        hie._agent_timestamps[key] = time.time() - hours * 3600
        real_cleanup()

    yield SimpleNamespace(turn=turn, answers=answers, saved=saved, pid=pid,
                          user=user, cloud_syncs=cloud_syncs, hie=hie,
                          saved_bytes=saved_bytes, restart=forget, idle=idle)
    forget()


HER_TAGS = ['reel_studio']        # what the phone puts on every turn of her agent


def test_her_hint_is_stamped_when_gathering_ends(chat):
    """Her instructions carry no reel phrase; only the hint says it."""
    chat.answers += [PLAIN, REVIEW,
                     _completed('Help my salon with weekend bookings',
                                ['Edit the clips she sends', 'Write a caption'])]
    chat.turn('I want help with my salon', goal_tags=HER_TAGS)
    chat.turn('weekend bookings, captions', goal_tags=HER_TAGS)
    done = chat.turn('yes, build it', goal_tags=HER_TAGS)
    assert done['Agent_status'] == 'Review Mode'
    assert chat.saved()['goal_tags'] == ['reel_studio']
    assert chat.cloud_syncs() == [], (
        "a reel_studio record must not be posted to the cloud's createpromptlist")


def test_a_restart_mid_gather_still_stamps_her_tag(chat):
    """Amendment A19: this node restarts between her turns, so everything it
    kept in memory for the gather is gone.  The turn that ends the gather
    carries her hint, and her agent is saved a Reel Studio one."""
    chat.answers += [PLAIN, REVIEW, _completed('Help my salon', ['Edit the clips'])]
    chat.turn('I want help with my salon', goal_tags=HER_TAGS)
    chat.restart()
    chat.turn('weekend bookings, captions', goal_tags=HER_TAGS)
    done = chat.turn('yes, build it', goal_tags=HER_TAGS)
    assert done['Agent_status'] == 'Review Mode'
    assert chat.saved()['goal_tags'] == ['reel_studio'], (
        'a restart mid-gather saved her agent without reel_studio: it can '
        'never get the reel tools after that')
    assert chat.cloud_syncs() == []


def test_a_gather_over_two_hours_still_stamps_her_tag(chat):
    """Amendment A19: she answers the next question three hours later.  The
    node's own sweep has forgotten the idle gather by then (an hour), and
    the old hint hold had expired (two hours); her turn carries the hint."""
    chat.answers += [PLAIN, REVIEW, _completed('Help my salon', ['Edit the clips'])]
    chat.turn('I want help with my salon', goal_tags=HER_TAGS)
    chat.idle(hours=3)
    assert f'{chat.user}_{chat.pid}' not in chat.hie._gather_turn_counts, (
        'precondition: the sweep forgot the idle gather')
    chat.turn('weekend bookings, captions', goal_tags=HER_TAGS)
    chat.turn('yes, build it', goal_tags=HER_TAGS)
    assert chat.saved()['goal_tags'] == ['reel_studio']
    assert chat.cloud_syncs() == []


def test_a_hint_on_an_earlier_turn_alone_stamps_nothing(chat):
    """Nothing holds a hint between turns (A19 / A20: the 2 h in-memory hold
    is gone): only the saving turn's own hint is stamped.  A client that
    sent it on its first turn alone gets an ordinary record, written and
    synced as any other -- the same record a restart would have left."""
    chat.answers += [PLAIN, REVIEW, _completed('Help my salon', ['Edit the clips'])]
    chat.turn('I want help with my salon', goal_tags=HER_TAGS)
    chat.turn('weekend bookings, captions')
    chat.turn('yes, build it')
    assert 'goal_tags' not in chat.saved(), (
        'a hint from an earlier turn was held in memory and stamped: a restart '
        'or a long gather would have lost it, so the record depended on luck')
    assert len(chat.cloud_syncs()) == 1


def test_no_hint_no_stamp_even_when_the_saved_goal_names_reels(chat):
    """Her words alone never stamp the tag: detection grants the reel tools
    per turn, but only the explicit hint keeps an agent on this node."""
    chat.answers += [REVIEW, _completed('Reel studio: edit my reels and edit my clips',
                                        ['Cut the clips to the beat'])]
    chat.turn('make my reels')
    chat.turn('yes')
    assert 'goal_tags' not in chat.saved()
    assert len(chat.cloud_syncs()) == 1


def test_a_reel_tag_the_gather_llm_wrote_itself_is_dropped(chat):
    """The gathered JSON is the record: a reel_studio in it without her hint
    is not trusted (amendment A8 c); its other tags are kept."""
    answer = dict(json.loads(_completed('Help my salon', ['Edit the clips'])),
                  goal_tags=['reel_studio', 'news'])
    chat.answers += [REVIEW, json.dumps(answer)]
    chat.turn('help my salon')
    chat.turn('yes')
    assert chat.saved()['goal_tags'] == ['news']
    assert len(chat.cloud_syncs()) == 1


def test_a_commerce_lane_goal_is_saved_and_synced_as_before(chat):
    answer = _completed('Find bridal makeup artists and pitch our product',
                        ['Post Instagram reels to makeup artists'])
    chat.answers += [REVIEW, answer]
    chat.turn('find makeup artists and pitch our product')
    chat.turn('yes')
    assert chat.saved() == dict(json.loads(answer), prompt_id=chat.pid,
                                creator_user_id=chat.user)
    assert len(chat.cloud_syncs()) == 1


def test_on_central_the_hint_stamps_nothing(chat, monkeypatch):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    chat.answers += [REVIEW, _completed('Help my salon', ['Edit the clips'])]
    chat.turn('help my salon', goal_tags=HER_TAGS)
    chat.turn('yes', goal_tags=HER_TAGS)
    assert 'goal_tags' not in chat.saved()
    assert len(chat.cloud_syncs()) == 1, (
        'central skipped the cloud prompt-list sync for a reel hint')


def test_a_hint_that_is_not_user_assignable_is_ignored(chat):
    chat.answers += [REVIEW, _completed('Plan my week', ['List my tasks'])]
    chat.turn('plan my week', goal_tags=['marketing', 'coding'])
    chat.turn('yes', goal_tags=['marketing', 'coding'])
    assert 'goal_tags' not in chat.saved()


def test_any_other_agents_record_is_written_as_before(chat):
    """Byte-for-byte what the save wrote before this change: the gathered
    config plus prompt_id and creator_user_id, in that key order, as
    json.dump writes it with its defaults (no goal_tags key, not even a null
    one), and the cloud sync posted."""
    answer = _completed('Plan my week and compose a jingle', ['List my tasks'],
                        extra='weekdays only')
    chat.answers += [REVIEW, answer]
    chat.turn('plan my week')
    chat.turn('yes')
    expected = dict(json.loads(answer), prompt_id=chat.pid, creator_user_id=chat.user)
    assert chat.saved_bytes() == json.dumps(expected).encode('ascii')
    assert len(chat.cloud_syncs()) == 1
