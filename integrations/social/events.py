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
from datetime import datetime, timedelta

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


# ── Video-meeting sources: Zoom + Google Meet (omni-channel bridge #64) ──
# Same shape as the .ics path: a PURE parse_*() (no I/O, unit-tested against the
# documented API response) feeds the canonical ingest_event(); a thin
# fetch_and_ingest_*() wrapper adds the network call.  The OAuth bearer token is
# the caller's responsibility (creds-gated, like every other channel adapter) —
# absent a token the fetch is a no-op, so the source is simply inactive until
# credentials are configured.  The wrappers are live-validated once those creds
# exist; the mapping logic is fully covered now.

def _parse_iso_dt(v):
    """Parse an ISO-8601 datetime (Zoom + Google emit e.g. 2026-06-04T15:00:00Z).
    Returns a naive-UTC datetime, or None.  Tolerates offset / fractional forms."""
    if not v:
        return None
    v = v.strip()
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:  # offsets (+05:30) / fractional seconds
        return datetime.fromisoformat(v.replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def parse_zoom_meetings(api_json):
    """Zoom `GET /users/me/meetings` response → ingest_event field dicts (pure).

    Documented shape: {"meetings": [{"id", "topic", "agenda", "start_time"
    (ISO-8601 Z), "duration" (minutes), "join_url", …}]}.  No network — accepts
    either the full dict or a bare meetings list so it is trivially unit-tested.
    """
    meetings = api_json.get('meetings', []) if isinstance(api_json, dict) else (api_json or [])
    out = []
    for m in meetings:
        if not isinstance(m, dict):
            continue
        start = _parse_iso_dt(m.get('start_time'))
        end = None
        dur = m.get('duration')
        if start is not None and isinstance(dur, (int, float)):
            end = start + timedelta(minutes=int(dur))
        out.append({
            'source_event_id': str(m['id']) if m.get('id') is not None else None,
            'title': m.get('topic') or '(untitled meeting)',
            'description': m.get('agenda', '') or '',
            'url': m.get('join_url'),
            'start_time': start,
            'end_time': end,
        })
    return out


def ingest_zoom_meetings(api_json, community_id=None, created_by=None):
    """Ingest every meeting from a Zoom meetings-list response (source='zoom')."""
    results = []
    for fields in parse_zoom_meetings(api_json):
        row = ingest_event(source='zoom', community_id=community_id,
                           created_by=created_by, **fields)
        if row:
            results.append(row)
    return results


def parse_gmeet_events(api_json):
    """Google Calendar `events.list` response → ingest_event field dicts (pure).

    Only events that actually carry a Meet link (hangoutLink, or a
    conferenceData video entryPoint) are surfaced — a plain calendar entry is
    not a meeting.  Documented shape: {"items": [{"id", "summary", "description",
    "location", "start": {"dateTime"|"date"}, "end": {…}, "hangoutLink",
    "conferenceData": {"entryPoints": [{"entryPointType": "video", "uri"}]}}]}.
    """
    items = api_json.get('items', []) if isinstance(api_json, dict) else (api_json or [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        link = it.get('hangoutLink')
        if not link:
            for ep in (it.get('conferenceData') or {}).get('entryPoints', []) or []:
                if isinstance(ep, dict) and ep.get('entryPointType') == 'video' and ep.get('uri'):
                    link = ep['uri']
                    break
        if not link:
            continue  # not a Meet event — skip
        start = it.get('start') or {}
        end = it.get('end') or {}
        out.append({
            'source_event_id': it.get('id'),
            'title': it.get('summary') or '(untitled meeting)',
            'description': it.get('description', '') or '',
            'location': it.get('location'),
            'url': link,
            'start_time': _parse_iso_dt(start.get('dateTime') or start.get('date')),
            'end_time': _parse_iso_dt(end.get('dateTime') or end.get('date')),
        })
    return out


def ingest_gmeet_events(api_json, community_id=None, created_by=None):
    """Ingest every Meet-linked event from a Google Calendar response (source='gmeet')."""
    results = []
    for fields in parse_gmeet_events(api_json):
        row = ingest_event(source='gmeet', community_id=community_id,
                           created_by=created_by, **fields)
        if row:
            results.append(row)
    return results


_ZOOM_MEETINGS_API = 'https://api.zoom.us/v2/users/me/meetings'
_GCAL_EVENTS_API = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'


def fetch_and_ingest_zoom(access_token, community_id=None, created_by=None, timeout=15):
    """Fetch upcoming Zoom meetings (OAuth scope meeting:read) and ingest them.

    Network wrapper around the unit-tested parse_zoom_meetings/ingest_event core.
    Requires a Zoom OAuth bearer token; returns [] (inactive) without one, and
    on any network/HTTP error (timeout-bounded).  Live-validated once Zoom OAuth
    app credentials are configured.
    """
    if not access_token:
        return []
    try:
        import requests
        resp = requests.get(
            _ZOOM_MEETINGS_API, params={'type': 'upcoming', 'page_size': 100},
            headers={'Authorization': f'Bearer {access_token}'}, timeout=timeout)
        resp.raise_for_status()
        return ingest_zoom_meetings(resp.json(), community_id=community_id, created_by=created_by)
    except Exception as e:
        logger.warning(f"fetch_and_ingest_zoom failed: {e}")
        return []


def fetch_and_ingest_gmeet(access_token, time_min_iso=None, community_id=None,
                           created_by=None, timeout=15):
    """Fetch upcoming Meet-linked Google Calendar events and ingest them.

    Network wrapper around the unit-tested parse_gmeet_events/ingest_event core.
    Requires a Google OAuth bearer token (scope calendar.readonly); returns []
    without one or on error.  `time_min_iso` defaults to now (caller may pass a
    fixed value for determinism).  Live-validated once Google OAuth creds exist.
    """
    if not access_token:
        return []
    try:
        import requests
        if not time_min_iso:
            from datetime import timezone
            time_min_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        resp = requests.get(
            _GCAL_EVENTS_API,
            params={'timeMin': time_min_iso, 'singleEvents': 'true',
                    'orderBy': 'startTime', 'maxResults': 100},
            headers={'Authorization': f'Bearer {access_token}'}, timeout=timeout)
        resp.raise_for_status()
        return ingest_gmeet_events(resp.json(), community_id=community_id, created_by=created_by)
    except Exception as e:
        logger.warning(f"fetch_and_ingest_gmeet failed: {e}")
        return []


def list_upcoming_events(within_hours=168, community_id=None, created_by=None,
                         limit=50, now=None):
    """Read upcoming events from the Event table — the READ side of ingest_event.

    Without this the Event table is write-only: every ingest_* path (ics / Zoom /
    Meet) writes rows nothing can read back.  Returns the events whose
    start_time is in [now, now+within_hours), soonest-first, as Event dicts.
    Events with no start_time are excluded (can't be "upcoming").  Optional
    community_id / created_by scope the read.  `now` is injectable for
    deterministic tests; defaults to utcnow().  within_hours=0/None => no upper
    horizon (everything from now on).  Returns [] on any failure.
    """
    try:
        from integrations.social.models import db_session, Event
    except Exception as e:
        logger.warning(f"list_upcoming_events: models unavailable: {e}")
        return []
    if now is None:
        now = datetime.utcnow()
    try:
        horizon = now + timedelta(hours=within_hours) if within_hours else None
        with db_session() as db:
            q = db.query(Event).filter(
                Event.start_time != None,           # noqa: E711  (SQL IS NOT NULL)
                Event.start_time >= now,
            )
            if horizon is not None:
                q = q.filter(Event.start_time < horizon)
            if community_id:
                q = q.filter(Event.community_id == community_id)
            if created_by:
                q = q.filter(Event.created_by == created_by)
            q = q.order_by(Event.start_time.asc())
            if limit:
                q = q.limit(limit)
            return [ev.to_dict() for ev in q.all()]
    except Exception as e:
        logger.warning(f"list_upcoming_events failed: {e}")
        return []
