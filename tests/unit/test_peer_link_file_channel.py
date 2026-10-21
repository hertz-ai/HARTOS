"""A person's phone moves files to and from its desktop on PeerLink channel
'file' (0x0C; owner ruling U3, option A): core.peer_link.file_channel, the
door, in front of core.user_file_store, the one store.

Driven through a real PeerLink receive loop (the device channel policy,
request threading, the 8-request bound, replies) and a real store on a temp
root.  Only the store's disk-free probe is stubbed, so a nearly full test
drive does not refuse uploads.
"""
import base64
import errno
import hashlib
import json
import os
import queue
import sys
import threading
import time
from typing import Optional

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core import user_file_store as ufs  # noqa: E402
from core.peer_link import channels  # noqa: E402
from core.peer_link import file_channel  # noqa: E402
from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.link_manager import get_link_manager, reset_link_manager  # noqa: E402
from core.user_file_store import UserFileStore  # noqa: E402

#: How long an answer may take: generous, as a write and its fsync on a
#: nearly full test drive can be slow; a missing answer still fails.
WAIT_S = 15
QUIET_S = 0.6
USER = '40021'
OTHER = '50555'
CAPTURED = 1760264100000
SEVEN = ('file_offer', 'file_put', 'file_commit', 'file_abort', 'file_get', 'file_list',
         'file_fetched')


# ── the harness: a device link whose socket is two queues ────────────────────

class DeviceSocket:
    """The socket a link reads and writes: frames in from a queue, every frame
    the desktop sends recorded, replies kept by the request id they answer."""

    def __init__(self):
        self.incoming = queue.Queue()
        self.out = queue.Queue()
        self.replies = {}

    # the duck type link.py speaks
    def recv(self, timeout=None):
        # Blocks like a real socket (up to 1 s here, so a stopped link's loop
        # ends soon): link.py calls a link dead after 100 receives that come
        # back empty at once, so a socket that returned every 50 ms would be
        # "dead" after 5 s of waiting on one slow write.
        try:
            return self.incoming.get(timeout=min(timeout or 1.0, 1.0))
        except queue.Empty:
            raise TimeoutError('idle')

    def send(self, data):
        self.out.put(json.loads(data.decode('utf-8') if isinstance(data, bytes) else data))

    def close(self):
        pass

    # the phone's side
    def request(self, d, *, ch='file', rq=True, rid=None) -> str:
        rid = rid or os.urandom(6).hex()
        env = {'ch': ch, 'id': rid, 'd': d}
        if rq:
            env['rq'] = 1
        self.incoming.put(json.dumps(env))
        return rid

    def reply(self, rid, wait=WAIT_S) -> Optional[dict]:
        """The reply envelope to request ``rid``, or None after ``wait``."""
        deadline = time.monotonic() + wait
        while rid not in self.replies:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                env = self.out.get(timeout=min(left, 0.1))
            except queue.Empty:
                continue
            if env.get('re'):
                self.replies[env['re']] = env
        return self.replies.pop(rid)


_LIVE = []


def device_link(user=USER, *, peer_id=None, requires_e2e=False, kind='device'):
    """A live link of ``kind`` (a phone's, by default) with its receive loop
    running over a DeviceSocket, registered in the link manager."""
    link = PeerLink(peer_id or f'{kind}-{user}-{len(_LIVE)}',
                    'relay:conv-1' if requires_e2e else '192.168.0.164:41234',
                    TrustLevel.SAME_USER)
    link.kind = kind
    link.user_id = user if kind == 'device' else ''
    link.requires_e2e = requires_e2e
    ws = DeviceSocket()
    link._ws = ws
    link._state = LinkState.CONNECTED
    mgr = get_link_manager()
    mgr._apply_channel_handlers(link)
    mgr._links[link.peer_id] = link
    threading.Thread(target=link._receive_loop, daemon=True).start()
    _LIVE.append(link)
    return link, ws


def ask(ws, d, wait=WAIT_S, **kw) -> Optional[dict]:
    """Send ``d`` as a request on 'file'; its reply's d, or None."""
    env = ws.reply(ws.request(d, **kw), wait)
    return None if env is None else env['d']


def stop_links():
    while _LIVE:
        _LIVE.pop()._state = LinkState.DISCONNECTED


def _big_disk(path):
    return 1 << 40


def make_store(tmp_path, name='user_files', **kw):
    kw.setdefault('disk_free', _big_disk)
    return UserFileStore(root=str(tmp_path / name), **kw)


@pytest.fixture
def store(tmp_path):
    # 64 KiB chunks (the store's minimum) keep these files small.
    return make_store(tmp_path, policy={'chunk_size': 64 << 10})


@pytest.fixture(autouse=True)
def _door(monkeypatch, store):
    reset_link_manager()
    monkeypatch.setattr(file_channel, '_store', lambda: store)
    assert file_channel.install()
    yield
    stop_links()
    reset_link_manager()


# ── files ────────────────────────────────────────────────────────────────

def blob(size, seed=1):
    return bytes((k * 131 + seed * 7 + (k >> 8)) & 0xFF for k in range(size))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def offer(data, name='VID_20261012_101500.mp4', mime='video/mp4', purpose='reel_source', **extra):
    frame = {'type': 'file_offer', 'name': name, 'size': len(data), 'sha256': sha(data),
             'mime': mime, 'purpose': purpose}
    frame.update(extra)
    return frame


def put(data, i, cs, **over):
    part = data[i * cs:(i + 1) * cs]
    frame = {'type': 'file_put', 'xid': sha(data), 'i': i,
             'b64': base64.b64encode(part).decode('ascii'), 'c': sha(part)}
    frame.update(over)
    return frame


def upload(ws, data, **offer_extra):
    """Offer, put every missing chunk, commit: the answers in order."""
    accepted = ask(ws, offer(data, **offer_extra))
    assert accepted['type'] == 'file_accept', accepted
    cs = accepted['chunk_size']
    for first, last in accepted['missing']:
        for i in range(first, last + 1):
            assert ask(ws, put(data, i, cs)) == {'type': 'file_ack', 'xid': sha(data), 'i': i}
    return accepted, ask(ws, {'type': 'file_commit', 'xid': sha(data)})


def reel_in_outbox(store, tmp_path, data, user=USER, prompt_id='7001', request_id='req-7001-0001'):
    src = tmp_path / f'reel-{sha(data)[:8]}.mp4'
    src.write_bytes(data)
    return store.offer_to_device(user, str(src), mime='video/mp4', purpose='reel',
                                 prompt_id=prompt_id, request_id=request_id,
                                 name='reel_3f9c2a10.mp4')


def download(ws, xid):
    first = ask(ws, {'type': 'file_get', 'xid': xid, 'i': 0})
    assert first['type'] == 'file_chunk', first
    chunks = [first]
    for i in range(1, first['n']):
        chunks.append(ask(ws, {'type': 'file_get', 'xid': xid, 'i': i}))
    return chunks


# ── the channel ──────────────────────────────────────────────────────────

def test_file_is_channel_0x0c_open_to_a_phone_for_its_seven_requests_only():
    cfg = channels.get_channel_config('file')
    assert cfg['id'] == 0x0C
    assert channels.CHANNEL_NAMES[0x0C] == 'file'
    assert channels.is_private_channel('file')
    assert (cfg['priority'], cfg['reliable']) == (2, True)
    assert channels.device_send_types('file') == SEVEN
    for kind in SEVEN:
        assert channels.device_may_send('file', {'type': kind}), kind
    for frame in ({'type': 'file_chunk'}, {'type': 'tunnel_open'}, {'type': 'api_request'},
                  {}, None, b'\x00binary'):
        assert not channels.device_may_send('file', frame), frame
    # nothing on 'file' is sent to a phone unasked: replies need no 'out'
    assert not channels.device_may_receive('file')
    assert channels.device_unasked_types('file') == ()


def test_the_handshake_names_every_file_request_this_desktop_answers():
    caps = PeerLink._get_local_capabilities()
    assert set(SEVEN) <= set(caps['device_requests'])


# ── uploads ───────────────────────────────────────────────────────────────

def test_a_phone_uploads_a_clip_and_its_capture_time_reaches_the_store(store):
    from integrations.reel_studio.contracts import ClipSource, validate_file_offer
    _, ws = device_link()
    data = blob(150_000)
    frame = offer(data, captured_at=CAPTURED, duration_ms=15000)
    assert validate_file_offer(frame) == []
    accepted, done = upload(ws, data, captured_at=CAPTURED, duration_ms=15000,
                            user_id=OTHER, path='C:/Windows/x')
    assert accepted == {'type': 'file_accept', 'xid': sha(data), 'chunk_size': 64 << 10,
                        'n': 3, 'missing': [[0, 2]], 'max_inflight': 3}
    assert done == {'type': 'file_done', 'xid': sha(data)}
    stored = store.resolve(USER, sha(data))
    with open(stored.path, 'rb') as f:
        assert f.read() == data
    # only the capture facts of the frame are kept; a user it names is not the uploader
    assert stored.meta == {'captured_at': CAPTURED, 'duration_ms': 15000}
    assert stored.purpose == 'reel_source' and stored.name == frame['name']
    assert store.resolve(OTHER, sha(data)) is None
    assert ClipSource.from_stored(stored).captured_at_ms == CAPTURED


def test_a_capture_time_that_is_not_epoch_ms_is_dropped_not_refused(store):
    _, ws = device_link()
    data = blob(1000)
    _, done = upload(ws, data, captured_at=0, duration_ms='15s')
    assert done['type'] == 'file_done'
    assert store.resolve(USER, sha(data)).meta == {}


def test_chunks_out_of_order_and_twice_make_one_whole_file(store):
    _, ws = device_link()
    data = blob(200_000, seed=2)
    cs = ask(ws, offer(data))['chunk_size']
    for i in (3, 1, 1, 0, 2, 3):
        assert ask(ws, put(data, i, cs))['type'] == 'file_ack'
    assert ask(ws, {'type': 'file_commit', 'xid': sha(data)})['type'] == 'file_done'
    with open(store.resolve(USER, sha(data)).path, 'rb') as f:
        assert f.read() == data


def test_a_chunk_with_a_wrong_hash_or_bad_base64_is_refused_and_stays_missing():
    _, ws = device_link()
    data = blob(150_000, seed=3)
    cs = ask(ws, offer(data))['chunk_size']
    xid = sha(data)
    assert ask(ws, put(data, 0, cs))['type'] == 'file_ack'
    assert ask(ws, put(data, 2, cs))['type'] == 'file_ack'
    assert ask(ws, put(data, 1, cs, c=sha(b'other'))) == {
        'type': 'file_refused', 'reason': 'chunk_corrupt', 'xid': xid, 'i': 1}
    assert ask(ws, put(data, 1, cs, b64='@@ not base64 @@')) == {
        'type': 'file_refused', 'reason': 'chunk_corrupt', 'xid': xid, 'i': 1}
    assert ask(ws, {'type': 'file_commit', 'xid': xid}) == {
        'type': 'file_corrupt', 'xid': xid, 'missing': [[1, 1]]}


def test_a_phone_that_redials_resumes_with_only_the_missing_chunks():
    old, ws = device_link()
    data = blob(150_000, seed=4)
    cs = ask(ws, offer(data))['chunk_size']
    assert ask(ws, put(data, 1, cs))['type'] == 'file_ack'
    old._state = LinkState.DISCONNECTED
    _, ws2 = device_link()                       # the same person, a new link
    again = ask(ws2, offer(data))
    assert again['missing'] == [[0, 0], [2, 2]]
    for i in (0, 2):
        assert ask(ws2, put(data, i, cs))['type'] == 'file_ack'
    assert ask(ws2, {'type': 'file_commit', 'xid': sha(data)})['type'] == 'file_done'


def test_a_relayed_link_is_given_smaller_chunks_than_a_direct_one(tmp_path, monkeypatch):
    big = make_store(tmp_path, 'default_policy')        # 1 MiB chunks by default
    monkeypatch.setattr(file_channel, '_store', lambda: big)
    data = blob(3 << 20, seed=5)
    _, lan = device_link()
    _, relay = device_link(OTHER, requires_e2e=True)
    direct = ask(lan, offer(data))
    relayed = ask(relay, offer(data))
    assert (direct['chunk_size'], direct['n']) == (1 << 20, 3)
    assert (relayed['chunk_size'], relayed['n']) == (256 << 10, 12)


def test_moving_from_the_lan_to_the_relay_keeps_the_bytes_already_sent(tmp_path, monkeypatch):
    big = make_store(tmp_path, 'default_policy')
    monkeypatch.setattr(file_channel, '_store', lambda: big)
    data = blob(3 << 20, seed=6)
    lan_link, lan = device_link()
    assert ask(lan, put(data, 0, ask(lan, offer(data))['chunk_size']))['type'] == 'file_ack'
    lan_link._state = LinkState.DISCONNECTED
    _, relay = device_link(requires_e2e=True)            # she left the house
    resumed = ask(relay, offer(data))
    assert resumed['chunk_size'] == 256 << 10
    assert resumed['missing'] == [[4, 11]]                # the first MiB is kept
    for i in range(4, 12):
        assert ask(relay, put(data, i, 256 << 10))['type'] == 'file_ack'
    assert ask(relay, {'type': 'file_commit', 'xid': sha(data)})['type'] == 'file_done'
    with open(big.resolve(USER, sha(data)).path, 'rb') as f:
        assert f.read() == data


def test_abort_drops_the_upload(store):
    _, ws = device_link()
    data = blob(100_000, seed=7)
    cs = ask(ws, offer(data))['chunk_size']
    assert ask(ws, put(data, 0, cs))['type'] == 'file_ack'
    assert store.usage(USER) == len(data)
    assert ask(ws, {'type': 'file_abort', 'xid': sha(data)}) == {
        'type': 'file_aborted', 'xid': sha(data)}
    assert store.usage(USER) == 0
    assert ask(ws, {'type': 'file_commit', 'xid': sha(data)})['reason'] == 'unknown_xid'


# ── downloads ─────────────────────────────────────────────────────────────

def test_a_phone_downloads_its_reel_verifies_it_and_says_so(store, tmp_path):
    from integrations.reel_studio.contracts import validate_outbox_item
    data = blob(150_000, seed=8)
    reel = reel_in_outbox(store, tmp_path, data)
    _, ws = device_link()
    listed = ask(ws, {'type': 'file_list'})
    assert listed['type'] == 'file_items'
    assert [i['xid'] for i in listed['items']] == [reel.xid]
    assert validate_outbox_item(listed['items'][0]) == []
    chunks = download(ws, reel.xid)
    assert [c['last'] for c in chunks] == [False, False, True]
    assert {c['chunk_size'] for c in chunks} == {64 << 10}
    got = b''
    for c in chunks:
        part = base64.b64decode(c['b64'], validate=True)
        assert c['c'] == sha(part)
        assert len(got) == c['i'] * c['chunk_size']
        got += part
    assert got == data and sha(got) == reel.xid
    assert ask(ws, {'type': 'file_fetched', 'xid': reel.xid}) == {
        'type': 'file_ack', 'xid': reel.xid}
    assert ask(ws, {'type': 'file_list'}) == {'type': 'file_items', 'items': []}
    assert store.resolve(USER, reel.xid).fetched is True


def test_downloads_use_one_chunk_size_on_every_link(tmp_path, monkeypatch):
    """The phone resumes a download from its own byte count: chunk i must be
    the same bytes whichever link it asks on, so the size does not follow the
    link the way an upload's does."""
    big = make_store(tmp_path, 'default_policy')
    monkeypatch.setattr(file_channel, '_store', lambda: big)
    reel = reel_in_outbox(big, tmp_path, blob(700_000, seed=9))
    _, lan = device_link()
    _, relay = device_link(requires_e2e=True)
    a = ask(lan, {'type': 'file_get', 'xid': reel.xid, 'i': 1})
    b = ask(relay, {'type': 'file_get', 'xid': reel.xid, 'i': 1})
    assert a == b
    assert (a['chunk_size'], a['n']) == (256 << 10, 3)


def test_file_list_names_one_agents_reels_and_refuses_a_malformed_agent(store, tmp_path):
    first = reel_in_outbox(store, tmp_path, blob(1000, seed=10), prompt_id='7001')
    second = reel_in_outbox(store, tmp_path, blob(1000, seed=11), prompt_id='8002')
    _, ws = device_link()
    assert [i['xid'] for i in ask(ws, {'type': 'file_list'})['items']] == [first.xid, second.xid]
    assert [i['xid'] for i in ask(ws, {'type': 'file_list', 'prompt_id': '7001'})['items']] == [
        first.xid]
    assert [i['xid'] for i in ask(ws, {'type': 'file_list', 'prompt_id': 8002})['items']] == [
        second.xid]
    for bad in (True, [7001], {'id': 1}, 1.5):
        assert ask(ws, {'type': 'file_list', 'prompt_id': bad}) == {
            'type': 'file_refused', 'reason': 'bad_request'}, bad


def test_an_uploaded_file_is_never_served_back(store):
    _, ws = device_link()
    data = blob(1000, seed=12)
    assert upload(ws, data)[1]['type'] == 'file_done'
    assert ask(ws, {'type': 'file_get', 'xid': sha(data), 'i': 0}) == {
        'type': 'file_refused', 'reason': 'not_found', 'xid': sha(data), 'i': 0}


def test_another_persons_phone_reaches_none_of_these_files(store, tmp_path):
    _, mine = device_link(USER)
    data = blob(150_000, seed=13)
    cs = ask(mine, offer(data))['chunk_size']
    assert ask(mine, put(data, 0, cs))['type'] == 'file_ack'
    reel = reel_in_outbox(store, tmp_path, blob(2000, seed=14))
    _, theirs = device_link(OTHER)
    assert ask(theirs, {'type': 'file_list'}) == {'type': 'file_items', 'items': []}
    assert ask(theirs, {'type': 'file_get', 'xid': reel.xid, 'i': 0})['reason'] == 'not_found'
    assert ask(theirs, {'type': 'file_fetched', 'xid': reel.xid}) == {
        'type': 'file_refused', 'reason': 'not_found'}
    assert ask(theirs, put(data, 1, cs))['reason'] == 'unknown_xid'
    assert ask(theirs, {'type': 'file_commit', 'xid': sha(data)})['reason'] == 'unknown_xid'
    assert store.resolve(USER, reel.xid).fetched is False
    assert store.usage(OTHER) == 0


# ── her own devices: nothing here is egress, so nothing is DLP-scanned ────

#: A name the remote-desktop file scan refuses: it carries an email and a
#: phone number (integrations.remote_desktop.security.scan_file_transfer, the
#: DLP engine's check_outbound on the file name).
PII_NAME = 'Bride Priya 415-555-0199 priya.k@example.com.mp4'


@pytest.mark.parametrize('relayed', [False, True], ids=['lan', 'relay'])
def test_her_files_move_between_her_own_devices_unscanned(store, tmp_path, relayed):
    """Her phone and her desktop are one person's devices: a device link of
    its token's user, every file keyed by that user.  security.edge_privacy
    scans only what reaches OTHER people's nodes (owner ruling 2026-09-26;
    a USER_DEVICES destination runs no DLP), so this door scans nothing: a
    clip she named after her client's phone and email uploads, and a reel so
    named comes back, the name unchanged, on the LAN and on the relay.  Fails
    if the door grows the remote-desktop scan, which refuses this name
    (inventory D35, judged clean)."""
    from integrations.remote_desktop.security import scan_file_transfer
    assert scan_file_transfer(PII_NAME)[0] is False     # the scan this door must not run
    _, ws = device_link(requires_e2e=relayed)
    clip = blob(1000, seed=20)
    _, done = upload(ws, clip, name=PII_NAME)
    assert done == {'type': 'file_done', 'xid': sha(clip)}
    assert store.resolve(USER, sha(clip)).name == PII_NAME
    src = tmp_path / 'reel-pii.mp4'
    src.write_bytes(blob(1000, seed=21))
    reel = store.offer_to_device(USER, str(src), mime='video/mp4', purpose='reel',
                                 prompt_id='7001', request_id='req-7001-0002', name=PII_NAME)
    listed = ask(ws, {'type': 'file_list'})
    assert [(i['xid'], i['name']) for i in listed['items']] == [(reel.xid, PII_NAME)]
    got = ask(ws, {'type': 'file_get', 'xid': reel.xid, 'i': 0})
    assert got['type'] == 'file_chunk', got
    assert got['last'] is True
    assert base64.b64decode(got['b64'], validate=True) == src.read_bytes()


# ── who may ask, and how ──────────────────────────────────────────────────

def _spy(link):
    seen = []
    link.on_message('file', lambda ch, data, pid: seen.append(data))
    return seen


def test_another_node_is_not_answered_and_nothing_is_written(store):
    link, ws = device_link(kind='node')
    data = blob(1000, seed=15)
    assert ask(ws, offer(data), wait=QUIET_S) is None
    assert not os.path.exists(store.root) or os.listdir(store.root) == []


def test_a_binary_frame_on_file_reaches_no_handler(store):
    link, ws = device_link()
    seen = _spy(link)
    ws.incoming.put(bytes([0x0C]) + b'\x00\x00\x00\x01' + json.dumps(offer(blob(10))).encode())
    time.sleep(QUIET_S)
    assert seen == [] and ws.out.empty()
    assert store.usage(USER) == 0


def test_a_frame_that_asks_for_nothing_or_names_another_type_is_dropped(store):
    link, ws = device_link()
    seen = _spy(link)
    assert ask(ws, offer(blob(1000, seed=16)), wait=QUIET_S, rq=False) is None
    assert ask(ws, {'type': 'file_chunk', 'xid': 'ab' * 32, 'i': 0}, wait=QUIET_S) is None
    assert seen == []
    assert store.usage(USER) == 0


def test_past_eight_requests_in_flight_a_phone_hears_busy(store, monkeypatch):
    from core.peer_link.link import _MAX_CONCURRENT_REQUESTS
    _, ws = device_link()
    data = blob(1000, seed=17)
    cs = ask(ws, offer(data))['chunk_size']
    gate, entered = threading.Event(), []
    real_put = store.put_chunk

    def held(*a, **kw):
        entered.append(1)
        gate.wait(WAIT_S * 2)
        return real_put(*a, **kw)
    monkeypatch.setattr(store, 'put_chunk', held)
    try:
        waiting = [ws.request(put(data, 0, cs)) for _ in range(_MAX_CONCURRENT_REQUESTS)]
        deadline = time.monotonic() + WAIT_S
        while len(entered) < _MAX_CONCURRENT_REQUESTS and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(entered) == _MAX_CONCURRENT_REQUESTS
        assert ask(ws, put(data, 0, cs), wait=WAIT_S) == {'type': 'busy'}
    finally:
        gate.set()
    for rid in waiting:
        assert ws.reply(rid)['d'] == {'type': 'file_ack', 'xid': sha(data), 'i': 0}


# ── failures the store raises are answered, not left hanging ─────────────

def test_a_full_disk_while_committing_is_answered_no_space_and_a_retry_completes(store, monkeypatch):
    _, ws = device_link()
    data = blob(100_000, seed=18)
    cs = ask(ws, offer(data))['chunk_size']
    for i in range(2):
        assert ask(ws, put(data, i, cs))['type'] == 'file_ack'
    real_write = ufs.atomic_json_write

    def disk_full(path, doc, *a, **kw):
        if os.path.basename(path) == 'index.json':
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real_write(path, doc, *a, **kw)
    monkeypatch.setattr(ufs, 'atomic_json_write', disk_full)
    assert ask(ws, {'type': 'file_commit', 'xid': sha(data)}) == {
        'type': 'file_refused', 'reason': 'no_space'}
    monkeypatch.setattr(ufs, 'atomic_json_write', real_write)
    assert ask(ws, {'type': 'file_commit', 'xid': sha(data)}) == {
        'type': 'file_done', 'xid': sha(data)}


def test_an_unexpected_failure_on_the_desktop_is_answered_error(store, monkeypatch, caplog):
    def broken(*a, **kw):
        raise RuntimeError('store exploded')
    monkeypatch.setattr(store, 'get_chunk', broken)
    _, ws = device_link()
    with caplog.at_level('WARNING', logger='hevolve.peer_link'):
        assert ask(ws, {'type': 'file_get', 'xid': 'ab' * 32, 'i': 4}) == {
            'type': 'file_refused', 'reason': 'error', 'i': 4}
    assert any('file_get' in r.getMessage() for r in caplog.records)


def test_every_refusal_the_door_can_send_is_named():
    assert set(ufs.REFUSALS) < set(file_channel.REFUSALS)
    assert {'bad_request', 'error'} <= set(file_channel.REFUSALS)
    assert len(set(file_channel.REFUSALS)) == len(file_channel.REFUSALS)


# ── boot ──────────────────────────────────────────────────────────────────

def test_installing_the_door_applies_the_stores_retention(tmp_path, monkeypatch):
    now = [1_760_000_000.0]
    aging = make_store(tmp_path, 'aging', clock=lambda: now[0])
    monkeypatch.setattr(file_channel, '_store', lambda: aging)
    data = blob(1000, seed=19)
    assert aging.offer(USER, 'a.mp4', len(data), sha(data), 'video/mp4',
                       'reel_source')['type'] == 'file_accept'
    assert aging.usage(USER) == len(data)
    now[0] += aging.policy['part_ttl_s'] + 60
    assert file_channel.install()
    assert aging.usage(USER) == 0


@pytest.mark.parametrize('tier', ['flat', 'regional', 'central'])
def test_boot_opens_the_door_on_every_node_but_central(monkeypatch, tier):
    import hartos.hartos_bootstrap as hb
    reset_link_manager()
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: tier)
    hb._install_file_channel()
    answered = set(get_link_manager().device_requests_answered())
    assert (set(SEVEN) <= answered) is (tier != 'central')


def test_the_boot_sequence_runs_the_door_step(monkeypatch):
    from unittest.mock import MagicMock, patch
    from flask import Flask
    import hartos.hartos_bootstrap as hb
    reset_link_manager()
    monkeypatch.setattr(hb, '_BOOTSTRAP_DONE', hb._BOOTSTRAP_DONE)
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'flat')
    others = ('_install_api_gate', '_install_device_verifier', '_install_peer_admission_ask',
              '_install_peer_link_relay', '_init_social_subsystem', '_register_core_blueprints',
              '_run_consumer_hook', '_register_hive_blueprints', '_init_a2a_server',
              '_install_mobile_adapter', '_init_database', '_init_channel_adapters',
              '_init_hevolveai_subprocess', '_init_agent_engine_subsystem',
              '_init_livekit_supervisor', '_init_whatsapp_supervisor',
              '_run_on_bootstrap_complete')
    with patch.multiple(hb, **{name: MagicMock() for name in others}):
        hb._run_bootstrap(Flask('boot'), {})
    assert 'file_offer' in get_link_manager().device_requests_answered()
