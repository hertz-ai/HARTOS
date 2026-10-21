"""A reel this turn made rides its /chat reply, and the mirror's assistant row.

Plan WS-8a step 4 (corrections C9, finding 44).  _chat_reply is the one exit
of /chat.  Before this change the assistant's mirror row was saved with
``attachments=None`` hard-coded and no reply carried media, so a reel that
finished inside the phone's wait never reached it in the reply.  Now the reply
takes this turn's outbound reel from the user file store (once: the store
marks it replied) into ``media``, and the assistant row gets the documented
mirror shape ``[{file_id, sha256, name, mime, size}]``.  A reply with no reel
is unchanged: no ``media`` key and no assistant attachments.

Drives the REAL _chat_reply inside a request carrying the turn's body, with
the real store on a temp root; TTS, memory and the mirror writer are the
mocked boundaries.
"""
import os
import tempfile

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

UID = 'reply-user-1'


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def reply(hie, tmp_path, monkeypatch):
    from core import user_file_store as ufs
    from integrations.social import chat_messages
    store = ufs.UserFileStore(str(tmp_path / 'user_files'), disk_free=lambda _p: 1 << 40)
    monkeypatch.setattr(ufs, '_STORE', store)
    mirror = MagicMock(name='persist_and_publish_async')
    monkeypatch.setattr(chat_messages, 'persist_and_publish_async', mirror)
    monkeypatch.setattr(hie, '_tts_synthesize_and_publish', lambda *a, **k: None)
    monkeypatch.setattr(hie, 'get_memory', lambda **k: None)
    monkeypatch.setattr(hie, '_get_or_create_graph', lambda *a, **k: None)

    def _reply(body, text='Your reel is ready', **payload):
        mirror.reset_mock()
        with hie.app.test_request_context('/chat', json=body):
            resp = hie._chat_reply(UID, body.get('request_id'), text,
                                   user_prompt='make my reel', **payload)
        rows = {c.args[1]: c.kwargs for c in mirror.call_args_list}
        return resp.get_json(), rows

    reel = tmp_path / 'reel.mp4'
    reel.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'frames' * 500)
    _reply.store = store
    _reply.reel = str(reel)
    return _reply


def _media(reel_id='r-1'):
    return {'kind': 'video', 'xid': '', 'sha256': '', 'mime': 'video/mp4', 'size': 0,
            'width': 1080, 'height': 1920, 'duration_s': 20.0, 'has_audio': False,
            'reel_id': reel_id, 'prompt_id': '', 'request_id': None,
            'caption': 'Bridal look', 'hashtags': ['#bridalmakeup'], 'song': None,
            'notes': [], 'qa': {'passed': True, 'failed': []}}


def _offer(reply, prompt_id='42', request_id='rid-1', reel_id='r-1'):
    return reply.store.offer_to_device(UID, reply.reel, mime='video/mp4', purpose='reel',
                                       prompt_id=prompt_id, request_id=request_id,
                                       meta={'media': _media(reel_id)})


def test_the_turns_reel_rides_the_reply_and_the_assistant_row(reply):
    sf = _offer(reply)
    body, rows = reply({'prompt': 'make my reel', 'prompt_id': 42, 'request_id': 'rid-1'})
    assert 'media' in body, 'the reel this turn made is missing from its reply'
    [item] = body['media']
    assert item['xid'] == item['sha256'] == sf.xid
    assert item['size'] == sf.size and item['request_id'] == 'rid-1'
    assert item['caption'] == 'Bridal look'
    assert rows['assistant']['attachments'] == [{
        'file_id': sf.xid, 'sha256': sf.xid, 'name': 'reel_r-1.mp4',
        'mime': 'video/mp4', 'size': sf.size}], (
        'the assistant mirror row still carries attachments=None')
    assert rows['user']['attachments'] is None, 'the user row is unchanged'


def test_a_reel_rides_one_reply_only(reply):
    _offer(reply)
    first, _ = reply({'prompt': 'x', 'prompt_id': 42, 'request_id': 'rid-1'})
    again, rows = reply({'prompt': 'x', 'prompt_id': 42, 'request_id': 'rid-1'})
    assert len(first['media']) == 1
    assert 'media' not in again
    assert rows['assistant']['attachments'] is None


def test_another_turns_reel_is_not_in_this_reply(reply):
    _offer(reply, request_id='rid-older')
    body, rows = reply({'prompt': 'x', 'prompt_id': 42, 'request_id': 'rid-new'})
    assert 'media' not in body
    assert rows['assistant']['attachments'] is None


def test_the_prompt_id_may_come_from_the_reply_payload(reply):
    sf = _offer(reply, prompt_id='777')
    body, _ = reply({'prompt': 'x', 'request_id': 'rid-1'}, prompt_id='777')
    assert [m['xid'] for m in body['media']] == [sf.xid]


def test_a_reply_without_a_reel_is_unchanged(reply):
    body, rows = reply({'prompt': 'hello', 'prompt_id': 42, 'request_id': 'rid-1'},
                       text='hi there')
    assert 'media' not in body
    assert body['response'] == 'hi there'
    assert rows['assistant']['attachments'] is None


def test_a_caller_given_media_is_kept(reply):
    xid = 'ef' * 32
    given = [dict(_media('given'), xid=xid, sha256=xid, size=9)]
    body, rows = reply({'prompt': 'x', 'prompt_id': 42, 'request_id': 'rid-1'},
                       media=given)
    assert body['media'] == given
    assert rows['assistant']['attachments'][0]['file_id'] == xid


def test_a_broken_store_never_costs_the_reply(reply, monkeypatch):
    def broken(*_a, **_k):
        raise OSError('disk gone')
    monkeypatch.setattr(reply.store, 'take_replied', broken)
    body, rows = reply({'prompt': 'x', 'prompt_id': 42, 'request_id': 'rid-1'})
    assert body['response'] == 'Your reel is ready' and 'media' not in body
    assert rows['assistant']['attachments'] is None
