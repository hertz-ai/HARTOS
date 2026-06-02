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
