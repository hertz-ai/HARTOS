"""core.user_file_store: the ONE store for a user's phone files (WS-7a).

The phone uploads clips, a logo and song audio in chunks; the desktop sends
the finished reel back from an outbox.  These tests drive the real store on a
temporary root, with the clock and the free-disk probe injected so the
retention windows and the disk check are exact.  No network, no PeerLink.

Covers plan section 5 row 7a: offer/put/commit; out-of-order and duplicate
chunks; chunk and file hash mismatch; resume from the sidecar in a new store
instance; abort; path names; no_space; quota; the 2-upload limit;
bind/query/consume; take_replied marks once; list_outbox(prompt_id=); the GC
windows; another user's xid is not found; prompt_id 123 and "123" bind the
same.  Plus amendment A1: the phone's captured_at rides the offer into the
index, and a bad one is dropped rather than refusing the upload.
"""
import errno
import hashlib
import json
import os
import random
import subprocess
import sys
import threading

import pytest

import core.platform_paths as pp
import core.user_file_store as ufs
from core.user_file_store import POLICY, StoredFile, UserFileStore, get_user_file_store
from integrations.reel_studio import contracts as C

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB = 1024
CS = 64 * KB                      # chunk size the tests upload with
HOUR = 3600.0
T0 = 1_760_000_000.0              # the fake clock's start (epoch seconds)
CAPTURED = 1_760_000_000_000      # a phone DATE_TAKEN (epoch ms)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / 'user_files')


@pytest.fixture
def store(root, clock):
    return UserFileStore(root, disk_free=lambda path: 1 << 40, clock=clock)


def blob(n, seed=1):
    return random.Random(seed).randbytes(n)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def chunks(data, cs=CS):
    return [data[k:k + cs] for k in range(0, len(data), cs)]


def put(store, user, data, i, cs=CS):
    part = chunks(data, cs)[i]
    return store.put_chunk(user, sha(data), i, part, sha(part))


def upload(store, user, data, *, name='clip.mp4', mime='video/mp4', purpose='reel_source',
           meta=None):
    """Offer, put every chunk and commit; return the xid."""
    reply = store.offer(user, name, len(data), sha(data), mime, purpose, meta=meta,
                        chunk_size=CS)
    assert reply['type'] == 'file_accept', reply
    for i in range(reply['n']):
        assert put(store, user, data, i)['type'] == 'file_ack'
    done = store.commit(user, sha(data))
    assert done == {'type': 'file_done', 'xid': sha(data)}, done
    return sha(data)


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def all_files(root):
    return sorted(os.path.relpath(os.path.join(d, f), root)
                  for d, _, fs in os.walk(root) for f in fs)


def reel_file(tmp_path, data, name='reel.mp4'):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def media_item(reel_id='r-1', prompt_id='123', request_id='req-1'):
    """A reply media item as the pipeline builds it, before the store fills in
    the facts only it knows (xid, sha256, size)."""
    return {'kind': 'video', 'mime': 'video/mp4', 'width': 1080, 'height': 1920,
            'duration_s': 20.0, 'has_audio': False, 'reel_id': reel_id,
            'prompt_id': prompt_id, 'request_id': request_id, 'caption': 'Bridal look',
            'hashtags': ['#bridalmakeup'], 'song': None, 'notes': [],
            'qa': {'passed': True, 'failed': []}}


def send_reel(store, tmp_path, user='u1', data=None, *, prompt_id='123', request_id='req-1',
              seed=7):
    data = data if data is not None else blob(150 * KB, seed)
    path = reel_file(tmp_path, data, 'reel_%d.mp4' % seed)
    return store.offer_to_device(user, path, mime='video/mp4', purpose='reel',
                                 prompt_id=prompt_id, request_id=request_id,
                                 meta={'media': media_item(prompt_id=str(prompt_id),
                                                           request_id=request_id)})


# ── upload: offer, put, commit ────────────────────────────────────────────

def test_offer_put_commit_lands_the_file_in_the_inbox(store, root, clock):
    data = blob(200 * KB)

    reply = store.offer('u1', 'VID_1.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                        chunk_size=CS)

    assert reply == {'type': 'file_accept', 'xid': sha(data), 'chunk_size': CS, 'n': 4,
                     'missing': [[0, 3]], 'max_inflight': POLICY['max_inflight']}
    for i in range(4):
        assert put(store, 'u1', data, i) == {'type': 'file_ack', 'xid': sha(data), 'i': i}
    assert store.commit('u1', sha(data)) == {'type': 'file_done', 'xid': sha(data)}

    got = store.resolve('u1', sha(data))
    assert isinstance(got, StoredFile)
    assert (got.box, got.name, got.mime, got.size, got.sha256, got.purpose) == \
        ('in', 'VID_1.mp4', 'video/mp4', len(data), sha(data), 'reel_source')
    assert got.created_at == clock.now and got.consumed_at is None
    assert read(got.path) == data
    assert os.path.commonpath([root, got.path]) == root
    assert all('.part' not in f for f in all_files(root))


def test_chunks_may_arrive_out_of_order_and_twice(store):
    data = blob(300 * KB, 2)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)

    for i in (4, 1, 1, 0, 3, 4, 2, 0):
        assert put(store, 'u1', data, i)['type'] == 'file_ack'

    assert store.commit('u1', sha(data))['type'] == 'file_done'
    assert read(store.resolve('u1', sha(data)).path) == data


def test_a_chunk_whose_hash_does_not_match_is_refused_and_not_kept(store):
    data = blob(200 * KB, 3)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)

    bad = store.put_chunk('u1', sha(data), 1, chunks(data)[1], sha(b'something else'))

    assert bad == {'type': 'file_refused', 'reason': 'chunk_corrupt', 'xid': sha(data), 'i': 1}
    again = store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                        chunk_size=CS)
    assert again['missing'] == [[0, 3]]


def test_a_chunk_of_the_wrong_length_is_refused(store):
    data = blob(200 * KB, 4)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)
    short = chunks(data)[0][:-1]

    assert store.put_chunk('u1', sha(data), 0, short, sha(short))['reason'] == 'chunk_corrupt'
    assert store.put_chunk('u1', sha(data), 0, 'not bytes', sha(b''))['reason'] == 'chunk_corrupt'


@pytest.mark.parametrize('i', [4, -1, '0', 1.0, True, None])
def test_a_chunk_index_outside_the_file_is_refused(store, i):
    data = blob(200 * KB, 5)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)

    reply = store.put_chunk('u1', sha(data), i, chunks(data)[0], sha(chunks(data)[0]))

    assert reply['type'] == 'file_refused' and reply['reason'] == 'out_of_range'


@pytest.mark.parametrize('xid', ['f' * 64, '../../evil', 'F' * 64, '', None, 7])
def test_a_put_for_an_upload_never_offered_is_refused(store, root, xid):
    reply = store.put_chunk('u1', xid, 0, b'abc', sha(b'abc'))

    assert reply['type'] == 'file_refused' and reply['reason'] == 'unknown_xid'
    assert store.commit('u1', xid)['reason'] == 'unknown_xid'
    assert all_files(root) == []


def test_commit_with_chunks_missing_names_them(store):
    data = blob(400 * KB, 6)     # 7 chunks
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)
    for i in (0, 3, 4):
        put(store, 'u1', data, i)

    reply = store.commit('u1', sha(data))

    assert reply == {'type': 'file_corrupt', 'xid': sha(data), 'missing': [[1, 2], [5, 6]]}
    assert store.resolve('u1', sha(data)) is None


def test_a_file_that_does_not_hash_to_its_xid_starts_over(store):
    claimed = blob(200 * KB, 7)
    sent = blob(200 * KB, 8)          # every chunk matches its own hash, the whole does not
    store.offer('u1', 'a.mp4', len(claimed), sha(claimed), 'video/mp4', 'reel_source',
                chunk_size=CS)
    for i, part in enumerate(chunks(sent)):
        assert store.put_chunk('u1', sha(claimed), i, part, sha(part))['type'] == 'file_ack'

    reply = store.commit('u1', sha(claimed))

    assert reply == {'type': 'file_corrupt', 'xid': sha(claimed), 'missing': [[0, 3]]}
    assert store.resolve('u1', sha(claimed)) is None
    assert store.offer('u1', 'a.mp4', len(claimed), sha(claimed), 'video/mp4', 'reel_source',
                       chunk_size=CS)['missing'] == [[0, 3]]


def test_an_upload_resumes_from_its_sidecar_in_a_new_store(root, clock):
    data = blob(250 * KB, 9)          # 4 chunks
    first = UserFileStore(root, disk_free=lambda p: 1 << 40, clock=clock)
    first.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS,
                meta={'captured_at': CAPTURED})
    put(first, 'u1', data, 0)
    put(first, 'u1', data, 2)

    restarted = UserFileStore(root, disk_free=lambda p: 1 << 40, clock=clock)
    reply = restarted.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                            chunk_size=CS)

    assert reply['missing'] == [[1, 1], [3, 3]]
    put(restarted, 'u1', data, 1)
    put(restarted, 'u1', data, 3)
    assert restarted.commit('u1', sha(data))['type'] == 'file_done'
    got = restarted.resolve('u1', sha(data))
    assert read(got.path) == data
    assert got.meta == {'captured_at': CAPTURED}


def test_a_resume_on_smaller_chunks_keeps_the_bytes_already_sent(store):
    data = blob(300 * KB, 10)
    big = 128 * KB
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=big)
    put(store, 'u1', data, 0, cs=big)       # bytes 0..128K

    reply = store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                        chunk_size=CS)        # the link moved to the relay

    assert reply['chunk_size'] == CS and reply['n'] == 5
    assert reply['missing'] == [[2, 4]]
    for i in (2, 3, 4):
        put(store, 'u1', data, i)
    assert store.commit('u1', sha(data))['type'] == 'file_done'
    assert read(store.resolve('u1', sha(data)).path) == data


def test_offering_a_file_already_held_answers_file_have(store):
    data = blob(100 * KB, 11)
    xid = upload(store, 'u1', data)

    reply = store.offer('u1', 'again.mp4', len(data), xid, 'video/mp4', 'reel_source',
                        chunk_size=CS)

    assert reply == {'type': 'file_have', 'xid': xid}
    assert store.resolve('u1', xid).name == 'again.mp4'


def test_offering_a_consumed_file_again_makes_it_usable_again(store, clock):
    data = blob(100 * KB, 12)
    xid = upload(store, 'u1', data)
    store.mark_consumed('u1', [xid])
    clock.now += 60

    assert store.offer('u1', 'c.mp4', len(data), xid, 'video/mp4', 'reel_source',
                       meta={'captured_at': CAPTURED})['type'] == 'file_have'

    got = store.resolve('u1', xid)
    assert got.consumed_at is None and got.created_at == clock.now
    assert got.meta == {'captured_at': CAPTURED}
    assert [f.xid for f in store.query_inbox('u1')] == [xid]


def test_a_held_file_that_vanished_is_uploaded_again(store):
    data = blob(100 * KB, 13)
    xid = upload(store, 'u1', data)
    os.remove(store.resolve('u1', xid).path)

    assert store.resolve('u1', xid) is None
    assert store.offer('u1', 'a.mp4', len(data), xid, 'video/mp4', 'reel_source',
                       chunk_size=CS)['type'] == 'file_accept'


def test_abort_discards_the_partial_upload(store):
    data = blob(200 * KB, 14)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)
    put(store, 'u1', data, 0)
    assert store.usage('u1') == len(data)

    assert store.abort('u1', sha(data)) == {'type': 'file_aborted', 'xid': sha(data)}

    assert store.usage('u1') == 0
    assert put(store, 'u1', data, 1)['reason'] == 'unknown_xid'
    assert store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                       chunk_size=CS)['missing'] == [[0, 3]]
    assert store.abort('u1', 'e' * 64) == {'type': 'file_aborted', 'xid': 'e' * 64}


# ── what an offer may carry ───────────────────────────────────────────────

@pytest.mark.parametrize('name', ['../evil.mp4', '..\\evil.mp4', 'a/b.mp4', 'a\\b.mp4', '',
                                  '..', '.', 'x\x00.mp4', 'line\nbreak.mp4', 'x' * 256, None, 5])
def test_a_name_that_is_not_a_bare_file_name_is_refused(store, root, name):
    data = blob(10 * KB, 15)

    reply = store.offer('u1', name, len(data), sha(data), 'video/mp4', 'reel_source')

    assert reply == {'type': 'file_refused', 'reason': 'bad_name', 'xid': sha(data)}
    assert [f for f in all_files(root) if f.endswith(('.part', '.mp4'))] == []


@pytest.mark.parametrize('name', ['VID_20261012.mp4', 'my clip (1).mov', 'கல்யாணம்.mp4',
                                  '.hidden.mp4', 'C:x.mp4', 'x' * 255, '../x', 'a/b', '', '..',
                                  'tab\there.mp4', 'x' * 256])
def test_the_store_accepts_exactly_the_names_the_contract_accepts(store, name):
    data = blob(10 * KB, 16)
    frame = {'type': 'file_offer', 'name': name, 'size': len(data), 'sha256': sha(data),
             'mime': 'video/mp4', 'purpose': 'reel_source'}

    reply = store.offer('u1', name, len(data), sha(data), 'video/mp4', 'reel_source')

    assert (reply['type'] == 'file_accept') == (C.validate_file_offer(frame) == [])


@pytest.mark.parametrize('mime,purpose', [('image/png', 'reel_source'), ('video/mp4', 'logo'),
                                          ('video/mp4', 'song_audio'), ('text/html', 'logo'),
                                          ('VIDEO/MP4', 'reel_source'), (None, 'logo')])
def test_the_mime_must_fit_the_purpose(store, mime, purpose):
    data = blob(10 * KB, 17)

    reply = store.offer('u1', 'f.bin', len(data), sha(data), mime, purpose)

    assert reply == {'type': 'file_refused', 'reason': 'bad_mime', 'xid': sha(data)}


@pytest.mark.parametrize('field,value', [('size', 0), ('size', -5), ('size', '100'),
                                         ('size', True), ('size', 10.0),
                                         ('sha256', 'abc'), ('sha256', 'A' * 64),
                                         ('sha256', None), ('purpose', 'reel'),
                                         ('purpose', 'payload'), ('purpose', None)])
def test_a_malformed_offer_is_refused(store, root, field, value):
    data = blob(10 * KB, 18)
    offer = {'name': 'a.mp4', 'size': len(data), 'sha256': sha(data), 'mime': 'video/mp4',
             'purpose': 'reel_source'}
    offer[field] = value

    reply = store.offer('u1', offer['name'], offer['size'], offer['sha256'], offer['mime'],
                        offer['purpose'])

    assert reply['type'] == 'file_refused' and reply['reason'] == 'bad_offer'
    assert all_files(root) == []


def test_a_file_over_the_size_cap_is_refused(root, clock):
    store = UserFileStore(root, policy={'max_file': 100 * KB}, disk_free=lambda p: 1 << 40,
                          clock=clock)

    reply = store.offer('u1', 'big.mp4', 100 * KB + 1, 'a' * 64, 'video/mp4', 'reel_source')

    assert reply == {'type': 'file_refused', 'reason': 'too_large', 'xid': 'a' * 64}
    assert store.offer('u1', 'ok.mp4', 100 * KB, 'b' * 64, 'video/mp4',
                       'reel_source')['type'] == 'file_accept'


def test_an_offer_that_would_leave_too_little_disk_is_refused(root, clock):
    free = {'bytes': POLICY['free_margin'] + 500 * KB}
    store = UserFileStore(root, disk_free=lambda p: free['bytes'], clock=clock)

    assert store.offer('u1', 'a.mp4', 600 * KB, 'a' * 64, 'video/mp4',
                       'reel_source') == {'type': 'file_refused', 'reason': 'no_space',
                                          'xid': 'a' * 64}
    assert store.offer('u1', 'b.mp4', 400 * KB, 'b' * 64, 'video/mp4',
                       'reel_source')['type'] == 'file_accept'


def test_a_full_disk_while_writing_refuses_the_chunk_and_keeps_it_missing(store, monkeypatch):
    data = blob(200 * KB, 19)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)

    def disk_full(path, offset, payload):
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(ufs, '_write_at', disk_full)
    reply = put(store, 'u1', data, 1)
    monkeypatch.undo()

    assert reply == {'type': 'file_refused', 'reason': 'no_space', 'xid': sha(data), 'i': 1}
    assert store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                       chunk_size=CS)['missing'] == [[0, 3]]


def _disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, 'No space left on device')


def test_a_full_disk_while_saving_the_sidecar_refuses_the_chunk(store, monkeypatch):
    data = blob(200 * KB, 65)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)

    monkeypatch.setattr(ufs, 'atomic_json_write', _disk_full)
    reply = put(store, 'u1', data, 2)
    monkeypatch.undo()

    assert reply == {'type': 'file_refused', 'reason': 'no_space', 'xid': sha(data), 'i': 2}
    assert store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                       chunk_size=CS)['missing'] == [[0, 3]]


def test_a_full_disk_while_starting_an_upload_refuses_it_and_leaves_nothing(store, root,
                                                                           monkeypatch):
    monkeypatch.setattr(ufs, 'atomic_json_write', _disk_full)

    reply = store.offer('u1', 'a.mp4', 10, 'a' * 64, 'video/mp4', 'reel_source')

    assert reply == {'type': 'file_refused', 'reason': 'no_space', 'xid': 'a' * 64}
    assert all_files(root) == []


def test_an_upload_whose_bytes_vanished_starts_over(root, clock):
    data = blob(200 * KB, 66)
    store = UserFileStore(root, disk_free=lambda p: 1 << 40, clock=clock)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)
    put(store, 'u1', data, 0)
    put(store, 'u1', data, 1)
    [part] = [os.path.join(root, f) for f in all_files(root) if f.endswith('.part')]
    os.remove(part)

    assert store.usage('u1') == len(data)     # a read notices the loss ...
    restarted = UserFileStore(root, disk_free=lambda p: 1 << 40, clock=clock)
    assert restarted.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                           chunk_size=CS)['missing'] == [[0, 3]]   # ... and it sticks
    for i in range(4):
        put(restarted, 'u1', data, i)
    assert restarted.commit('u1', sha(data))['type'] == 'file_done'


def test_the_user_quota_counts_held_files_and_open_uploads(root, clock):
    store = UserFileStore(root, policy={'user_quota': 500 * KB}, disk_free=lambda p: 1 << 40,
                          clock=clock)
    held = blob(200 * KB, 20)
    upload(store, 'u1', held)
    store.offer('u1', 'open.mp4', 200 * KB, 'c' * 64, 'video/mp4', 'reel_source')

    assert store.usage('u1') == 400 * KB
    assert store.offer('u1', 'more.mp4', 101 * KB, 'd' * 64, 'video/mp4',
                       'reel_source')['reason'] == 'quota'
    assert store.offer('u2', 'theirs.mp4', 400 * KB, 'd' * 64, 'video/mp4',
                       'reel_source')['type'] == 'file_accept'      # quotas are per user
    store.abort('u1', 'c' * 64)
    assert store.offer('u1', 'more.mp4', 101 * KB, 'd' * 64, 'video/mp4',
                       'reel_source')['type'] == 'file_accept'


def test_only_two_uploads_run_at_once(store, clock):
    one, two = blob(70 * KB, 21), blob(70 * KB, 22)
    store.offer('u1', '1.mp4', len(one), sha(one), 'video/mp4', 'reel_source', chunk_size=CS)
    store.offer('u1', '2.mp4', len(two), sha(two), 'video/mp4', 'reel_source', chunk_size=CS)

    assert store.offer('u1', '3.mp4', 10, 'e' * 64, 'video/mp4',
                       'reel_source') == {'type': 'file_refused', 'reason': 'busy',
                                          'xid': 'e' * 64}
    # resuming one of the two is not a third upload
    assert store.offer('u1', '1.mp4', len(one), sha(one), 'video/mp4', 'reel_source',
                       chunk_size=CS)['type'] == 'file_accept'
    # another user has their own two
    assert store.offer('u2', '3.mp4', 10, 'e' * 64, 'video/mp4',
                       'reel_source')['type'] == 'file_accept'
    for i in range(2):
        put(store, 'u1', one, i)
    assert store.commit('u1', sha(one))['type'] == 'file_done'
    assert store.offer('u1', '3.mp4', 10, 'e' * 64, 'video/mp4',
                       'reel_source')['type'] == 'file_accept'


def test_an_upload_left_idle_stops_counting_as_running(store, clock):
    for k in range(2):
        store.offer('u1', '%d.mp4' % k, 10, ('%d' % k) * 64, 'video/mp4', 'reel_source')
    assert store.offer('u1', 'x.mp4', 10, 'e' * 64, 'video/mp4', 'reel_source')['reason'] == 'busy'

    clock.now += POLICY['upload_idle_s'] + 1

    assert store.offer('u1', 'x.mp4', 10, 'e' * 64, 'video/mp4',
                       'reel_source')['type'] == 'file_accept'


def test_concurrent_puts_to_one_upload_all_land(store):
    data = blob(64 * CS - 123, 23)      # 64 chunks
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)
    order = list(range(64)) * 2
    random.Random(5).shuffle(order)
    replies, guard = [], threading.Lock()

    def worker(slice_):
        for i in slice_:
            r = put(store, 'u1', data, i)
            with guard:
                replies.append(r['type'])

    threads = [threading.Thread(target=worker, args=(order[k::8],)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert replies.count('file_ack') == 128
    assert store.commit('u1', sha(data))['type'] == 'file_done'
    assert read(store.resolve('u1', sha(data)).path) == data


# ── the phone's capture time (amendment A1) ───────────────────────────────

def test_the_phones_capture_time_rides_the_offer_into_the_index(store):
    data = blob(30 * KB, 24)

    xid = upload(store, 'u1', data, meta={'captured_at': CAPTURED, 'duration_ms': 15000,
                                          'type': 'file_offer', 'name': 'ignored'})

    got = store.resolve('u1', xid)
    assert got.meta == {'captured_at': CAPTURED, 'duration_ms': 15000}
    clip = C.ClipSource.from_stored(got)
    assert (clip.captured_at_ms, clip.duration_ms, clip.committed_at) == \
        (CAPTURED, 15000, got.created_at)
    assert clip.path == got.path and clip.xid == xid


@pytest.mark.parametrize('captured', [0, -5, True, '1760000000000', 1.5e12, None])
def test_a_bad_capture_time_is_dropped_not_refused(store, captured):
    data = blob(30 * KB, 25)

    xid = upload(store, 'u1', data, meta={'captured_at': captured, 'duration_ms': -1})

    assert store.resolve('u1', xid).meta == {}


def test_offers_without_meta_still_upload(store):
    xid = upload(store, 'u1', blob(30 * KB, 26))

    assert store.resolve('u1', xid).meta == {}


# ── users are kept apart ──────────────────────────────────────────────────

def test_another_users_files_are_not_found(store, tmp_path):
    clip = upload(store, 'alice', blob(50 * KB, 27))
    reel = send_reel(store, tmp_path, 'alice').xid
    data = blob(100 * KB, 28)
    store.offer('alice', 'p.mp4', len(data), sha(data), 'video/mp4', 'reel_source',
                chunk_size=CS)

    assert store.resolve('bob', clip) is None and store.resolve('bob', reel) is None
    assert store.get_chunk('bob', reel, 0)['reason'] == 'not_found'
    assert store.mark_fetched('bob', reel) is False
    assert store.bind('bob', [clip], prompt_id='1', request_id='r') == []
    assert store.query_inbox('bob') == [] and store.list_outbox('bob') == []
    assert put(store, 'bob', data, 0)['reason'] == 'unknown_xid'
    assert store.commit('bob', sha(data))['reason'] == 'unknown_xid'
    assert store.take_replied('bob', prompt_id='123') == []
    assert store.usage('bob') == 0
    store.abort('bob', sha(data))
    assert put(store, 'alice', data, 0)['type'] == 'file_ack'


def test_user_and_prompt_ids_compare_as_strings(store):
    xid = upload(store, 10077, blob(40 * KB, 29))

    bound = store.bind('10077', [xid], prompt_id=123, request_id='req-1')

    assert [f.xid for f in bound] == [xid]
    assert bound[0].prompt_id == '123' and bound[0].user_id == '10077'
    assert [f.xid for f in store.query_inbox(10077, prompt_id='123')] == [xid]
    assert [f.xid for f in store.query_inbox('10077', prompt_id=123)] == [xid]


@pytest.mark.parametrize('user', ['', '   ', None, True, 1.5, ['u']])
def test_a_missing_user_is_a_caller_bug(store, user):
    with pytest.raises(ValueError):
        store.offer(user, 'a.mp4', 10, 'a' * 64, 'video/mp4', 'reel_source')


# ── turn binding and consumption ──────────────────────────────────────────

def test_bind_ties_inbound_files_to_a_turn(store):
    a = upload(store, 'u1', blob(40 * KB, 30), name='a.mp4')
    b = upload(store, 'u1', blob(40 * KB, 31), name='b.mp4')

    bound = store.bind('u1', [b, 'f' * 64, a, b, 'junk'], prompt_id='123', request_id='req-1')

    assert [f.xid for f in bound] == [b, a]
    assert all(f.prompt_id == '123' and f.request_id == 'req-1' for f in bound)
    assert [f.xid for f in store.query_inbox('u1', prompt_id='123', request_id='req-1')] == [a, b]
    assert store.query_inbox('u1', prompt_id='123', request_id='req-2') == []
    assert store.query_inbox('u1', prompt_id='999') == []
    store.bind('u1', [a], prompt_id='123', request_id='req-2')       # attached again later
    assert [f.xid for f in store.query_inbox('u1', request_id='req-2')] == [a]


def test_query_inbox_lists_in_upload_order_and_filters(store, clock):
    clip1 = upload(store, 'u1', blob(40 * KB, 32))
    clock.now += 10
    logo = upload(store, 'u1', blob(5 * KB, 33), name='logo.png', mime='image/png',
                  purpose='logo')
    clock.now += 10
    clip2 = upload(store, 'u1', blob(40 * KB, 34))
    clock.now += 10
    song = upload(store, 'u1', blob(20 * KB, 35), name='song.m4a', mime='audio/mp4',
                  purpose='song_audio')

    assert [f.xid for f in store.query_inbox('u1')] == [clip1, logo, clip2, song]
    assert [f.xid for f in store.query_inbox('u1', purpose='reel_source')] == [clip1, clip2]
    assert [f.xid for f in store.query_inbox('u1', purpose='logo')] == [logo]
    assert [f.xid for f in store.query_inbox('u1', max_age_s=15)] == [clip2, song]
    assert [os.path.splitext(f.path)[1] for f in store.query_inbox('u1')] == \
        ['.mp4', '.png', '.mp4', '.m4a']


def test_consumed_files_leave_the_default_query(store, clock):
    a = upload(store, 'u1', blob(40 * KB, 36))
    b = upload(store, 'u1', blob(40 * KB, 37))
    clock.now += 5

    assert store.mark_consumed('u1', [a, 'f' * 64]) is None

    assert [f.xid for f in store.query_inbox('u1')] == [b]
    everything = store.query_inbox('u1', unconsumed_only=False)
    assert [f.xid for f in everything] == [a, b]
    assert everything[0].consumed_at == clock.now


def test_binding_a_consumed_file_again_makes_it_usable(store):
    a = upload(store, 'u1', blob(40 * KB, 38))
    store.mark_consumed('u1', [a])

    store.bind('u1', [a], prompt_id='5', request_id='r2')

    assert [f.xid for f in store.query_inbox('u1', prompt_id='5')] == [a]


def test_a_second_store_sees_the_first_ones_changes(root, clock):
    first = UserFileStore(root, disk_free=lambda p: 1 << 40, clock=clock)
    second = UserFileStore(root, disk_free=lambda p: 1 << 40, clock=clock)
    xid = upload(first, 'u1', blob(40 * KB, 39))

    first.bind('u1', [xid], prompt_id='7', request_id='r')

    assert [f.xid for f in second.query_inbox('u1', prompt_id='7')] == [xid]
    second.mark_consumed('u1', [xid])
    assert first.query_inbox('u1') == []


# ── the outbox (desktop -> phone) ─────────────────────────────────────────

def test_offer_to_device_puts_the_reel_in_the_outbox(store, tmp_path, clock):
    data = blob(150 * KB, 40)
    src = reel_file(tmp_path, data)

    got = store.offer_to_device('u1', src, mime='video/mp4', purpose='reel', prompt_id=123,
                                request_id='req-1', meta={'media': media_item()},
                                name='reel_r-1.mp4')

    assert (got.box, got.xid, got.sha256, got.size, got.purpose, got.prompt_id,
            got.request_id, got.replied, got.fetched) == \
        ('out', sha(data), sha(data), len(data), 'reel', '123', 'req-1', False, False)
    assert read(got.path) == data and read(src) == data       # the caller's file is untouched
    assert got.path.endswith('.mp4') and got.created_at == clock.now
    media = got.meta['media']
    assert (media['xid'], media['sha256'], media['size']) == (sha(data), sha(data), len(data))
    assert C.validate_media_item(media) == []
    [item] = store.list_outbox('u1')
    assert C.validate_outbox_item(item) == []
    assert (item['xid'], item['name'], item['fetched'], item['meta']['prompt_id']) == \
        (sha(data), 'reel_r-1.mp4', False, '123')


def test_offer_to_device_refuses_what_is_not_a_reel(store, tmp_path):
    src = reel_file(tmp_path, blob(10 * KB, 41))
    with pytest.raises(ValueError):
        store.offer_to_device('u1', src, mime='video/mp4', purpose='reel_source',
                              prompt_id='1', request_id=None, meta={})
    with pytest.raises(ValueError):
        store.offer_to_device('u1', reel_file(tmp_path, b'', 'empty.mp4'), mime='video/mp4',
                              purpose='reel', prompt_id='1', request_id=None, meta={})
    with pytest.raises(OSError):
        store.offer_to_device('u1', str(tmp_path / 'missing.mp4'), mime='video/mp4',
                              purpose='reel', prompt_id='1', request_id=None, meta={})
    assert store.list_outbox('u1') == []


def test_take_replied_hands_each_reel_to_one_reply(store, tmp_path, clock):
    r1 = send_reel(store, tmp_path, prompt_id='123', request_id='req-1', seed=42).xid
    r2 = send_reel(store, tmp_path, prompt_id='123', request_id='req-2', seed=43).xid
    other = send_reel(store, tmp_path, prompt_id='456', request_id='req-1', seed=44).xid

    assert [f.xid for f in store.take_replied('u1', prompt_id=123, request_id='req-1')] == [r1]
    assert store.take_replied('u1', prompt_id='123', request_id='req-1') == []
    assert [f.xid for f in store.take_replied('u1', prompt_id='123')] == [r2]
    assert store.take_replied('u1', prompt_id='123') == []
    assert store.resolve('u1', other).replied is False

    clock.now += 1801
    assert store.take_replied('u1', prompt_id='456') == []
    assert [f.xid for f in store.take_replied('u1', prompt_id='456', max_age_s=3600)] == [other]


def test_get_chunk_serves_the_outbox_in_chunks(store, tmp_path):
    data = blob(150 * KB + 7, 45)
    xid = send_reel(store, tmp_path, data=data).xid

    got = [store.get_chunk('u1', xid, i, chunk_size=CS) for i in range(3)]

    assert [c['type'] for c in got] == ['file_chunk'] * 3
    assert [(c['xid'], c['i'], c['n'], c['last']) for c in got] == \
        [(xid, 0, 3, False), (xid, 1, 3, False), (xid, 2, 3, True)]
    assert all(c['c'] == sha(c['data']) for c in got)
    assert b''.join(c['data'] for c in got) == data
    assert store.get_chunk('u1', xid, 3, chunk_size=CS)['reason'] == 'out_of_range'
    assert store.get_chunk('u1', xid, -1, chunk_size=CS)['reason'] == 'out_of_range'
    whole = store.get_chunk('u1', xid, 0)                  # default 1 MiB chunks
    assert (whole['n'], whole['last'], whole['data']) == (1, True, data)


def test_get_chunk_never_serves_inbound_or_unknown_files(store):
    clip = upload(store, 'u1', blob(40 * KB, 46))

    for xid in (clip, 'f' * 64, '../index', None):
        reply = store.get_chunk('u1', xid, 0)
        assert reply['type'] == 'file_refused' and reply['reason'] == 'not_found'


@pytest.mark.parametrize('chunk_size', [0, 1024, 64 * 1024 * 1024, '65536', 1.5])
def test_a_chunk_size_outside_the_policy_is_a_caller_bug(store, chunk_size):
    with pytest.raises(ValueError):
        store.offer('u1', 'a.mp4', 10, 'a' * 64, 'video/mp4', 'reel_source',
                    chunk_size=chunk_size)
    with pytest.raises(ValueError):
        store.get_chunk('u1', 'a' * 64, 0, chunk_size=chunk_size)


def test_list_outbox_filters_by_prompt_and_by_fetched(store, tmp_path):
    a = send_reel(store, tmp_path, prompt_id='123', seed=47).xid
    b = send_reel(store, tmp_path, prompt_id='456', seed=48).xid

    assert [i['xid'] for i in store.list_outbox('u1')] == [a, b]
    assert [i['xid'] for i in store.list_outbox('u1', prompt_id=123)] == [a]

    assert store.mark_fetched('u1', a) is True
    assert store.mark_fetched('u1', a) is True
    assert [i['xid'] for i in store.list_outbox('u1')] == [b]
    listed = store.list_outbox('u1', unfetched_only=False)
    assert [(i['xid'], i['fetched']) for i in listed] == [(a, True), (b, False)]
    assert all(C.validate_outbox_item(i) == [] for i in listed)


def test_mark_fetched_is_false_for_what_is_not_in_the_outbox(store):
    clip = upload(store, 'u1', blob(40 * KB, 49))

    assert store.mark_fetched('u1', clip) is False
    assert store.mark_fetched('u1', 'f' * 64) is False
    assert store.mark_fetched('u1', 'nonsense') is False


def test_the_same_reel_offered_again_is_one_entry_for_the_new_turn(store, tmp_path):
    data = blob(100 * KB, 50)
    first = send_reel(store, tmp_path, data=data, request_id='req-1')
    store.take_replied('u1', prompt_id='123')
    store.mark_fetched('u1', first.xid)

    again = send_reel(store, tmp_path, data=data, request_id='req-2')

    assert again.xid == first.xid
    assert (again.request_id, again.replied, again.fetched) == ('req-2', False, False)
    assert [i['xid'] for i in store.list_outbox('u1')] == [first.xid]


# ── retention: the only GC ────────────────────────────────────────────────

def test_gc_removes_consumed_sources_a_day_after_use(store, clock):
    used = upload(store, 'u1', blob(40 * KB, 51))
    store.mark_consumed('u1', [used])
    path = store.resolve('u1', used).path

    clock.now += POLICY['source_after_use_s'] - 1
    assert store.gc() == 0 and os.path.isfile(path)
    clock.now += 2
    assert store.gc() == 1

    assert not os.path.exists(path)
    assert store.resolve('u1', used) is None
    assert store.usage('u1') == 0


def test_gc_removes_unused_sources_after_three_days(store, clock):
    song = upload(store, 'u1', blob(20 * KB, 52), name='s.mp3', mime='audio/mpeg',
                  purpose='song_audio')
    clip = upload(store, 'u1', blob(20 * KB, 53))

    assert store.gc(now=clock.now + POLICY['source_unused_s'] - 1) == 0
    assert store.gc(now=clock.now + POLICY['source_unused_s'] + 1) == 2
    assert store.resolve('u1', song) is None and store.resolve('u1', clip) is None


def test_gc_keeps_the_logo_until_a_new_one_replaces_it(store, clock):
    old = upload(store, 'u1', blob(5 * KB, 54), name='logo.png', mime='image/png',
                 purpose='logo')
    store.mark_consumed('u1', [old])

    assert store.gc(now=clock.now + 30 * 24 * HOUR) == 0
    assert store.resolve('u1', old) is not None

    clock.now += 60
    new = upload(store, 'u1', blob(5 * KB, 55), name='logo2.webp', mime='image/webp',
                 purpose='logo')
    assert store.gc() == 1
    assert store.resolve('u1', old) is None and store.resolve('u1', new) is not None


def test_gc_removes_fetched_reels_after_two_days_and_any_reel_after_a_week(store, tmp_path,
                                                                         clock):
    fetched = send_reel(store, tmp_path, seed=56).xid
    waiting = send_reel(store, tmp_path, seed=57).xid
    clock.now += 10
    store.mark_fetched('u1', fetched)

    assert store.gc(now=clock.now + POLICY['outbox_after_fetch_s'] - 1) == 0
    assert store.gc(now=clock.now + POLICY['outbox_after_fetch_s'] + 1) == 1
    assert store.resolve('u1', fetched) is None

    assert store.gc(now=T0 + POLICY['outbox_max_s'] - 1) == 0
    assert store.gc(now=T0 + POLICY['outbox_max_s'] + 1) == 1
    assert store.resolve('u1', waiting) is None
    assert store.list_outbox('u1', unfetched_only=False) == []


def test_gc_removes_stale_partial_uploads(store, clock, root):
    data = blob(200 * KB, 58)
    store.offer('u1', 'a.mp4', len(data), sha(data), 'video/mp4', 'reel_source', chunk_size=CS)
    put(store, 'u1', data, 0)

    clock.now += POLICY['part_ttl_s'] - 1
    put(store, 'u1', data, 1)                   # activity keeps it alive
    clock.now += POLICY['part_ttl_s'] - 1
    assert store.gc() == 0
    clock.now += 2
    assert store.gc() == 1

    assert all_files(root) == []
    assert store.usage('u1') == 0
    assert put(store, 'u1', data, 2)['reason'] == 'unknown_xid'


def test_gc_removes_files_the_index_does_not_know_once_they_are_old(store, root, clock):
    xid = upload(store, 'u1', blob(40 * KB, 59))
    inbox = os.path.dirname(store.resolve('u1', xid).path)
    stray = os.path.join(inbox, 'stray.bin')
    with open(stray, 'wb') as f:
        f.write(b'left by a crash')
    os.utime(stray, (T0, T0))

    assert store.gc(now=T0 + POLICY['part_ttl_s'] - 1) == 0
    assert os.path.isfile(stray)
    assert store.gc(now=T0 + POLICY['part_ttl_s'] + 1) == 1
    assert not os.path.exists(stray)
    assert store.resolve('u1', xid) is not None


def test_a_new_offer_runs_the_users_gc_first(root, clock):
    store = UserFileStore(root, policy={'user_quota': 300 * KB}, disk_free=lambda p: 1 << 40,
                          clock=clock)
    old = upload(store, 'u1', blob(200 * KB, 60))
    store.mark_consumed('u1', [old])
    clock.now += POLICY['source_after_use_s'] + 1

    assert store.offer('u1', 'new.mp4', 200 * KB, 'a' * 64, 'video/mp4',
                       'reel_source')['type'] == 'file_accept'
    assert store.resolve('u1', old) is None


def test_gc_on_an_empty_root_is_a_no_op(tmp_path, clock):
    assert UserFileStore(str(tmp_path / 'nothing'), clock=clock).gc() == 0


# ── robustness and placement ──────────────────────────────────────────────

def test_a_corrupt_index_is_set_aside_not_fatal(store, root, caplog):
    xid = upload(store, 'u1', blob(40 * KB, 61))
    user_dir = os.path.dirname(os.path.dirname(store.resolve('u1', xid).path))
    with open(os.path.join(user_dir, 'index.json'), 'w', encoding='utf-8') as f:
        f.write('{"schema": "hart.user_files.v1", "files": ')

    with caplog.at_level('ERROR', logger='core.user_file_store'):
        assert store.query_inbox('u1') == []

    assert any('index' in r.getMessage() for r in caplog.records)
    assert [n for n in os.listdir(user_dir) if n.startswith('index.corrupt')]
    assert upload(store, 'u1', blob(40 * KB, 62))


def test_the_index_is_plain_json_holding_the_capture_time(store):
    xid = upload(store, 'u1', blob(40 * KB, 63), meta={'captured_at': CAPTURED})
    user_dir = os.path.dirname(os.path.dirname(store.resolve('u1', xid).path))

    with open(os.path.join(user_dir, 'index.json'), encoding='utf-8') as f:
        doc = json.load(f)

    assert doc['user_id'] == 'u1'
    assert doc['files']['in'][xid]['meta'] == {'captured_at': CAPTURED}


def test_the_default_store_lives_under_the_data_dir_not_the_served_uploads_dir(
        tmp_path, monkeypatch):
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'data_root'))
    monkeypatch.setattr(pp, '_cached_data_dir', None)
    monkeypatch.setattr(ufs, '_STORE', None)

    store = get_user_file_store()

    assert store is get_user_file_store()
    assert store.root == os.path.join(str(tmp_path / 'data_root'), 'user_files')
    uploads = os.path.abspath(pp.get_uploads_dir())
    assert os.path.commonpath([uploads, os.path.abspath(store.root)]) != uploads


def test_the_policy_carries_the_plan_limits_and_is_read_only():
    assert POLICY['max_file'] == 1 << 30 and POLICY['user_quota'] == 5 << 30
    assert POLICY['max_active_uploads'] == 2 and POLICY['free_margin'] == 2 << 30
    assert POLICY['part_ttl_s'] == 48 * HOUR and POLICY['source_after_use_s'] == 24 * HOUR
    assert POLICY['source_unused_s'] == 72 * HOUR
    assert POLICY['outbox_after_fetch_s'] == 48 * HOUR
    assert POLICY['outbox_max_s'] == 7 * 24 * HOUR
    with pytest.raises(TypeError):
        POLICY['user_quota'] = 1
    with pytest.raises(ValueError):
        UserFileStore('x', policy={'no_such_limit': 1})


def test_the_store_shares_the_contract_facts():
    # core/ cannot import integrations/, so the store keeps its own copy of
    # these; this is what stops the two from drifting.
    assert ufs.INBOUND_PURPOSES == C.INBOUND_PURPOSES
    assert ufs.OUTBOUND_PURPOSE == C.OUTBOUND_PURPOSE
    assert ufs.INBOUND_META_KEYS == C.INBOUND_META_KEYS
    assert {k: set(v) for k, v in ufs.INBOUND_MIMES.items()} == \
        {k: set(v) for k, v in C.INBOUND_MIMES.items()}


def test_the_store_loads_no_transport_and_no_integrations():
    code = ('import sys, core.user_file_store; '
            'print(sorted(m for m in sys.modules '
            'if m.split(".")[0] == "integrations" or m.startswith("core.peer_link")))')
    out = subprocess.run([sys.executable, '-c', code], cwd=_REPO, capture_output=True,
                         text=True, timeout=120)

    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == '[]'


def test_a_stored_file_round_trips_through_json(store):
    got = store.resolve('u1', upload(store, 'u1', blob(10 * KB, 64),
                                     meta={'duration_ms': 1500}))

    assert StoredFile(**json.loads(json.dumps(got.to_dict()))) == got
