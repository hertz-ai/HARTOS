"""get_user_uploaded_file answers with every file a person gave the agent.

Measured live 2026-11-26 10:44-10:57 IST on the desktop (server.log.1,
build #5), the speech agent 95568353503 being built for user 10202: flow 7
step 3 asks the parent for "family members' home names, relation and
photos, and photos of the child, and save family_members".  The parent
answered five file_request cards with photos (asks c8478938.., 4737e863..,
c99c2603.., aa0e1680.., 816ed89e..).  person_ask._bind_picks bound every
photo to the agent under the ask's purpose, chat_attachment (the store's
index holds all five for prompt 95568353503, each with its ask id as the
request).  The agent called get_user_uploaded_file eight times, with no
argument, with a name and once with a photo's own file id, read "No file
uploaded from user" every time, and asked for the photos six more times.
The owner: "I uploaded lots of photos of dad and mom and family still the
agent keeps asking".

The tool read the turn's own file_id and then only the reel batch
(core.chat_attachments.latest_inbound, whose purpose is the reel's clips).
It now goes on to every other file this person gave THIS agent, newest
first, in pages of whole files, and a file id given as the name answers
that file (the store's own read, UserFileStore.resolve).  The reel answer,
and latest_inbound's reel default that reel_make reads, are unchanged.

Real store on a temporary root, installed as the process singleton the
tool asks; real tool closures from the factory; the photos are bound the
way the card's answer binds them (person_ask._bind_picks).

    python -m pytest tests/unit/test_an_agent_sees_the_files_a_person_gave_it.py --noconftest -q
"""
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from core import user_file_store as ufs
from core.chat_attachments import bind_inbound, latest_inbound, parse_inbound
from tests.unit.test_core_tools_uuid_user_id import _ctx, _on_the_persons_turn, _tool

USER = '10202'
#: The agent the factory's context names (_ctx: prompt_id 'p1').
AGENT = 'p1'
NO_FILE = 'No file uploaded from user'


class _Clock:
    def __init__(self):
        self.now = 1_795_670_000.0

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


def _upload(store, data, name, mime='image/jpeg', purpose='chat_attachment', user=USER):
    """A device's offer, chunk and commit of one file; returns its xid."""
    xid = hashlib.sha256(data).hexdigest()
    ans = store.offer(user, name, len(data), xid, mime, purpose)
    assert ans['type'] == 'file_accept', ans
    assert store.put_chunk(user, xid, 0, data, xid)['type'] == 'file_ack'
    assert store.commit(user, xid)['type'] == 'file_done'
    store.test_clock.now += 60
    return xid


def _answer_card(xids, ask_id, agent=AGENT, user=USER, purpose='chat_attachment'):
    """Her picks on a file_request card become the asking agent's files: the
    card's own binding (person_ask._bind_picks)."""
    from integrations.agent_engine.person_ask import _bind_picks
    ask = SimpleNamespace(ask_id=ask_id, user_id=user, prompt_id=agent, purpose=purpose)
    assert _bind_picks(ask, list(xids)) is None


def _photo(store, i, user=USER):
    return _upload(store, (f'photo of the family {i} ' * 40).encode(), f'IMG_{i:04d}.jpg',
                   user=user)


def _ask(name='', offset=0, ctx=None):
    # Her own turn: on a turn that is not a person's, nobody can upload.
    return _on_the_persons_turn(
        'req-her-turn',
        _tool(ctx or _ctx(USER), 'get_user_uploaded_file'), name, offset)


def _listed(reply):
    assert reply != NO_FILE, 'the files she gave this agent were not seen'
    body = json.loads(reply)
    return body, [f['file_id'] for f in body['files']]


# -- the regression ---------------------------------------------------------

def test_a_photo_given_on_a_file_request_is_seen(store):
    """THE REGRESSION: one photo answered on a chat_attachment file_request."""
    xid = _photo(store, 1)
    _answer_card([xid], 'c847893838074258afbe0b99f0619bb8')

    body, ids = _listed(_ask())

    assert ids == [xid]
    row = body['files'][0]
    assert row['name'] == 'IMG_0001.jpg' and row['mime'] == 'image/jpeg'
    assert row['purpose'] == 'chat_attachment'
    assert row['sent_for'] == 'c847893838074258afbe0b99f0619bb8'
    assert row['sent_at']
    # Nothing about this computer's disk goes to the model.
    assert 'path' not in row and 'user_id' not in row


def test_the_photos_of_every_ask_are_seen_newest_first(store):
    """Three cards answered one after another: all three photos, newest first
    (her latest answer is what the step just asked for)."""
    xids = []
    for i, ask in enumerate(('4737e86327494741852e69e33531ae09',
                             'c99c26035b05475684cdc7f0dd43ae59',
                             'aa0e168065934efe915f393df27c0368')):
        xid = _photo(store, i)
        _answer_card([xid], ask)
        xids.append(xid)

    body, ids = _listed(_ask())

    assert ids == list(reversed(xids))
    assert body['count'] == 3


def test_a_file_id_given_as_the_name_answers_that_file(store):
    """Live 10:55:32: the agent passed one photo's file id as the name."""
    first, second = _photo(store, 1), _photo(store, 2)
    _answer_card([first], 'a' * 32)
    _answer_card([second], 'b' * 32)

    _body, ids = _listed(_ask(name=second))

    assert ids == [second]


def test_a_page_never_cuts_a_file_and_says_where_the_rest_is(store, monkeypatch):
    """Pages hold whole files: every page is JSON, the files of all pages
    together are each file once, newest first, and a page that is not the
    last names the offset of the next."""
    import core.constants
    monkeypatch.setattr(core.constants, 'TOOL_OBSERVATION_MAX_CHARS', 700)
    xids = []
    for i in range(12):
        xid = _photo(store, i)
        _answer_card([xid], f'{i:032x}')
        xids.append(xid)

    seen, offset, pages = [], 0, 0
    while True:
        reply = _ask(offset=offset)
        body, ids = _listed(reply)
        pages += 1
        assert ids, 'a page with no file'
        seen += ids
        if len(seen) == len(xids):
            break
        assert f'offset={len(seen)}' in body['note'], body['note']
        offset = len(seen)
        assert pages < len(xids), 'paging never ends'
    assert pages > 1, 'the budget never paged the list'
    assert seen == list(reversed(xids))


def test_the_step_saves_the_file_id_the_list_gave(store):
    """The tool the waiting step calls next: flow 7 step 3 ends "... and save
    family_members", so the listed file id goes into save_data_in_memory and
    comes back from get_data_by_key."""
    xid = _photo(store, 1)
    _answer_card([xid], 'd0ef18bf146745ec87701c921219769a')
    _body, ids = _listed(_ask())

    class _Helper:
        def save_agent_data_to_file(self, *_a, **_k):
            return True

        def load_agent_data_from_file(self, *_a, **_k):
            return False

    ctx = _ctx(USER, helper_fun=_Helper())
    family = [{'name': 'Appa', 'relation': 'father', 'photo_file_id': ids[0]}]
    saved = _on_the_persons_turn('req-her-turn', _tool(ctx, 'save_data_in_memory'),
                                 'family_members', family)
    assert not saved.startswith('Error'), saved
    read = _on_the_persons_turn('req-her-turn', _tool(ctx, 'get_data_by_key'),
                                'family_members')
    assert ids[0] in read, read


# -- guards: what is not hers or not this agent's, and the reel ---------------

def test_another_agents_and_another_persons_files_are_not_listed(store):
    _answer_card([_photo(store, 1)], 'e' * 32, agent='p2')
    _answer_card([_photo(store, 2, user='777')], 'f' * 32, user='777')
    assert _ask() == NO_FILE


def test_a_file_sent_with_no_turn_is_not_this_agents(store):
    _photo(store, 1)
    assert _ask() == NO_FILE


def _reel_clip(store, i):
    return _upload(store, (f'clip-{i}' * 80).encode(), f'VID_{i}.mp4', mime='video/mp4',
                   purpose='reel_source')


def test_reel_studios_answer_is_the_reel_batch_exactly_as_before(store):
    """Reel Studio (agent 342) with clips: the reel batch, in its own shape
    and note, byte for byte, even with a chat photo given to it too."""
    clips = [_reel_clip(store, i) for i in range(3)]
    bind_inbound(USER, AGENT, 'req-reel', parse_inbound([
        {'file_id': x, 'sha256': x, 'name': f'VID_{i}.mp4', 'mime': 'video/mp4',
         'size': 10, 'purpose': 'reel_source'} for i, x in enumerate(clips)]))
    _answer_card([_photo(store, 9)], '9' * 32)

    files = latest_inbound(USER, AGENT)

    def _at(seconds):
        from datetime import datetime
        return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec='seconds')
    expected = json.dumps({
        'count': len(files),
        'files': [{'file_id': f['xid'], 'name': f['name'], 'mime': f['mime'],
                   'size': f['size'], 'sent_at': _at(f['created_at']), 'filmed_at': None}
                  for f in files],
        'note': ('The files she sent this agent that no reel has used yet, '
                 'in the order she sent them.'),
    })
    assert [f['xid'] for f in files] == clips
    assert _ask() == expected


def test_latest_inbound_still_reads_only_the_reels_clips(store):
    """reel_make's clips (integrations.reel_studio.tools._clips) read
    latest_inbound's default: a chat photo given to the agent is not a clip."""
    _answer_card([_photo(store, 1)], '1' * 32)
    assert latest_inbound(USER, AGENT) == []


def test_reel_make_given_a_listed_chat_video_says_what_to_do(store):
    """Q3 of the review: a reel agent now sees a chat video it was given; the
    video is not a reel clip, and reel_make says to ask for clips."""
    from integrations.reel_studio.pipeline import NEEDS_CLIPS_MESSAGE
    from integrations.reel_studio.tools import build_reel_tools
    video = _upload(store, b'chat video ' * 90, 'VID_chat.mp4', mime='video/mp4')
    _answer_card([video], '2' * 32)
    _body, ids = _listed(_ask())
    assert ids == [video]

    tools = dict(build_reel_tools(USER, prompt_id=AGENT,
                                  request_id_getter=lambda: 'req-her-turn'))
    said = tools['reel_make'](clips=video)

    assert 'needs_clips' in said
    assert ' '.join(NEEDS_CLIPS_MESSAGE.split()) in said
    assert 'reel_source' in said


def test_asking_by_the_ask_id_gives_the_file_ids_of_an_answered_card():
    """ask_person(ask_id=...) of an answered file_request returns the ids of
    the files she picked (live 10:50:48)."""
    from integrations.agent_engine import person_ask
    xid = hashlib.sha256(b'one photo').hexdigest()
    ask_id = '4737e86327494741852e69e33531ae09'
    with person_ask._lock:
        person_ask._asks[ask_id] = SimpleNamespace(
            ask_id=ask_id, user_id=USER, agent_id=AGENT, prompt_id=AGENT,
            ctype='file_request', dismissed=False, answer={'xids': [xid]},
            answered_at=time.time(), created=time.time(), waiters=0)
    try:
        _name, _desc, ask_person = person_ask.ask_person_tool(
            USER, AGENT, agent_id=AGENT, request_id=lambda: 'req-her-turn')
        reply = json.loads(ask_person(component='', ask_id=ask_id))
    finally:
        with person_ask._lock:
            person_ask._asks.pop(ask_id, None)
    assert reply['answered'] is True and reply['status'] == 'answered'
    assert reply['answer'] == {'xids': [xid]}
