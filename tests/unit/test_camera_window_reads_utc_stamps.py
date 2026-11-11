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


def _nunba(minutes_ago, offset=timedelta(0)):
    """A stamp as Nunba writes it, microseconds and a zone
    ('2026-11-10T23:59:00.146774+00:00'), in the zone ``offset``."""
    when = UTC_NOW - timedelta(minutes=minutes_ago) + timedelta(microseconds=146774)
    return when.astimezone(timezone(offset)).isoformat()


def test_a_stamp_in_another_zone_is_read_in_that_zone(ist):
    """Review of 3622c8310, mutant M3d (every zone read as UTC) survived:
    ten minutes ago written in IST is stale, one minute ago written at
    UTC-4 is fresh."""
    assert CAMERA_START not in uc._format_action_rich(
        [_caption(_nunba(10, timedelta(hours=5, minutes=30)))])
    assert CAMERA_START in uc._format_action_rich(
        [_caption(_nunba(1, timedelta(hours=-4)))])


# ── The other readers of the same rows (review of 3622c8310) ───────────────
#
# hartos.helper's camera tool (get_visual_context, behind reuse's
# get_user_camera_inp_by_mins), screen tool and visual-history search, and
# reuse_recipe's visual poll, parsed created_date with
# strptime("%Y-%m-%dT%H:%M:%S"): every caption Nunba writes raised
# "unconverted data remains: .146774+00:00", so on a desktop the agent's camera
# tool raised and the visual task never ran.


class _Reply:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


@pytest.fixture
def store(monkeypatch):
    from hartos import helper
    rows = []
    monkeypatch.setattr(helper, 'get_visual_context_api',
                        lambda user_id, mins=5: 'http://store/get_visual_bymins')
    monkeypatch.setattr(helper, 'pooled_request', lambda *a, **k: _Reply(rows))
    return helper, rows


def test_the_camera_tool_answers_from_nunba_stamps(ist, store):
    helper, rows = store
    rows += [_caption(_nunba(1)), _caption(_nunba(10))]
    assert helper.get_visual_context('child-1', 5) == [
        'A brown square sits on a beige surface. on 2026-11-11T05:29:00']


def test_the_camera_tool_keeps_the_newest_captions_by_the_one_cap(ist, store):
    """The store answers newest first (Nunba get_visual_bymins); the tool keeps
    CAMERA_WINDOW_MAX_CAPTIONS of them, the same cap as the camera window."""
    helper, rows = store
    rows += [dict(_caption(_nunba(n / 10)), action=f'scene {n}') for n in range(13)]
    got = helper.get_visual_context('child-1', 5)
    assert [line.split(' on ')[0] for line in got] == [
        f'scene {n}' for n in range(uc.CAMERA_WINDOW_MAX_CAPTIONS)]


def test_the_screen_tool_and_the_history_search_read_nunba_stamps(ist, store):
    helper, rows = store
    rows += [_caption(_nunba(1), 'screen'), _caption(_nunba(3), 'screen'),
             _caption(_nunba(1))]
    assert helper.get_screen_context('child-1', 2) == ['a code editor on 2026-11-11T05:29:00']
    assert helper.search_visual_history('child-1', 'brown', 30, 'camera') == [
        '[camera] A brown square sits on a beige surface. at 2026-11-11T05:29:00']


@pytest.fixture
def poll(monkeypatch):
    from hartos import reuse_recipe as rr
    rows, posted = [], []
    monkeypatch.setattr(rr, 'ACTION_API', 'http://store/create_action')
    monkeypatch.setattr(rr, '_visual_poll_paused_until', {})
    monkeypatch.setattr(rr, 'pooled_request', lambda *a, **k: _Reply(rows))
    monkeypatch.setattr(rr, 'pooled_post',
                        lambda url, data=None, headers=None: posted.append(data) or _Reply([]))
    return rr, rows, posted


def _real(minutes_ago, offset=timedelta(0)):
    """The poll reads the real clock: a stamp ``minutes_ago`` from now."""
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return when.astimezone(timezone(offset)).isoformat(timespec='microseconds')


def test_the_visual_poll_runs_on_a_fresh_nunba_caption(poll):
    rr, rows, posted = poll
    rows.append(_caption(_real(1)))
    assert rr.call_visual_task('look at the child', 7, 42) == 'done'
    assert len(posted) == 1


def test_the_visual_poll_reads_a_stamp_in_its_own_zone(poll):
    rr, rows, posted = poll
    rows.append(_caption(_real(10, timedelta(hours=5, minutes=30))))
    assert rr.call_visual_task('look at the child', 7, 42) is None
    assert posted == []


def test_the_visual_poll_still_reads_a_bare_stamp_as_utc(poll):
    rr, rows, posted = poll
    bare = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S')
    rows.append(_caption(bare))
    assert rr.call_visual_task('look at the child', 7, 42) == 'done'
