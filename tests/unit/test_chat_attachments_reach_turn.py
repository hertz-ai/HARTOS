"""A phone turn's attachments are bound to the turn before anything runs.

Plan WS-8a step 1.  The phone uploads clips into the user file store and the
chat turn carries only references (plan 4.2(e)).  /chat binds them to
(user, prompt, request) as soon as the turn's identity is known -- under the
user the caller's TOKEN names, never the body's -- and before the REUSE
ladder replaces the request body with the agent's own record.  The reel tool
then finds this turn's clips with chat_attachments.latest_inbound.

Driven through the REAL /chat (app.test_client) with the body Nunba's
hevolve_chat forwards for a phone turn.  Each case stops the turn where the
agent would start working (gather_info for a new agent, chat_agent on the
REUSE ladder) and reads what that agent would see.
"""
import hashlib
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

TOKEN_USER = 'phone-owner-77'
BODY_USER = 'someone-else-88'


class _Stop(Exception):
    """Raised where the agent would start working; the binding is done by then."""


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


def _upload(store, uid, data, purpose='reel_source', mime='video/mp4', name='VID.mp4'):
    xid = hashlib.sha256(data).hexdigest()
    ans = store.offer(uid, name, len(data), xid, mime, purpose, chunk_size=64 << 10)
    assert ans['type'] == 'file_accept', ans
    assert store.put_chunk(uid, xid, 0, data, xid)['type'] == 'file_ack'
    assert store.commit(uid, xid)['type'] == 'file_done'
    return xid


def _ref(xid, purpose='reel_source', mime='video/mp4', name='VID.mp4'):
    return {'kind': 'peerlink_file', 'ref': xid, 'purpose': purpose, 'name': name,
            'mime': mime, 'size': 10, 'captured_at': 1_760_000_000_000}


@pytest.fixture
def phone(hie, tmp_path, monkeypatch):
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    import core.cache_loaders as cache_loaders
    import core.recipe_sync as recipe_sync
    from core import user_file_store as ufs
    from integrations.social import chat_messages, rate_limiter
    from integrations.social.auth import generate_jwt
    refuse_all_network(monkeypatch)
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(rate_limiter._limiter, 'check', lambda *a, **k: True)
    monkeypatch.setattr(recipe_sync, 'pull_recipe', lambda *a, **k: False)
    monkeypatch.setattr(chat_messages, 'persist_and_publish_async', MagicMock())
    store = ufs.UserFileStore(str(tmp_path / 'user_files'), disk_free=lambda _p: 1 << 40)
    monkeypatch.setattr(ufs, '_STORE', store)
    for name in ('_persist_language', '_cleanup_stale_agents', '_record_lifecycle',
                 '_tts_synthesize_and_publish', 'publish_async'):
        monkeypatch.setattr(hie, name, lambda *a, **k: None)
    monkeypatch.setattr(hie, 'get_memory', lambda **k: None)
    monkeypatch.setattr(hie, '_get_or_create_graph', lambda *a, **k: None)
    monkeypatch.setattr(hie, 'pooled_get', MagicMock(side_effect=ConnectionError('no cloud')))
    monkeypatch.setattr(hie, 'pooled_post', MagicMock())

    pid = str(uuid.uuid4().int % 10 ** 11)
    seen = {}

    def working(where):
        """A stand-in for the agent starting work: reads this turn's clips."""
        from core.chat_attachments import latest_inbound
        from hartos.threadlocal import thread_local_data

        def _stand_in(*_a, **_k):
            rid = thread_local_data.get_request_id()
            seen['where'] = where
            seen['clips'] = [c['xid'] for c in latest_inbound(TOKEN_USER, pid, request_id=rid)]
            seen['body_user_clips'] = [c['xid'] for c in latest_inbound(BODY_USER, pid)]
            raise _Stop()
        return _stand_in

    monkeypatch.setitem(sys.modules, 'hartos.gather_agentdetails',
                        SimpleNamespace(gather_info=working('gather_info')))
    monkeypatch.setattr(hie, 'chat_agent', working('chat_agent'))
    from integrations.agent_engine import agent_daemon
    monkeypatch.setattr(agent_daemon, '_flow_recipe_exists', lambda _pid: True)

    token = generate_jwt(TOKEN_USER, 'Phone Owner')
    client = hie.app.test_client()

    def post(attachments, create_agent=True, **extra):
        body = {'prompt': 'make today\'s reel', 'user_id': BODY_USER,
                'prompt_id': int(pid), 'request_id': 'rid-%s' % uuid.uuid4().hex[:12],
                'casual_conv': False, 'probe': False, 'intermediate': False,
                'create_agent': create_agent, 'file_id': 0, 'preferred_lang': 'en',
                'tools': None, 'video_req': False, 'media_request': False,
                'media_mode': 'text', 'autonomous': False, 'agentic_execute': False,
                'device_id': 'phone-1', 'goal_tags': ['reel_studio']}
        if attachments is not None:
            body['attachments'] = attachments
        body.update(extra)
        seen.clear()
        try:
            r = client.post('/chat', json=body,
                            headers={'Authorization': 'Bearer ' + token})
        except _Stop:
            return body['request_id']
        raise AssertionError('the turn never reached the agent: %s %s'
                             % (r.status_code, r.get_data(as_text=True)[:300]))

    def make_reusable():
        record = {'name': 'Reel Studio', 'goal': 'Edit my bridal makeup reels',
                  'goal_tags': ['reel_studio'], 'prompt_id': pid,
                  'creator_user_id': TOKEN_USER,
                  'flows': [{'flow_name': 'main', 'persona': 'Artist',
                             'actions': ['Make a reel'], 'sub_goal': 'reels'}]}
        (prompts / f'{pid}.json').write_text(json.dumps(record))
        (prompts / f'{pid}_0_recipe.json').write_text(json.dumps(
            {'status': 'completed', 'actions': []}))

    yield SimpleNamespace(post=post, seen=seen, store=store, pid=pid,
                          make_reusable=make_reusable)
    key = f'{TOKEN_USER}_{pid}'
    for table in (hie.review_agents, hie.conversation_agent, hie._gather_turn_counts):
        table.pop(key, None)
    if pid in hie.first_promts:
        hie.first_promts.remove(pid)


def test_a_create_turns_clips_are_bound_under_the_tokens_user(phone):
    a = _upload(phone.store, TOKEN_USER, b'clip-a' * 80)
    b = _upload(phone.store, TOKEN_USER, b'clip-b' * 80)
    theirs = _upload(phone.store, BODY_USER, b'theirs' * 80)
    phone.post([_ref(a), _ref(b), _ref(theirs), _ref('e' * 64)])
    assert phone.seen['where'] == 'gather_info'
    assert phone.seen['clips'] == [a, b], (
        "this turn's clips were not bound before the agent started")
    assert phone.seen['body_user_clips'] == [], (
        "the body's user_id named another person; nothing may be bound to them")
    assert phone.store.resolve(BODY_USER, theirs).request_id is None


def test_on_the_reuse_ladder_the_clips_are_bound_before_the_body_is_replaced(phone):
    phone.make_reusable()
    a = _upload(phone.store, TOKEN_USER, b'reuse-a' * 80)
    rid = phone.post([_ref(a)], create_agent=False)
    assert phone.seen['where'] == 'chat_agent', 'this turn did not take the REUSE ladder'
    assert phone.seen['clips'] == [a]
    [rec] = phone.store.query_inbox(TOKEN_USER, request_id=rid)
    assert rec.prompt_id == phone.pid


def test_a_logo_and_a_song_bind_with_the_clips(phone):
    clip = _upload(phone.store, TOKEN_USER, b'clip' * 80)
    logo = _upload(phone.store, TOKEN_USER, b'\x89PNG' * 80, purpose='logo',
                   mime='image/png', name='logo.png')
    rid = phone.post([_ref(clip), _ref(logo, 'logo', 'image/png', 'logo.png')])
    assert {r.xid for r in phone.store.query_inbox(TOKEN_USER, request_id=rid)} == {clip, logo}


def test_a_turn_without_attachments_binds_nothing(phone, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError('a turn with no attachments asked the store to bind')
    monkeypatch.setattr(phone.store, 'bind', boom)
    phone.post(None)
    assert phone.seen['clips'] == []


@pytest.mark.parametrize('junk', ['a' * 64, {'ref': 'a' * 64}, [None, 'x'], 42])
def test_malformed_attachments_never_break_the_turn(phone, junk):
    phone.post(junk)
    assert phone.seen['where'] == 'gather_info'
    assert phone.seen['clips'] == []
