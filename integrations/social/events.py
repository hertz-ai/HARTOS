"""Event ingestion — omni-channel bridge Phase 3a (#64).

Surfaces external events (Discord scheduled events, Meetup, .ics calendars) into
Nunba's Event table.  Mirrors cross_channel.py (the inbound message ingest):
external sources call ingest_event(); a stdlib .ics parser is included so a
calendar feed works with no external dependency.  Discord/Meetup adapters just
call ingest_event() with their fields — no per-source table logic.

Dedup is on (source, source_event_id): re-ingesting the same upstream event
UPDATES the row instead of creating a duplicate.
"""
import logging
from datetime import datetime

logger = logging.getLogger('hevolve_social')


def ingest_event(source, title, source_event_id=None, start_time=None,
                 end_time=None, location=None, url=None, description='',
                 community_id=None, created_by=None):
    """Upsert one event.  Returns the Event dict, or None on failure.

    `source` in {discord, meetup, ics, manual, ...}.  When `source_event_id` is
    set, an existing row with the same (source, source_event_id) is UPDATED
    (idempotent re-ingest); otherwise a new row is created.
    """
    try:
        from integrations.social.models import db_session, Event
    except Exception as e:
        logger.warning(f"ingest_event: models unavailable: {e}")
        return None
    if not title:
        return None
    try:
        with db_session() as db:
            existing = None
            if source_event_id:
                existing = db.query(Event).filter(
                    Event.source == source,
                    Event.source_event_id == source_event_id,
                ).first()
            if existing is not None:
                existing.title = title
                existing.description = description or ''
                existing.start_time = start_time
                existing.end_time = end_time
                existing.location = location
                existing.url = url
                if community_id:
                    existing.community_id = community_id
                db.flush()
                return existing.to_dict()

            ev = Event(
                source=source, source_event_id=source_event_id, title=title,
                description=description or '', start_time=start_time,
                end_time=end_time, location=location, url=url,
                community_id=community_id, created_by=created_by,
            )
            db.add(ev)
            db.flush()
            return ev.to_dict()
    except Exception as e:
        logger.warning(f"ingest_event failed (source={source}): {e}")
        return None


def _parse_ics_dt(v):
    """Parse an iCalendar date/datetime value (stdlib).  Handles UTC (…Z),
    floating local, and all-day (date-only) forms.  Returns datetime or None."""
    if not v:
        return None
    v = v.strip()
    for fmt in ('%Y%m%dT%H%M%SZ', '%Y%m%dT%H%M%S', '%Y%m%d'):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def parse_ics(ics_text):
    """Parse VEVENTs from an .ics string into event-field dicts (stdlib only).

    Handles RFC 5545 line folding (continuation lines start with space/tab) and
    property parameters (DTSTART;TZID=… → DTSTART).  Returns a list of dicts
    ready to splat into ingest_event().
    """
    if not ics_text:
        return []
    raw = ics_text.replace('\r\n', '\n').replace('\r', '\n')
    unfolded = []
    for line in raw.split('\n'):
        if line[:1] in (' ', '\t') and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    events = []
    cur = None
    for line in unfolded:
        s = line.strip()
        if s == 'BEGIN:VEVENT':
            cur = {}
        elif s == 'END:VEVENT':
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ':' in line:
            key, val = line.split(':', 1)
            name = key.split(';', 1)[0].upper()  # strip params (TZID, VALUE, …)
            cur[name] = val.strip()

    out = []
    for e in events:
        out.append({
            'source_event_id': e.get('UID'),
            'title': e.get('SUMMARY', '(untitled)'),
            'description': e.get('DESCRIPTION', ''),
            'location': e.get('LOCATION'),
            'url': e.get('URL'),
            'start_time': _parse_ics_dt(e.get('DTSTART')),
            'end_time': _parse_ics_dt(e.get('DTEND')),
        })
    return out


def ingest_ics(ics_text, community_id=None, created_by=None):
    """Parse an .ics feed and ingest every VEVENT (source='ics').  Returns the
    list of ingested Event dicts (skips any that failed)."""
    results = []
    for fields in parse_ics(ics_text):
        row = ingest_event(source='ics', community_id=community_id,
                            created_by=created_by, **fields)
        if row:
            results.append(row)
    return results
