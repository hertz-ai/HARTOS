"""The one cross-language vector for PeerLink channel 'file':
tests/fixtures/peerlink_file_vector.json.

The phone's DesktopFilesVectorTest (Hevolve_React_Native, WS-11) reads a
copy of the same file and builds and parses these frames with its own code;
both tests pin the file's sha256, so neither side can change it alone
(critique finding 41).  Here the desktop replays every frame the vector
sends through the real door (core.peer_link.file_channel) on a real link
and the real store, and must answer exactly what the vector expects.

The file is one line with no line break: both repos check out with
core.autocrlf=true, which would otherwise rewrite it, and its hash, on
Windows.
"""
import base64
import hashlib
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link import channels, file_channel  # noqa: E402
from core.peer_link.link_manager import reset_link_manager  # noqa: E402
from core.user_file_store import UserFileStore  # noqa: E402
from integrations.reel_studio.contracts import (  # noqa: E402
    validate_file_offer, validate_outbox_item)
from tests.unit.test_peer_link_file_channel import ask, device_link, stop_links  # noqa: E402

VECTOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'fixtures', 'peerlink_file_vector.json')
#: The phone's DesktopFilesVectorTest pins this same value.
PINNED_SHA256 = '0070fc75aa8e1546f8be47367274b52c58762ac4a93070bea2361d2508e79b69'


def _raw() -> bytes:
    with open(VECTOR, 'rb') as f:
        return f.read()


def _vector() -> dict:
    return json.loads(_raw())


def _bytes_of(vector, sha256):
    for item in vector['files'].values():
        if item['sha256'] == sha256:
            return base64.b64decode(item['bytes_b64'], validate=True)
    raise AssertionError('the vector has no file %s' % sha256)


def test_the_vector_is_the_pinned_one_and_one_line():
    raw = _raw()
    assert hashlib.sha256(raw).hexdigest() == PINNED_SHA256
    assert b'\n' not in raw and b'\r' not in raw


def test_the_vector_names_this_desktops_file_channel_and_refusals():
    vector = _vector()
    cfg = channels.get_channel_config('file')
    assert vector['channel'] == {
        'name': 'file', 'id': cfg['id'], 'device': cfg[channels.DEVICE_POLICY_KEY],
        'device_types': list(channels.device_send_types('file')),
        'data_class': cfg['data_class'], 'priority': cfg['priority'],
        'reliable': cfg['reliable']}
    assert vector['refusals'] == list(file_channel.REFUSALS)
    sent = {x['send']['type'] for x in vector['exchanges']}
    assert sent == set(vector['channel']['device_types'])
    reasons = {x['expect']['reason'] for x in vector['exchanges']
               if x['expect']['type'] == 'file_refused'}
    assert reasons <= set(vector['refusals'])


def test_every_file_and_chunk_in_the_vector_is_the_bytes_it_names():
    vector = _vector()
    cs = vector['desktop']['store_policy']['chunk_size']
    for item in vector['files'].values():
        data = base64.b64decode(item['bytes_b64'], validate=True)
        assert hashlib.sha256(data).hexdigest() == item['sha256']
    checked = 0
    for x in vector['exchanges']:
        send, expect = x['send'], x['expect']
        if send['type'] == 'file_put' and expect['type'] == 'file_ack':
            part = base64.b64decode(send['b64'], validate=True)
            assert part == _bytes_of(vector, send['xid'])[send['i'] * cs:(send['i'] + 1) * cs]
            assert send['c'] == hashlib.sha256(part).hexdigest()
            checked += 1
        if expect['type'] == 'file_chunk':
            part = base64.b64decode(expect['b64'], validate=True)
            size = expect['chunk_size']
            assert part == _bytes_of(vector, expect['xid'])[expect['i'] * size:
                                                            (expect['i'] + 1) * size]
            assert expect['c'] == hashlib.sha256(part).hexdigest()
            checked += 1
    assert checked >= 6


def test_its_frames_meet_the_reel_studio_contracts():
    vector = _vector()
    for x in vector['exchanges']:
        if x['expect']['type'] == 'file_accept':
            assert validate_file_offer(x['send']) == [], x['note']
        if x['expect']['type'] == 'file_items':
            for item in x['expect']['items']:
                assert validate_outbox_item(item) == [], x['note']


@pytest.fixture
def vector_desktop(tmp_path, monkeypatch):
    """The desktop the vector describes: its store policy and clock, its
    outbox, and the phone's device link to it."""
    vector = _vector()
    desk = vector['desktop']
    store = UserFileStore(root=str(tmp_path / 'user_files'), policy=desk['store_policy'],
                          disk_free=lambda p: 1 << 40, clock=lambda: desk['clock'])
    monkeypatch.setattr(file_channel, '_store', lambda: store)
    reset_link_manager()
    assert file_channel.install()
    for n, item in enumerate(desk['outbox']):
        src = tmp_path / f'outbox-{n}.mp4'
        src.write_bytes(base64.b64decode(item['bytes_b64'], validate=True))
        store.offer_to_device(desk['user_id'], str(src), mime=item['mime'],
                              purpose=item['purpose'], prompt_id=item['prompt_id'],
                              request_id=item['request_id'], meta=item['meta'],
                              name=item['name'])
    _, ws = device_link(desk['user_id'], requires_e2e=desk['requires_e2e'])
    yield vector, store, ws
    stop_links()
    reset_link_manager()


def test_the_desktop_answers_every_frame_as_the_vector_records(vector_desktop):
    vector, store, ws = vector_desktop
    for n, x in enumerate(vector['exchanges']):
        assert ask(ws, x['send']) == x['expect'], '#%d %s' % (n, x['note'])
    clip = vector['files']['clip']
    offered = vector['exchanges'][0]['send']
    stored = store.resolve(vector['desktop']['user_id'], clip['sha256'])
    assert stored.meta == {'captured_at': offered['captured_at'],
                           'duration_ms': offered['duration_ms']}


def test_the_envelope_is_the_one_a_link_sends_and_answers(vector_desktop):
    vector, _, ws = vector_desktop
    env = vector['envelope']
    ws.incoming.put(json.dumps(env['request']))
    reply = ws.reply(env['request']['id'])
    assert reply is not None
    assert re.fullmatch(r'[0-9a-f]{12}', reply.pop('id'))
    assert reply == env['reply']
