"""The camera and screen windows read a caption's UTC stamp as UTC.

MEASURED 2026-11-11 on the owner's desktop (IST, UTC+5:30), build 70 and the
real core.user_context._format_action_rich: a caption stamped one minute ago
by Nunba ('...+00:00') was left out of <Last_5_Minutes_Visual_Context>; the
same caption stamped in local time was kept.  The windows compared the stamp,
its zone dropped, with the naive local clock, so every caption looked 5.5 h
old and no turn on an IST desktop ever saw the camera (the live speech-agent
proof's opening messages grew by the step text only).  West of UTC the same
compare kept every caption ever written.

The clock is pinned here (an IST desktop and a UTC-5 one), so these tests
fail the old compare on any machine, CI in UTC included.

    python -m pytest tests/unit/test_camera_window_reads_utc_stamps.py -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core import user_context as uc  # noqa: E402

UTC_NOW = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)
CAMERA_START = '<Last_5_Minutes_Visual_Context_Start>'
SCREEN_START = '<Last_2_Minutes_Screen_Context_Start>'


def _clock(local_offset):
    """A datetime whose now() is UTC_NOW, seen from a desktop at
    ``local_offset`` from UTC: naive local time without a zone, the zone's
    time with one."""
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return (UTC_NOW + local_offset).replace(tzinfo=None)
            return UTC_NOW.astimezone(tz)
    return _Clock


@pytest.fixture
def ist(monkeypatch):
    monkeypatch.setattr(uc, 'datetime', _clock(timedelta(hours=5, minutes=30)))


@pytest.fixture
def us_east(monkeypatch):
    monkeypatch.setattr(uc, 'datetime', _clock(timedelta(hours=-5)))


def _caption(stamp, kind='camera'):
    if kind == 'camera':
        return {'action': 'A brown square sits on a beige surface.',
                'zeroshot_label': 'Video Reasoning', 'gpt3_label': 'Visual Context',
                'created_date': stamp}
    return {'action': 'a code editor', 'zeroshot_label': 'Screen Reasoning',
            'gpt3_label': 'Screen Context', 'created_date': stamp}


def _utc(minutes_ago):
    return (UTC_NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_a_caption_from_a_minute_ago_is_in_the_window_on_an_ist_desktop(ist):
    assert CAMERA_START in uc._format_action_rich([_caption(_utc(1))])


def test_a_caption_older_than_five_minutes_stays_out_on_an_ist_desktop(ist):
    assert CAMERA_START not in uc._format_action_rich([_caption(_utc(10))])


def test_an_old_caption_stays_out_west_of_utc(us_east):
    assert CAMERA_START not in uc._format_action_rich([_caption(_utc(120))])


def test_a_fresh_caption_is_in_the_window_west_of_utc(us_east):
    assert CAMERA_START in uc._format_action_rich([_caption(_utc(1))])


def test_a_stamp_without_a_zone_is_read_as_local_time_as_before(ist):
    local = (UTC_NOW + timedelta(hours=5, minutes=30) - timedelta(minutes=1)).replace(tzinfo=None)
    assert CAMERA_START in uc._format_action_rich([_caption(local.isoformat())])
    stale = local - timedelta(minutes=10)
    assert CAMERA_START not in uc._format_action_rich([_caption(stale.isoformat())])


def test_the_screen_window_reads_utc_stamps_too(ist):
    assert SCREEN_START in uc._format_action_rich([_caption(_utc(1), 'screen')])
    assert SCREEN_START not in uc._format_action_rich([_caption(_utc(3), 'screen')])
