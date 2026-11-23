"""One camera per person: while the person's phone streams its camera to this
desktop, the desktop page frees the desktop camera (so another app, a Zoom
call, can open it) and its frames do not take turns with the phone's in that
person's captions; once the phone stops, the page takes its camera back.

Owner 2026-11-13 ~14:15 IST, before the AIC demo: the phone on a gimbal is the
camera, and "zoom call also demands that nunba relinquishes the camera
gracefully".  Before this, the page held the desktop camera for as long as
camera consent was on file, and both cameras' frames landed in one slot for
the account, so the captions alternated between the two scenes.

The real VisionService._ws_handler and the real FrameStore; the socket is a
stand-in that replays what a page sends and records what it is told.

    python -m pytest tests/unit/test_phone_camera_frees_the_laptop_camera.py -q -p no:cacheprovider --noconftest
"""
import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

PAGE = 'ws:127.0.0.1:50000'
PHONE = 'device:3f9a 1c02 77de b4e1'
USER = '10202'


def _jpeg():
    img = np.full((48, 64, 3), 128, dtype=np.uint8)
    ok, enc = cv2.imencode('.jpg', img)
    assert ok
    return enc.tobytes()


class _Page:
    """What a page sends over the socket, in order; what it is told back."""

    def __init__(self, messages, between=None):
        self._messages = list(messages)
        self._between = between or {}
        self.told = []
        self.remote_address = ('127.0.0.1', 50000)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for i, m in enumerate(self._messages):
            if i in self._between:
                self._between[i]()
            yield m

    async def send(self, text):
        self.told.append(text)


@pytest.fixture
def svc(monkeypatch):
    import core.ai_sensing as sensing
    monkeypatch.setattr(sensing, 'allowed', lambda sensor: True)
    # An agent needs the camera: these tests are about the phone.  With no
    # agent needing it the page is paused anyway
    # (test_the_camera_is_on_only_while_an_agent_needs_it.py).
    import core.agent_tools as tools
    monkeypatch.setattr(tools, 'camera_wanted', lambda user_id: True)
    from integrations.vision.frame_store import FrameStore
    from integrations.vision.vision_service import VisionService
    s = VisionService.__new__(VisionService)
    s.store = FrameStore()
    return s


def _run(svc, page):
    asyncio.run(svc._ws_handler(page))


def _page_frames(store):
    return [f for f in store._frames.get(USER, []) if f[2] != PHONE]


def test_while_the_phone_streams_the_page_is_told_to_free_its_camera(svc):
    svc.store.put_frame(USER, _jpeg(), source=PHONE)
    page = _Page([USER, 'video_start', _jpeg(), _jpeg(), _jpeg()])
    _run(svc, page)
    assert page.told == ['video_pause'], 'told once, not per frame'
    assert _page_frames(svc.store) == [], "the page's frames do not reach the captions"


def test_once_the_phone_stops_a_paused_page_takes_its_camera_back(svc, monkeypatch):
    svc.store.put_frame(USER, _jpeg(), source=PHONE)

    def phone_stops():
        svc.store._phone_frame_at[USER] -= svc.store.PHONE_CAMERA_WINDOW_S + 1

    page = _Page([USER, 'video_start', _jpeg(), 'video_paused', 'video_paused'],
                 between={4: phone_stops})
    _run(svc, page)
    assert page.told[0] == 'video_pause'
    assert 'video_resume' in page.told, 'the page is told to take its camera back'


def test_a_paused_page_is_not_told_to_resume_while_the_phone_still_streams(svc):
    svc.store.put_frame(USER, _jpeg(), source=PHONE)
    page = _Page([USER, 'video_start', _jpeg(), 'video_paused', 'video_paused'])
    _run(svc, page)
    assert page.told == ['video_pause']


def test_with_no_phone_the_page_streams_as_before(svc):
    page = _Page([USER, 'video_start', _jpeg(), _jpeg()])
    _run(svc, page)
    assert page.told == []
    assert len(_page_frames(svc.store)) == 2
    assert svc.store.phone_camera_live(USER) is False


def test_the_paused_heartbeat_is_never_taken_as_the_user_id(svc):
    page = _Page(['video_paused', USER, 'video_start', _jpeg()])
    _run(svc, page)
    assert list(svc.store._frames) == [USER]


def test_only_a_phone_makes_the_phone_camera_live(svc):
    svc.store.put_frame(USER, _jpeg(), source=PAGE)
    svc.store.put_frame(USER, _jpeg(), source='http:192.168.0.43')
    assert svc.store.phone_camera_live(USER) is False
    svc.store.put_frame(USER, _jpeg(), source=PHONE)
    assert svc.store.phone_camera_live(USER) is True
    assert svc.store.phone_camera_live(int(USER)) is True, 'an int id is the same person'
    assert svc.store.phone_camera_live('someone-else') is False
