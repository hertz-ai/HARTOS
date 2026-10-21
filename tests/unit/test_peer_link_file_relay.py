"""A phone's clip crosses the PeerLink relay to its desktop's store, and the
reel comes back, over a router that loses frames.

Off her Wi-Fi the phone reaches the desktop only through central's router
(core.peer_link.relay), which publishes fire-and-forget and drops frames
past its queue.  The file channel (core.peer_link.file_channel) makes every
frame a request with its own answer, so a lost frame is a timeout the phone
retries, and every retry is safe to repeat.  This runs the real handshake
(a device HELLO admitted by a stand-in verifier), the real session crypto,
the real receive loops, the production relay accept path (start_relay_hub)
and the real store, over an in-memory router that drops every Nth sealed
frame and records everything it carried.
"""
import base64
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link import file_channel  # noqa: E402
from core.peer_link import link as link_mod  # noqa: E402
from core.peer_link import relay as relay_mod  # noqa: E402
from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.link_manager import get_link_manager, reset_link_manager  # noqa: E402
from core.peer_link.relay import MAX_FRAME_BYTES, RELAY_ADDRESS_SCHEME, RelayHub  # noqa: E402
from core.user_file_store import UserFileStore  # noqa: E402
from tests.unit.test_peer_link_relay import MemoryRouter  # noqa: E402

USER = '40021'
DESKTOP = 'desktop-relay-inbox'
PHONE_PEER_ID = 'device-relay-phone'
CLIP_SIZE = 20 << 20
REEL_SIZE = 4 << 20
DROP_EVERY = 11
REQUEST_TIMEOUT_S = 1.5
ATTEMPTS = 25
# A 48-byte run at offset 0 (a multiple of 3), so its base64 is the start of
# chunk 0's b64 verbatim: if the router could read a frame, it would see it.
MARKER = b'CLIENT-FACE-FRAME-0001::bride-in-her-own-house..'
CLIP_NAME = 'VID_20261012_101500_bride.mp4'


class LossyRouter(MemoryRouter):
    """Drops every ``every``-th frame after the HELLO / HELLO_ACK pair (the
    sealed ones), as central's router does past its queue.  A dropped frame
    was still carried as far as the router: it is recorded, never delivered."""

    def __init__(self, every):
        super().__init__()
        self.every = every
        self.carried = 0
        self.dropped = 0
        self._count = threading.Lock()

    def deliver(self, topic, envelope):
        if 'b' in envelope:
            with self._count:
                self.carried += 1
                drop = self.carried > 2 and self.carried % self.every == 0
                self.dropped += drop
            if drop:
                with self._lock:
                    self.published.append((topic, json.loads(json.dumps(envelope))))
                return
        super().deliver(topic, envelope)


class PhoneLink(PeerLink):
    """The phone's end of a device link: its HELLO carries its device token
    (PeerLinkHandshake on the phone), signed with the rest of the HELLO."""

    def _ws_send(self, data):
        if self._state == LinkState.HANDSHAKING:
            hello = json.loads(data)
            if hello.get('type') == 'hello':
                from security.node_integrity import sign_json_payload
                hello.pop('signature', None)
                hello['device_token'] = 'the-phones-device-token'
                hello['signature'] = sign_json_payload(hello)
                data = json.dumps(hello, separators=(',', ':')).encode('utf-8')
        super()._ws_send(data)


def _admit_the_phone(token, address):
    """The verifier's 'ok' for the phone (hartos_bootstrap installs the real
    one): the key that signed the HELLO, and its person."""
    from security.node_integrity import get_public_key_hex
    return {'status': 'ok', 'public_key': get_public_key_hex(),
            'payload': {'user_id': USER}, 'peer_id': PHONE_PEER_ID}


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """A desktop on a lossy relay with its file door open, and a phone linked
    to it there.  Yields (router, store, phone link)."""
    router = LossyRouter(DROP_EVERY)
    store = UserFileStore(root=str(tmp_path / 'user_files'), disk_free=lambda p: 1 << 40)
    monkeypatch.setattr(file_channel, '_store', lambda: store)
    monkeypatch.setenv('HEVOLVE_PEER_LINK_RELAY', '1')   # conftest keeps it off
    reset_link_manager()
    relay_mod.stop_relay_hub()
    link_mod.set_device_verifier(_admit_the_phone)
    phone = None
    try:
        assert file_channel.install()
        assert relay_mod.start_relay_hub(DESKTOP, transport=router.transport()) is not None
        phone_hub = RelayHub('phone-relay-inbox', router.transport())
        assert phone_hub.start()
        phone = PhoneLink(peer_id=DESKTOP, address=RELAY_ADDRESS_SCHEME + DESKTOP,
                          trust=TrustLevel.SAME_USER)
        with patch.object(relay_mod, 'get_relay_hub', return_value=phone_hub):
            assert phone.connect()
        deadline = time.monotonic() + 5
        while get_link_manager().get_device_link(PHONE_PEER_ID) is None:
            assert time.monotonic() < deadline, 'the desktop never admitted the phone'
            time.sleep(0.02)
        yield router, store, phone
    finally:
        if phone is not None:
            phone.close()
        relay_mod.stop_relay_hub()
        link_mod.set_device_verifier(None)
        reset_link_manager()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Phone:
    """What DesktopFiles does on the phone: a window of three requests, and
    a lost answer (a timeout) or a busy one asks again."""

    def __init__(self, link):
        self.link = link
        self.retries = 0
        self._lock = threading.Lock()

    def ask(self, frame):
        for _ in range(ATTEMPTS):
            reply = self.link.send('file', frame, wait_response=True, timeout=REQUEST_TIMEOUT_S)
            if reply is not None and reply.get('type') != 'busy' and not (
                    reply.get('type') == 'file_refused' and reply.get('reason') == 'busy'):
                return reply
            with self._lock:
                self.retries += 1
        raise AssertionError(f'{frame["type"]} unanswered after {ATTEMPTS} tries')

    def upload(self, data, **offer_extra):
        xid = sha(data)
        accepted = self.ask(dict({'type': 'file_offer', 'name': CLIP_NAME, 'size': len(data),
                                  'sha256': xid, 'mime': 'video/mp4',
                                  'purpose': 'reel_source'}, **offer_extra))
        assert accepted['type'] == 'file_accept', accepted
        cs = accepted['chunk_size']
        wanted = [i for a, b in accepted['missing'] for i in range(a, b + 1)]

        def put(i):
            part = data[i * cs:(i + 1) * cs]
            return self.ask({'type': 'file_put', 'xid': xid, 'i': i,
                             'b64': base64.b64encode(part).decode('ascii'), 'c': sha(part)})
        with ThreadPoolExecutor(max_workers=accepted['max_inflight']) as window:
            acks = list(window.map(put, wanted))
        assert acks == [{'type': 'file_ack', 'xid': xid, 'i': i} for i in wanted]
        return accepted, self.ask({'type': 'file_commit', 'xid': xid})

    def download(self, xid):
        first = self.ask({'type': 'file_get', 'xid': xid, 'i': 0})
        assert first['type'] == 'file_chunk', first
        with ThreadPoolExecutor(max_workers=3) as window:
            rest = list(window.map(
                lambda i: self.ask({'type': 'file_get', 'xid': xid, 'i': i}),
                range(1, first['n'])))
        out = bytearray()
        for chunk in [first] + rest:
            part = base64.b64decode(chunk['b64'], validate=True)
            assert chunk['c'] == sha(part)
            assert len(out) == chunk['i'] * chunk['chunk_size']
            out += part
        return bytes(out)


def test_a_20_mb_clip_and_its_reel_cross_a_lossy_relay_whole_and_unread(relay, tmp_path):
    router, store, link = relay
    phone = Phone(link)
    rng = random.Random(186)
    clip = MARKER + rng.randbytes(CLIP_SIZE - len(MARKER))

    accepted, done = phone.upload(clip, captured_at=1760264100000, duration_ms=15000)
    assert accepted['chunk_size'] == 256 << 10            # a relayed link's chunks
    assert done == {'type': 'file_done', 'xid': sha(clip)}
    stored = store.resolve(USER, sha(clip))
    with open(stored.path, 'rb') as f:
        assert sha(f.read()) == sha(clip)
    assert stored.meta == {'captured_at': 1760264100000, 'duration_ms': 15000}

    reel_bytes = rng.randbytes(REEL_SIZE)
    src = tmp_path / 'reel.mp4'
    src.write_bytes(reel_bytes)
    reel = store.offer_to_device(USER, str(src), mime='video/mp4', purpose='reel',
                                 prompt_id='7001', request_id='req-7001-0001')
    assert [i['xid'] for i in phone.ask({'type': 'file_list'})['items']] == [reel.xid]
    assert phone.download(reel.xid) == reel_bytes
    assert phone.ask({'type': 'file_fetched', 'xid': reel.xid}) == {
        'type': 'file_ack', 'xid': reel.xid}

    # the router really lost frames, and the phone really asked again
    assert router.dropped > 0 and phone.retries > 0
    # ... and it read nothing but the HELLO and its answer
    frames = router.frames()
    readable = []
    for raw in frames:
        try:
            readable.append(json.loads(raw.decode('utf-8'))['type'])
        except Exception:
            pass
    assert readable == ['hello', 'hello_ack']
    marker_b64 = base64.b64encode(MARKER)
    assert not any(marker_b64 in raw or CLIP_NAME.encode() in raw for raw in frames)
    assert max(len(raw) for raw in frames) < MAX_FRAME_BYTES
