"""An open-mic final's judge is a call of the person's own turn, made through
the REAL /chat route (app.test_client).

Review of 4e58dddcb (hartos-17, rv_mic5): the open-mic hook ran before /chat
set the turn's request id (hart_intelligence_entry.py:11201 vs :11508), so
the judge's model call went to the llama scheduler as daemon work
(rid='', kind='daemon'): up to 120 s waiting for a slot before its 15 s
post, and pre-empted by any user turn, after which the judge says nothing
and the words are answered.  Whatever id the thread held then (none, as a
request's teardown leaves it, or one some other work left on it) was the
judge's.  The /chat tests of that commit only searched the source.

The model's HTTP answer, the llama scheduler and the chat-history writer are
stood in for; everything between the request and them is the real code
(ask_model_json -> pooled_post -> the scheduler's slot).
"""
import contextlib
import json
import os
import tempfile
import uuid

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from tests.conftest import refuse_all_network  # noqa: E402

PERSON = 'u-open-mic-7'
MAIN_PORT = '59917'
MAIN_URL = f'http://127.0.0.1:{MAIN_PORT}/v1/chat/completions'


class _Stop(Exception):
    """Raised where an addressed turn goes on as the person's turn."""


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def room(hie, tmp_path, monkeypatch):
    import core.cache_loaders as cache_loaders
    import core.platform_paths as platform_paths
    from integrations.agent_engine import dispatch
    from integrations.audio import addressee
    from integrations.social import chat_messages, rate_limiter

    refuse_all_network(monkeypatch)
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(platform_paths, 'get_prompts_dir', lambda: str(prompts))
    monkeypatch.setattr(rate_limiter._limiter, 'check', lambda *a, **k: True)
    monkeypatch.setattr(hie, '_persist_language', lambda *a, **k: None)
    written = []
    monkeypatch.setattr(chat_messages, 'persist',
                        lambda *a, **k: written.append((a, k)) or {'id': len(written)})
    monkeypatch.setattr(chat_messages, 'persist_and_publish_async', MagicMock())
    active = MagicMock()
    monkeypatch.setattr(dispatch, 'mark_user_chat_activity', active)
    # The judge's readers other than the model: no open ask, no earlier
    # turns, no voice window.
    monkeypatch.setattr(addressee, '_open_ask_for', lambda *a, **k: None)
    monkeypatch.setattr(addressee, '_recent_turns', lambda *a, **k: [])
    monkeypatch.setattr(addressee, '_voice_hint', lambda *a, **k: '')

    seen = {}

    class _Scheduler:
        def slot(self, rid, kind, cancel_fn=None, timeout=None):
            seen['rid'], seen['kind'] = rid, kind
            return contextlib.nullcontext()

    class _Answer:
        status_code = 200

        def json(self):
            verdict, reason = seen['verdict']
            return {'choices': [{'message': {'content': json.dumps(
                {'for_the_agent': verdict, 'reason': reason})}}]}

    class _Session:
        def post(self, url, timeout=None, **kw):
            seen['question'] = kw['json']['messages'][0]['content']
            seen['wire_user'] = kw['json'].get('user')
            return _Answer()

    monkeypatch.setattr('core.autogen_config.llm_http_target',
                        lambda draft=False: (MAIN_URL, {}, 'main'))
    monkeypatch.setattr('core.autogen_config.llm_stays_on_premises', lambda: True)
    monkeypatch.setattr('core.http_pool._main_llama_port', lambda: MAIN_PORT)
    monkeypatch.setattr('core.llama_scheduler.get_scheduler', lambda: _Scheduler())
    monkeypatch.setattr('core.http_pool._llama_session_for', lambda _kind: _Session())
    monkeypatch.setattr('core.llm_outbound_logger.log_outbound', lambda *a, **k: None)

    pid = str(uuid.uuid4().int % 10 ** 11)
    (prompts / f'{pid}.json').write_text(json.dumps(
        {'name': 'Arjun', 'prompt_id': pid, 'goal': 'speech drills for a child'}))
    client = hie.app.test_client()

    def post(words, verdict, request_id):
        seen.clear()
        seen['verdict'] = verdict
        r = client.post('/chat', json={
            'user_id': PERSON, 'prompt': words, 'prompt_id': pid,
            'request_id': request_id, 'open_mic': True, 'casual_conv': False,
            'preferred_lang': 'en', 'device_id': 'desk-1'})
        return r.status_code, r.get_json()

    return SimpleNamespace(post=post, seen=seen, written=written, active=active,
                           pid=pid)


@pytest.mark.parametrize('left_on_the_thread', [None, 'daemon_goal-left-by-an-earlier-request'])
def test_the_judge_is_admitted_as_the_persons_turn_not_as_daemon_work(room, left_on_the_thread):
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id(left_on_the_thread)
    status, body = room.post('so the session ends at four', (False, 'two adults talking'),
                             'rq-open-mic-1')
    assert status == 200 and body['addressed'] is False
    assert room.seen.get('rid') == 'rq-open-mic-1', (
        f"the judge's model call carried request id {room.seen.get('rid')!r}, "
        f"not the turn's")
    assert room.seen['kind'] == 'user', 'the judge queued as daemon work'
    assert room.seen['wire_user'] == 'rq-open-mic-1'


def test_overheard_words_are_kept_as_heard_with_no_reply_and_no_activity(room):
    """Not for the agent: kept on record as heard (never the person's turn),
    the page gets the agent's feedback with nothing to say or speak, and the
    person is not marked active (room talk must not make background work
    yield)."""
    words = 'my PIN is 4471, do not tell the kid'
    status, body = room.post(words, (False, 'said to another adult'), 'rq-open-mic-2')
    assert status == 200
    assert body == {'text': '', 'response': '', 'addressed': False,
                    'not_addressed': True, 'reason': 'said to another adult',
                    'request_id': 'rq-open-mic-2'}
    assert room.written == [((PERSON, 'heard', words),
                             {'prompt_id': room.pid, 'request_id': 'rq-open-mic-2',
                              'channel_type': 'open_mic_heard'})], room.written
    room.active.assert_not_called()


def test_the_judge_is_told_the_turns_agents_name(room):
    room.active.side_effect = _Stop()   # the turn stops right after the judge
    with pytest.raises(_Stop):
        room.post('Arjun, I said ball!', (True, 'called by its name'), 'rq-open-mic-3')
    assert 'Your name is Arjun' in room.seen['question'], room.seen['question'][:400]


def test_an_addressed_open_mic_turn_goes_on_as_the_persons_turn(room):
    """For the agent: the turn goes on past the judge as the person's own
    turn (it marks the person active, the first step after the judge), with
    no heard row written."""
    room.active.side_effect = _Stop()
    with pytest.raises(_Stop):
        room.post('baw', (True, 'answers the drill'), 'rq-open-mic-4')
    assert room.seen['kind'] == 'user'
    assert room.written == []
