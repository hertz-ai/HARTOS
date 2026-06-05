"""Bridge Phase 3a (#64a) — Event model + ingestion, verified against a REAL DB.

parse_ics is pure (stdlib).  ingest_event / ingest_ics are exercised against an
in-memory SQLite with the real Event table — real INSERT + the (source,
source_event_id) dedup UPSERT — not mocks.  This proves the ingestion actually
round-trips, rather than declaring it.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


SAMPLE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:evt-1@hive\r\n"
    "SUMMARY:Hive Demo Day\r\n"
    "DTSTART:20260615T130000Z\r\n"
    "DTEND:20260615T140000Z\r\n"
    "LOCATION:Online\r\n"
    "DESCRIPTION:Come see the\r\n  demos\r\n"   # RFC5545 folded continuation
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:evt-2@hive\r\n"
    "SUMMARY:Contributor Sync\r\n"
    "DTSTART;TZID=Asia/Kolkata:20260620T100000\r\n"   # property parameter
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_parse_ics_handles_folding_and_params():
    from integrations.social.events import parse_ics
    evs = parse_ics(SAMPLE_ICS)
    assert len(evs) == 2
    assert evs[0]['source_event_id'] == 'evt-1@hive'
    assert evs[0]['title'] == 'Hive Demo Day'
    assert evs[0]['start_time'].year == 2026 and evs[0]['start_time'].hour == 13
    assert evs[0]['end_time'].hour == 14
    assert evs[0]['location'] == 'Online'
    assert 'demos' in evs[0]['description'], "folded continuation line must unfold"
    # property parameter (TZID=…) stripped, datetime still parsed
    assert evs[1]['title'] == 'Contributor Sync'
    assert evs[1]['start_time'].hour == 10


@pytest.fixture
def ev_models(monkeypatch):
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import integrations.social.models as M
    except Exception as e:
        pytest.skip(f"social models / sqlalchemy unavailable: {e}")

    engine = create_engine('sqlite:///:memory:')
    M.Event.__table__.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def _fake_db_session():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(M, 'db_session', _fake_db_session)
    return M


def test_ingest_event_creates_then_dedups(ev_models):
    from integrations.social.events import ingest_event
    r1 = ingest_event('ics', 'Demo Day', source_event_id='evt-1', location='Online')
    assert r1 and r1['title'] == 'Demo Day' and r1['source'] == 'ics'

    # Re-ingest the SAME upstream id with a new title → UPDATE, not duplicate.
    r2 = ingest_event('ics', 'Demo Day (updated)', source_event_id='evt-1')
    assert r2['id'] == r1['id'], "same (source, source_event_id) must update the same row"

    with ev_models.db_session() as db:
        assert db.query(ev_models.Event).count() == 1, "dedup must not create a 2nd row"
        assert db.query(ev_models.Event).first().title == 'Demo Day (updated)'


def test_ingest_ics_end_to_end_lands_two_rows(ev_models):
    from integrations.social.events import ingest_ics
    rows = ingest_ics(SAMPLE_ICS)
    assert len(rows) == 2
    titles = {r['title'] for r in rows}
    assert titles == {'Hive Demo Day', 'Contributor Sync'}
    with ev_models.db_session() as db:
        assert db.query(ev_models.Event).count() == 2
    # idempotent: re-ingesting the same feed updates, doesn't duplicate
    ingest_ics(SAMPLE_ICS)
    with ev_models.db_session() as db:
        assert db.query(ev_models.Event).count() == 2, "re-ingest must dedup on UID"


# ── list_upcoming_events: the READ side (without it the table is write-only) ──

def test_list_upcoming_events_future_only_sorted(ev_models):
    from datetime import datetime, timedelta
    from integrations.social.events import ingest_event, list_upcoming_events
    now = datetime(2026, 6, 4, 12, 0, 0)
    ingest_event('manual', 'Past standup', source_event_id='p1',
                 start_time=now - timedelta(hours=2))
    ingest_event('manual', 'Soon: demo', source_event_id='f1',
                 start_time=now + timedelta(hours=1))
    ingest_event('manual', 'Later: sync', source_event_id='f2',
                 start_time=now + timedelta(hours=5))
    ingest_event('manual', 'Undated note', source_event_id='u1')  # no start_time

    titles = [r['title'] for r in list_upcoming_events(now=now)]
    assert titles == ['Soon: demo', 'Later: sync']            # future only, soonest-first
    assert 'Past standup' not in titles                       # past excluded
    assert 'Undated note' not in titles                       # no start_time excluded


def test_list_upcoming_events_window_and_limit(ev_models):
    from datetime import datetime, timedelta
    from integrations.social.events import ingest_event, list_upcoming_events
    now = datetime(2026, 6, 4, 12, 0, 0)
    ingest_event('manual', 'In window', source_event_id='w1',
                 start_time=now + timedelta(hours=10))
    ingest_event('manual', 'Outside window', source_event_id='w2',
                 start_time=now + timedelta(hours=400))       # beyond default 168h

    assert [r['title'] for r in list_upcoming_events(now=now)] == ['In window']
    wide = {r['title'] for r in list_upcoming_events(within_hours=500, now=now)}
    assert wide == {'In window', 'Outside window'}
    lim = list_upcoming_events(within_hours=500, limit=1, now=now)
    assert len(lim) == 1 and lim[0]['title'] == 'In window'   # soonest within limit


def test_list_upcoming_events_community_filter(ev_models):
    from datetime import datetime, timedelta
    from integrations.social.events import ingest_event, list_upcoming_events
    now = datetime(2026, 6, 4, 12, 0, 0)
    ingest_event('manual', 'Team A event', source_event_id='a1',
                 start_time=now + timedelta(hours=2), community_id='comm-a')
    ingest_event('manual', 'Team B event', source_event_id='b1',
                 start_time=now + timedelta(hours=3), community_id='comm-b')

    assert [r['title'] for r in list_upcoming_events(community_id='comm-a', now=now)] \
        == ['Team A event']
