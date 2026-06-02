"""#72 — server-side LiveKit Egress recording of a call room.

The mobile demo capture is the "Teams recording while screen-sharing" model:
the LiveKit server composites the room (camera + the screen-share track the
call already carries) to an mp4 server-side — no external OS screen-grab.
LiveKitService.start_recording / stop_recording drive the egress API.

Mocks only the network entry (LiveKitAPI); the egress REQUEST objects are the
real SDK classes so request construction is genuinely exercised.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def lk():
    try:
        import integrations.social.livekit_service as m
    except Exception as e:
        pytest.skip(f"livekit_service unavailable: {e}")
    if not m._HAS_LIVEKIT_SDK:
        pytest.skip("livekit-api SDK not installed")
    return m


def _fake_livekit_api(capture: dict):
    """Build a fake LiveKitAPI class that records the egress request and
    returns a synthetic EgressInfo, with async egress methods + aclose."""
    class _Egress:
        async def start_room_composite_egress(self, req):
            capture['start_req'] = req
            return types.SimpleNamespace(egress_id='EG_test123', status=2)

        async def stop_egress(self, req):
            capture['stop_req'] = req
            return types.SimpleNamespace(egress_id=req.egress_id, status=4)

    class _FakeAPI:
        def __init__(self, url, key, secret, **kw):
            capture['ctor'] = (url, key, secret)
            self.egress = _Egress()

        async def aclose(self):
            capture['closed'] = True

    return _FakeAPI


def test_api_base_url_converts_ws_schemes(lk):
    assert lk.LiveKitService._api_base_url('wss://h:7880') == 'https://h:7880'
    assert lk.LiveKitService._api_base_url('ws://h:7880') == 'http://h:7880'
    assert lk.LiveKitService._api_base_url('https://h:7880') == 'https://h:7880'


def test_start_recording_no_config_returns_p2p(lk, monkeypatch):
    monkeypatch.setattr(lk, '_resolved_config', lambda: (None, None, None))
    out = lk.LiveKitService.start_recording('call-1')
    assert out['ok'] is False and out.get('mode') == 'p2p_mesh'


def test_start_recording_builds_mp4_room_composite_request(lk, monkeypatch):
    capture = {}
    monkeypatch.setattr(lk, '_resolved_config',
                        lambda: ('wss://lk.test:7880', 'KEY', 'SECRET'))
    monkeypatch.setattr(lk.livekit_api, 'LiveKitAPI', _fake_livekit_api(capture))

    out = lk.LiveKitService.start_recording('call-xyz', layout='grid')

    assert out['ok'] is True
    assert out['egress_id'] == 'EG_test123'
    assert out['room'] == 'call-xyz'
    # API client got the http(s) base URL, not wss
    assert capture['ctor'][0] == 'https://lk.test:7880'
    assert capture['closed'] is True
    # Request: right room, MP4 file output
    req = capture['start_req']
    assert req.room_name == 'call-xyz'
    assert len(req.file_outputs) == 1
    assert req.file_outputs[0].file_type == lk.livekit_api.EncodedFileType.MP4
    assert req.file_outputs[0].filepath.endswith('.mp4')


def test_stop_recording_calls_stop_egress(lk, monkeypatch):
    capture = {}
    monkeypatch.setattr(lk, '_resolved_config',
                        lambda: ('wss://lk.test:7880', 'KEY', 'SECRET'))
    monkeypatch.setattr(lk.livekit_api, 'LiveKitAPI', _fake_livekit_api(capture))

    out = lk.LiveKitService.stop_recording('EG_test123')
    assert out['ok'] is True
    assert capture['stop_req'].egress_id == 'EG_test123'
    assert capture['closed'] is True


def test_start_recording_surfaces_server_error(lk, monkeypatch):
    """A reachable SFU with no egress worker raises — we surface the reason
    (request was well-formed; only the egress service is missing)."""
    monkeypatch.setattr(lk, '_resolved_config',
                        lambda: ('wss://lk.test:7880', 'KEY', 'SECRET'))

    class _BoomAPI:
        def __init__(self, *a, **k):
            self.egress = self

        async def start_room_composite_egress(self, req):
            raise RuntimeError('no egress instances available')

        async def aclose(self):
            pass

    monkeypatch.setattr(lk.livekit_api, 'LiveKitAPI', _BoomAPI)
    out = lk.LiveKitService.start_recording('call-1')
    assert out['ok'] is False
    assert 'no egress' in out['reason']
