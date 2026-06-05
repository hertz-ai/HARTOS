"""Zoom + Google-Meet event sources (omni-channel bridge #64).

The pure parse_*() mappers turn a documented Zoom/Google API response into
ingest_event() field dicts; the ingest_*() wrappers feed the canonical
ingest_event().  These are behavioural tests of the real functions: feed a
realistic (documented-shape) response, assert the mapped fields; for the ingest
wrapper, mock the ingest_event boundary (the DB write) and assert it is called
with source='zoom'/'gmeet' and the right fields.  No grep tests.

The OAuth-bearer fetch wrappers are creds-gated (a token is the caller's, like
every channel adapter); only their no-token inactivity is asserted here — live
API validation happens once Zoom/Google OAuth credentials are configured.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social import events


# ── Documented-shape fixtures ───────────────────────────────────────────────
ZOOM = {
    "meetings": [
        {"id": 84753094043, "topic": "HART weekly sync", "agenda": "roadmap",
         "type": 2, "start_time": "2026-06-10T15:00:00Z", "duration": 45,
         "timezone": "UTC", "join_url": "https://zoom.us/j/84753094043"},
        {"id": 99, "topic": "No-duration meeting",
         "start_time": "2026-06-11T09:00:00Z", "join_url": "https://zoom.us/j/99"},
    ]
}

GCAL = {
    "items": [
        {"id": "evt1", "summary": "Design review", "description": "ui",
         "location": "Room A",
         "start": {"dateTime": "2026-06-12T10:00:00Z"},
         "end": {"dateTime": "2026-06-12T11:00:00Z"},
         "hangoutLink": "https://meet.google.com/abc-defg-hij"},
        {"id": "evt2", "summary": "Conf-data meeting",
         "start": {"dateTime": "2026-06-13T14:00:00Z"},
         "end": {"dateTime": "2026-06-13T15:00:00Z"},
         "conferenceData": {"entryPoints": [
             {"entryPointType": "phone", "uri": "tel:+1-555"},
             {"entryPointType": "video", "uri": "https://meet.google.com/xyz"}]}},
        {"id": "evt3", "summary": "Plain calendar block (no meet)",
         "start": {"dateTime": "2026-06-14T08:00:00Z"},
         "end": {"dateTime": "2026-06-14T09:00:00Z"}},
    ]
}


# ── _parse_iso_dt ───────────────────────────────────────────────────────────
def test_parse_iso_dt_handles_documented_forms():
    assert events._parse_iso_dt("2026-06-10T15:00:00Z") == datetime(2026, 6, 10, 15, 0, 0)
    assert events._parse_iso_dt("2026-06-10") == datetime(2026, 6, 10, 0, 0, 0)
    # offset form -> naive (tz dropped), not a crash
    assert events._parse_iso_dt("2026-06-10T15:00:00+05:30") == datetime(2026, 6, 10, 15, 0, 0)
    assert events._parse_iso_dt("") is None
    assert events._parse_iso_dt(None) is None
    assert events._parse_iso_dt("not-a-date") is None


# ── Zoom ────────────────────────────────────────────────────────────────────
def test_parse_zoom_maps_fields_and_computes_end_from_duration():
    rows = events.parse_zoom_meetings(ZOOM)
    assert len(rows) == 2
    a = rows[0]
    assert a['source_event_id'] == '84753094043'   # coerced to str (PK is String)
    assert a['title'] == 'HART weekly sync'
    assert a['description'] == 'roadmap'
    assert a['url'] == 'https://zoom.us/j/84753094043'
    assert a['start_time'] == datetime(2026, 6, 10, 15, 0, 0)
    assert a['end_time'] == datetime(2026, 6, 10, 15, 45, 0)   # start + 45 min


def test_parse_zoom_tolerates_missing_duration_and_agenda():
    rows = events.parse_zoom_meetings(ZOOM)
    b = rows[1]
    assert b['source_event_id'] == '99'
    assert b['end_time'] is None        # no duration -> no end
    assert b['description'] == ''       # missing agenda -> empty, not KeyError


def test_parse_zoom_accepts_bare_list_and_empty():
    assert events.parse_zoom_meetings({"meetings": []}) == []
    assert events.parse_zoom_meetings([]) == []
    assert events.parse_zoom_meetings(None) == []


def test_ingest_zoom_calls_ingest_event_with_source(monkeypatch):
    calls = []
    monkeypatch.setattr(events, 'ingest_event',
                        lambda **kw: (calls.append(kw) or {'id': 'row', **kw}))
    rows = events.ingest_zoom_meetings(ZOOM, community_id='c1', created_by='u1')
    assert len(rows) == 2 and len(calls) == 2
    assert all(c['source'] == 'zoom' for c in calls)
    assert all(c['community_id'] == 'c1' and c['created_by'] == 'u1' for c in calls)
    assert calls[0]['title'] == 'HART weekly sync'


# ── Google Meet ─────────────────────────────────────────────────────────────
def test_parse_gmeet_surfaces_only_meet_linked_events():
    rows = events.parse_gmeet_events(GCAL)
    # evt3 has no Meet link -> skipped
    assert len(rows) == 2
    assert [r['source_event_id'] for r in rows] == ['evt1', 'evt2']


def test_parse_gmeet_prefers_hangoutlink_then_video_entrypoint():
    rows = events.parse_gmeet_events(GCAL)
    assert rows[0]['url'] == 'https://meet.google.com/abc-defg-hij'   # hangoutLink
    # entryPoints: phone is ignored, the video uri is taken
    assert rows[1]['url'] == 'https://meet.google.com/xyz'
    assert rows[0]['location'] == 'Room A'
    assert rows[0]['start_time'] == datetime(2026, 6, 12, 10, 0, 0)


def test_ingest_gmeet_calls_ingest_event_with_source(monkeypatch):
    calls = []
    monkeypatch.setattr(events, 'ingest_event',
                        lambda **kw: (calls.append(kw) or {'id': 'row', **kw}))
    rows = events.ingest_gmeet_events(GCAL)
    assert len(rows) == 2 and len(calls) == 2
    assert all(c['source'] == 'gmeet' for c in calls)


# ── creds-gated fetch wrappers: inactive without a token ────────────────────
def test_fetch_wrappers_inactive_without_token():
    assert events.fetch_and_ingest_zoom('') == []
    assert events.fetch_and_ingest_zoom(None) == []
    assert events.fetch_and_ingest_gmeet('') == []
    assert events.fetch_and_ingest_gmeet(None) == []
