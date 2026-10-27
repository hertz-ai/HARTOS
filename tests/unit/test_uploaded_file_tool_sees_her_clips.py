"""get_user_uploaded_file answers with the files she sent this agent.

Measured live 2026-10-20 on her desktop (gui_app.log 19:34-19:37, request
8610dffc): her Reel Studio agent (342), working its first step "Verify the 5
clips are accessible in this chat session", called get_user_uploaded_file
three times, was told "No file uploaded from user" each time, and asked her
where the five clips were stored.  The user file store held all five for
agent 342 the whole time; the tool read only the turn's own file_id.

It now also answers with the batch a reel turn of this agent would use, by
the one rule reel_make reads (core.chat_attachments.latest_inbound).  These
tests drive the real store on a temporary root, installed as the process
singleton the tool asks.

    python -m pytest tests/unit/test_uploaded_file_tool_sees_her_clips.py -q
"""
import hashlib
import json

import pytest

from core import user_file_store as ufs
from core.chat_attachments import bind_inbound, parse_inbound
from core.session_cache import TTLCache
from tests.unit.test_core_tools_uuid_user_id import _ctx, _tool

USER = '10202'
#: The agent the factory's context names (_ctx: prompt_id 'p1').
AGENT = 'p1'


class _Clock:
    def __init__(self):
        self.now = 1_760_800_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path, monkeypatch):
    clock = _Clock()
    s = ufs.UserFileStore(str(tmp_path / 'user_files'), disk_free=lambda _p: 1 << 40,
                          clock=clock)
    monkeypatch.setattr(ufs, '_STORE', s)
    s.test_clock = clock
    return s


def _upload(store, data, name, captured_at=None):
    """Her phone's offer, chunk and commit of one clip; returns its xid."""
    xid = hashlib.sha256(data).hexdigest()
    meta = {'captured_at': captured_at} if captured_at else None
    ans = store.offer(USER, name, len(data), xid, 'video/mp4', 'reel_source', meta=meta)
    assert ans['type'] == 'file_accept', ans
    assert store.put_chunk(USER, xid, 0, data, xid)['type'] == 'file_ack'
    assert store.commit(USER, xid)['type'] == 'file_done'
    return xid


def _row(xid, name='VID.mp4'):
    return {'file_id': xid, 'sha256': xid, 'name': name, 'mime': 'video/mp4', 'size': 10,
            'purpose': 'reel_source'}


def _her_five_clips(store, agent=AGENT):
    """Five clips sent with one turn of ``agent`` (10-18 21:30 on her desktop)."""
    xids = [_upload(store, ('clip-%d' % i).encode() * 80, '100031410%d.mp4' % i,
                    captured_at=1_756_799_520_000 + i * 60_000) for i in range(5)]
    bind_inbound(USER, agent, 'req-1018', parse_inbound([_row(x) for x in xids]))
    return xids


def _ask(recent_file_id=None):
    return _tool(_ctx(USER, recent_file_id=recent_file_id), 'get_user_uploaded_file')()


def test_her_clips_for_this_agent_are_the_answer(store):
    """THE REGRESSION: 46 hours after she sent them, still unused."""
    xids = _her_five_clips(store)
    store.test_clock.now += 46 * 3600

    reply = _ask()

    assert reply != 'No file uploaded from user'
    body = json.loads(reply)
    assert body['count'] == 5
    assert [f['file_id'] for f in body['files']] == xids
    first = body['files'][0]
    assert first['name'] == '1000314100.mp4'
    assert first['filmed_at'] and first['sent_at']
    # Nothing about this computer's disk goes to the model.
    assert 'path' not in first and 'user_id' not in first


def test_another_agents_clips_are_not_this_agents(store):
    _her_five_clips(store, agent='p2')
    assert _ask() == 'No file uploaded from user'


def test_clips_a_reel_already_used_are_not_offered_again(store):
    store.mark_consumed(USER, _her_five_clips(store))
    assert _ask() == 'No file uploaded from user'


def test_clips_the_store_no_longer_keeps_are_not_offered(store):
    _her_five_clips(store)
    store.test_clock.now += ufs.PURPOSES['reel_source'].unused_s + 1
    assert _ask() == 'No file uploaded from user'


def test_a_file_id_sent_with_the_turn_still_answers_first(store):
    _her_five_clips(store)
    cache = TTLCache(ttl_seconds=60, max_size=8, name='test_recent_file_id')
    cache[USER] = 'file-abc123'
    assert _ask(recent_file_id=cache) == 'Got user uploaded file the file_id is file-abc123'
