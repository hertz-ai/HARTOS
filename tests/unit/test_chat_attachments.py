"""core.chat_attachments: a /chat turn's file references in, a reel out.

Reel Studio (plan WS-8a).  The phone uploads clips into the one user file
store (core.user_file_store) and a chat turn carries only references to them
(plan 4.2(e)); the reel the turn makes leaves through the same store's outbox
and rides the reply as ``media`` (4.2(f)).  chat_attachments holds no state of
its own: it parses the turn's ``attachments`` and delegates every read and
write to the store.

ONE attachments shape in and out (amendment A9 a): the documented
ConversationEntry.attachments row ``{file_id, sha256, name, mime, size}``
(file_id = the store xid, which is the sha256) plus optional ``purpose``,
``captured_at`` and ``duration_ms`` -- what media_to_mirror writes is what
parse_inbound reads.

These tests drive the REAL store on a temporary root (installed as the
process singleton the module uses) and assert what a turn can see.
"""
import hashlib
import os

import pytest

from core import user_file_store as ufs
from core.chat_attachments import (
    MAX_ATTACHMENTS, bind_inbound, latest_inbound, media_to_mirror,
    parse_inbound, take_outbound,
)

MP4 = 'video/mp4'


class _Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def store(tmp_path, clock, monkeypatch):
    """The real store on a temp root, as the singleton chat_attachments asks."""
    s = ufs.UserFileStore(str(tmp_path / 'user_files'),
                          disk_free=lambda _p: 1 << 40, clock=clock)
    monkeypatch.setattr(ufs, '_STORE', s)
    return s


def _upload(store, uid, data, *, name='VID.mp4', mime=MP4, purpose='reel_source',
            meta=None):
    """Offer, put and commit one small file; returns its xid."""
    xid = hashlib.sha256(data).hexdigest()
    ans = store.offer(uid, name, len(data), xid, mime, purpose, meta=meta,
                      chunk_size=64 << 10)
    if ans['type'] == 'file_have':
        return xid
    assert ans['type'] == 'file_accept', ans
    for i in range(ans['n']):
        part = data[i * ans['chunk_size']:(i + 1) * ans['chunk_size']]
        assert store.put_chunk(uid, xid, i, part,
                               hashlib.sha256(part).hexdigest())['type'] == 'file_ack'
    assert store.commit(uid, xid)['type'] == 'file_done'
    return xid


def _att(ref, purpose='reel_source', **extra):
    """One /chat attachments row in the documented shape (A9 a)."""
    item = {'file_id': ref, 'sha256': ref, 'name': 'VID.mp4', 'mime': MP4, 'size': 10}
    if purpose is not None:
        item['purpose'] = purpose
    item.update(extra)
    return item


# ── parse_inbound: pure ─────────────────────────────────────────────────

class TestParseInbound:

    def test_a_well_formed_row_comes_back_as_its_reference(self):
        ref = 'a' * 64
        assert parse_inbound([_att(ref, captured_at=1_760_000_000_000)]) == [
            {'file_id': ref, 'purpose': 'reel_source'}]

    def test_purpose_is_optional(self):
        ref = 'a' * 64
        assert parse_inbound([_att(ref, purpose=None)]) == [{'file_id': ref}]

    def test_the_contracts_validator_accepts_the_rows_this_module_reads(self):
        """One shape: each documented row form, which parse_inbound keeps, is
        a valid contracts attachment.  (parse_inbound also keeps rows the
        validator refuses for fields it never reads; see the module
        docstring.)"""
        from integrations.reel_studio import contracts
        for row in (_att('a' * 64), _att('b' * 64, purpose=None),
                    _att('c' * 64, captured_at=1_760_000_000_000, duration_ms=4000)):
            assert parse_inbound([row]), row
            assert contracts.validate_attachment(row) == [], row

    def test_what_the_mirror_writes_is_what_a_turn_reads(self):
        """inbound == outbound: a reply's mirror row, sent back on a later turn,
        parses as the same file."""
        xid = 'ab' * 32
        [row] = media_to_mirror([dict(_media('r-1'), xid=xid, sha256=xid, size=99)])
        assert parse_inbound([row]) == [{'file_id': xid}]
        from integrations.reel_studio import contracts
        assert contracts.validate_attachment(row) == []

    def test_at_most_max_attachments_are_read(self):
        raw = [_att('%064x' % i) for i in range(MAX_ATTACHMENTS + 3)]
        got = parse_inbound(raw)
        assert len(got) == MAX_ATTACHMENTS
        assert [g['file_id'] for g in got] == [r['file_id'] for r in raw[:MAX_ATTACHMENTS]]

    def test_a_reference_named_twice_is_kept_once(self):
        ref = 'b' * 64
        assert [g['file_id'] for g in parse_inbound([_att(ref), _att(ref, 'logo')])] == [ref]

    @pytest.mark.parametrize('bad', [
        'not a dict',
        None,
        {'sha256': 'c' * 64, 'purpose': 'reel_source'},           # no file_id
        _att('c' * 64) | {'sha256': 'e' * 64},                     # file_id is not the sha
        {k: v for k, v in _att('c' * 64).items() if k != 'sha256'},  # no sha256
        {'kind': 'peerlink_file', 'ref': 'c' * 64,                 # the retired shape
         'purpose': 'reel_source'},
        _att('C' * 64),                                            # not lowercase hex
        _att('c' * 63),                                            # too short
        _att('../../etc/passwd'),                                  # not an xid at all
        _att('c' * 64, 'reel'),                                    # an outbound purpose
        _att('c' * 64, 'anything'),
    ])
    def test_a_malformed_entry_is_dropped_not_raised_on(self, bad):
        assert parse_inbound([bad, _att('d' * 64)]) == [
            {'file_id': 'd' * 64, 'purpose': 'reel_source'}]

    @pytest.mark.parametrize('raw', [None, '', [], {}, 'a' * 64, 42, {'file_id': 'a' * 64}])
    def test_anything_but_a_list_reads_as_no_attachments(self, raw):
        assert parse_inbound(raw) == []

    def test_the_purposes_are_the_contracts_purposes(self):
        """core may not import integrations, so the store owns the purposes
        and the contracts import them (A9 d): a purpose a row may name is one
        the contracts accept."""
        from integrations.reel_studio import contracts
        assert tuple(ufs.INBOUND_PURPOSES) == tuple(contracts.INBOUND_PURPOSES)
        assert not hasattr(contracts, 'ATTACHMENT_KIND'), (
            'the {kind, ref} attachment shape is retired (A9 a)')


# ── bind_inbound: the store binds, foreign refs are dropped ─────────────

class TestBindInbound:

    def test_binds_this_users_files_to_the_turn(self, store):
        a = _upload(store, 'u1', b'clip-a' * 50)
        b = _upload(store, 'u1', b'clip-b' * 50)
        got = bind_inbound('u1', '777', 'rid-1', parse_inbound([_att(a), _att(b)]))
        assert [g['xid'] for g in got] == [a, b]
        assert {(g['prompt_id'], g['request_id']) for g in got} == {('777', 'rid-1')}
        assert [f.xid for f in store.query_inbox('u1', prompt_id='777',
                                                 request_id='rid-1')] == [a, b]

    def test_another_users_file_is_not_bound_and_stays_theirs(self, store, caplog):
        mine = _upload(store, 'u1', b'mine' * 50)
        theirs = _upload(store, 'u2', b'theirs' * 50)
        got = bind_inbound('u1', '777', 'rid-2',
                           parse_inbound([_att(mine), _att(theirs), _att('e' * 64)]))
        assert [g['xid'] for g in got] == [mine]
        assert [f.xid for f in store.query_inbox('u1', request_id='rid-2')] == [mine]
        # u2's file was not touched: still unbound.
        rec = store.resolve('u2', theirs)
        assert rec.prompt_id is None and rec.request_id is None
        assert 'dropped' in caplog.text.lower(), 'a dropped ref must be logged'

    def test_prompt_id_123_and_string_123_bind_the_same(self, store):
        a = _upload(store, 'u1', b'int-or-str' * 40)
        bind_inbound('u1', 123, 'rid-3', parse_inbound([_att(a)]))
        assert [f.xid for f in store.query_inbox('u1', prompt_id='123')] == [a]

    @pytest.mark.parametrize('uid', [None, '', True])
    def test_a_bad_user_binds_nothing_and_does_not_raise(self, store, uid):
        a = _upload(store, 'u1', b'safe' * 50)
        assert bind_inbound(uid, '777', 'rid', parse_inbound([_att(a)])) == []

    def test_no_items_touch_nothing(self, store, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError('the store was asked to bind nothing')
        monkeypatch.setattr(store, 'bind', boom)
        assert bind_inbound('u1', '777', 'rid', []) == []

    def test_a_failing_store_is_logged_and_binds_nothing(self, store, monkeypatch):
        def broken(*_a, **_k):
            raise OSError('disk gone')
        monkeypatch.setattr(store, 'bind', broken)
        assert bind_inbound('u1', '777', 'rid', parse_inbound([_att('f' * 64)])) == []

    def _clip_also_sent_in_chat(self, store, clock):
        """Her clip, bound to reel turn (55, r1), whose bytes she later sent
        in chat as an attachment (the same file, a second purpose)."""
        clip = b'clip-and-attachment' * 40
        xid = _upload(store, 'u1', clip)
        bind_inbound('u1', '55', 'r1', parse_inbound([_att(xid)]))
        clock.now += 5
        assert store.put_file('u1', clip, name='v.mp4', mime=MP4,
                              purpose='chat_attachment')['type'] == 'file_done'
        return xid

    def test_a_row_binds_the_purpose_it_names_and_leaves_the_others(self, store, clock):
        xid = self._clip_also_sent_in_chat(store, clock)

        got = bind_inbound('u1', '77', 'r2',
                           parse_inbound([_att(xid, 'chat_attachment', name='v.mp4')]))

        assert [(g['xid'], g['purpose'], g['prompt_id']) for g in got] == \
            [(xid, 'chat_attachment', '77')]
        assert [g['xid'] for g in latest_inbound('u1', '55', request_id='r1')] == [xid]

    def test_a_row_without_purpose_binds_the_file_as_she_last_sent_it(self, store, clock):
        xid = self._clip_also_sent_in_chat(store, clock)

        got = bind_inbound('u1', '77', 'r2', parse_inbound([_att(xid, purpose=None)]))

        assert [(g['xid'], g['purpose']) for g in got] == [(xid, 'chat_attachment')]
        assert [g['xid'] for g in latest_inbound('u1', '55', request_id='r1')] == [xid]

    def test_a_row_naming_a_purpose_the_file_was_not_sent_for_is_dropped(self, store, caplog):
        clip = _upload(store, 'u1', b'only-a-clip' * 40)

        assert bind_inbound('u1', '9', 'r', parse_inbound([_att(clip, 'logo')])) == []
        assert 'dropped' in caplog.text.lower()
        assert store.resolve('u1', clip).prompt_id is None

    def test_rows_of_several_purposes_come_back_in_the_order_sent(self, store):
        clip = _upload(store, 'u1', b'clip' * 60)
        logo = _upload(store, 'u1', b'\x89PNG' * 60, name='logo.png', mime='image/png',
                       purpose='logo')
        song = _upload(store, 'u1', b'ID3' * 60, name='s.mp3', mime='audio/mpeg',
                       purpose='song_audio')

        got = bind_inbound('u1', '9', 'r', parse_inbound(
            [_att(song, 'song_audio'), _att(clip), _att(logo, 'logo')]))

        assert [(g['xid'], g['purpose']) for g in got] == \
            [(song, 'song_audio'), (clip, 'reel_source'), (logo, 'logo')]


# ── latest_inbound: what a reel turn sees ───────────────────────────────

class TestLatestInbound:

    def test_this_turns_clips_in_upload_order(self, store):
        c1 = _upload(store, 'u1', b'one' * 60)
        c2 = _upload(store, 'u1', b'two' * 60)
        c3 = _upload(store, 'u1', b'three' * 60)
        bind_inbound('u1', '9', 'r1', parse_inbound([_att(c3), _att(c1)]))
        bind_inbound('u1', '9', 'r2', parse_inbound([_att(c2)]))
        assert [g['xid'] for g in latest_inbound('u1', '9', request_id='r1')] == [c1, c3]
        assert [g['xid'] for g in latest_inbound('u1', '9', request_id='r2')] == [c2]

    def test_without_a_request_the_newest_batch(self, store):
        old1 = _upload(store, 'u1', b'old1' * 60)
        old2 = _upload(store, 'u1', b'old2' * 60)
        bind_inbound('u1', '9', 'turn-a', parse_inbound([_att(old1), _att(old2)]))
        new1 = _upload(store, 'u1', b'new1' * 60)
        bind_inbound('u1', '9', 'turn-b', parse_inbound([_att(new1)]))
        assert [g['xid'] for g in latest_inbound('u1', '9')] == [new1]

    def test_the_items_carry_what_the_pipeline_reads(self, store):
        c = _upload(store, 'u1', b'cap' * 60,
                    meta={'captured_at': 1_760_000_123_000, 'duration_ms': 4000})
        bind_inbound('u1', '9', 'r', parse_inbound([_att(c)]))
        [item] = latest_inbound('u1', '9', request_id='r')
        from integrations.reel_studio.contracts import ClipSource
        src = ClipSource.from_stored(item)
        assert (src.xid, src.captured_at_ms, src.duration_ms) == (c, 1_760_000_123_000, 4000)
        with open(src.path, 'rb') as f:
            assert f.read() == b'cap' * 60

    def test_purpose_age_and_consumption_filter(self, store, clock):
        clip = _upload(store, 'u1', b'clip' * 60)
        logo = _upload(store, 'u1', b'\x89PNG' * 60, name='logo.png',
                       mime='image/png', purpose='logo')
        bind_inbound('u1', '9', 'r', parse_inbound([_att(clip), _att(logo, 'logo')]))
        assert [g['xid'] for g in latest_inbound('u1', '9', request_id='r')] == [clip]
        assert [g['xid'] for g in latest_inbound('u1', '9', request_id='r',
                                                 purpose='logo')] == [logo]
        clock.now += 7 * 3600
        assert latest_inbound('u1', '9', request_id='r') == []
        assert [g['xid'] for g in latest_inbound('u1', '9', request_id='r',
                                                 max_age_s=8 * 3600)] == [clip]
        store.mark_consumed('u1', [clip])
        assert latest_inbound('u1', '9', request_id='r', max_age_s=8 * 3600) == []

    def test_another_agents_clips_are_not_this_agents(self, store):
        c = _upload(store, 'u1', b'agent-a' * 40)
        bind_inbound('u1', 'agent-a', 'r', parse_inbound([_att(c)]))
        assert latest_inbound('u1', 'agent-b', request_id='r') == []
        assert latest_inbound('u1', 'agent-b') == []

    def test_a_bad_user_reads_as_nothing(self, store):
        assert latest_inbound(None, '9') == []


# ── take_outbound / media_to_mirror: the reel leaves with the reply ────

def _media(reel_id='reel-1'):
    return {'kind': 'video', 'xid': '', 'sha256': '', 'mime': MP4, 'size': 0,
            'width': 1080, 'height': 1920, 'duration_s': 20.0, 'has_audio': False,
            'reel_id': reel_id, 'prompt_id': '', 'request_id': None,
            'caption': 'Bridal look', 'hashtags': ['#bridalmakeup'], 'song': None,
            'notes': [], 'qa': {'passed': True, 'failed': []}}


@pytest.fixture
def reel(tmp_path):
    p = tmp_path / 'out.mp4'
    p.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'reel' * 300)
    return str(p)


class TestTakeOutbound:

    def test_the_turns_reel_is_taken_once(self, store, reel):
        sf = store.offer_to_device('u1', reel, mime=MP4, purpose='reel',
                                   prompt_id='42', request_id='rid-9',
                                   meta={'media': _media()})
        got = take_outbound('u1', '42', 'rid-9')
        assert len(got) == 1
        assert got[0]['xid'] == got[0]['sha256'] == sf.xid
        assert got[0]['request_id'] == 'rid-9' and got[0]['prompt_id'] == '42'
        assert got[0]['caption'] == 'Bridal look'
        assert take_outbound('u1', '42', 'rid-9') == [], 'a reel rides ONE reply'

    def test_another_turns_or_agents_reel_is_not_taken(self, store, reel):
        store.offer_to_device('u1', reel, mime=MP4, purpose='reel', prompt_id='42',
                              request_id='rid-9', meta={'media': _media()})
        assert take_outbound('u1', '42', 'other-rid') == []
        assert take_outbound('u1', '43', 'rid-9') == []
        assert take_outbound('u2', '42', 'rid-9') == []
        assert len(take_outbound('u1', 42, 'rid-9')) == 1   # int and str are one id

    def test_no_prompt_or_a_bad_user_takes_nothing(self, store, reel):
        store.offer_to_device('u1', reel, mime=MP4, purpose='reel', prompt_id='42',
                              request_id='rid-9', meta={'media': _media()})
        assert take_outbound('u1', None, 'rid-9') == []
        assert take_outbound(None, '42', 'rid-9') == []
        assert len(take_outbound('u1', '42', 'rid-9')) == 1

    def test_an_outbound_file_without_a_media_item_is_not_in_the_reply(self, store, reel):
        store.offer_to_device('u1', reel, mime=MP4, purpose='reel', prompt_id='42',
                              request_id='rid-9', meta={'note': 'no media item'})
        assert take_outbound('u1', '42', 'rid-9') == []

    def test_a_reply_for_a_user_with_no_files_leaves_no_store_state(self, store):
        """_chat_reply asks on every reply that has a prompt, on every node,
        central included.  A user the store holds nothing for gets nothing
        and leaves nothing behind: no directory, and no entry in the per-user
        lock registry, which is never evicted (one lock per user ever seen)."""
        before = set(ufs._LOCKS)
        assert take_outbound('never-sent-a-file', '42', 'rid-1') == []
        assert set(ufs._LOCKS) == before, (
            'a reply registered a store lock for a user with no files')
        assert not os.path.exists(store.root), 'a reply created the store root'


class TestMediaToMirror:

    def test_the_documented_mirror_shape(self):
        xid = 'ab' * 32
        item = dict(_media('r-77'), xid=xid, sha256=xid, size=4242)
        assert media_to_mirror([item]) == [{
            'file_id': xid, 'sha256': xid, 'name': 'reel_r-77.mp4',
            'mime': MP4, 'size': 4242}]

    def test_a_reel_id_unfit_for_a_name_falls_back_to_the_xid(self):
        xid = 'cd' * 32
        item = dict(_media('../../x y'), xid=xid, sha256=xid, size=1)
        assert media_to_mirror([item])[0]['name'] == 'reel_%s.mp4' % xid[:12]

    @pytest.mark.parametrize('media', [None, [], 'x', [None, {'xid': 'nope'}, 7]])
    def test_nothing_mirrorable_is_an_empty_list(self, media):
        assert media_to_mirror(media) == []
