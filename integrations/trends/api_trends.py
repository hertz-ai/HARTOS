"""integrations.trends.api_trends: GET /api/trends/songs/latest, central's
published trending-song list for desktops to pull.

Public and read-only: the list holds no URLs, no session data and nothing
about any user (contracts.validate_trending_songs; trending_songs.read_latest
serves nothing it refuses).  security.middleware's API gate leaves it open
on central (not an admin path, not a network-protected one).

Registered ONLY on a central node (critique finding 42): anywhere else it
would be a new public route serving nothing.  init_agent_engine calls
register_trends_api in its synchronous phase 1.

Query: ``lang`` (comma-separated language tags; songs whose language_hint
is one of them), ``limit`` (1 to trending_songs.MAX_SONGS).  Junk in either
is ignored, never an error.  Before the first publish the body is
``{"schema", "generated_at": null, "songs": []}``.  Responses carry a
strong ETag over the body and ``Cache-Control: public``; a matching
``If-None-Match`` gets a 304 with no body.
"""
import hashlib
import json
import logging
import re

from flask import Blueprint, Response, request

from integrations.reel_studio.contracts import TRENDING_SCHEMA
from integrations.trends import trending_songs as ts

logger = logging.getLogger(__name__)

ROUTE = ts.SONGS_ROUTE
BLUEPRINT_NAME = 'trends_api'
#: Desktops cache six hours; an hour here lets a shared cache in between
#: answer without going stale for long.
CACHE_CONTROL = 'public, max-age=3600'

_LANG_RE = re.compile(r'[a-z]{2,8}')

trends_bp = Blueprint(BLUEPRINT_NAME, __name__)


def register_trends_api(app) -> bool:
    """Add the route to ``app`` when this node is central; True when the
    route is (now) on ``app``, False when the node is not central.
    Registering twice is harmless."""
    from security.key_delegation import get_node_tier
    if get_node_tier() != 'central':
        return False
    if BLUEPRINT_NAME not in app.blueprints:
        app.register_blueprint(trends_bp)
    return True


def _langs(raw):
    if not isinstance(raw, str):
        return None
    tags = [t.strip().lower() for t in raw.split(',')]
    tags = [t for t in tags if _LANG_RE.fullmatch(t)]
    return set(tags) or None


def _limit(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return min(value, ts.MAX_SONGS) if value > 0 else None


def narrow(doc, langs=None, limit=None):
    """``doc`` with its songs filtered to ``langs`` (by language_hint) and
    cut to ``limit``; ``doc`` itself when neither applies."""
    if not langs and not limit:
        return doc
    songs = [s for s in doc.get('songs') or []
             if not langs or (isinstance(s, dict) and s.get('language_hint') in langs)]
    return dict(doc, songs=songs[:limit] if limit else songs)


@trends_bp.route(ROUTE, methods=['GET'])
def latest_songs():
    doc = ts.read_latest() or {'schema': TRENDING_SCHEMA, 'generated_at': None, 'songs': []}
    body = narrow(doc, _langs(request.args.get('lang')), _limit(request.args.get('limit')))
    data = json.dumps(body, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')
    etag = '"%s"' % hashlib.sha256(data).hexdigest()[:32]
    headers = {'ETag': etag, 'Cache-Control': CACHE_CONTROL}
    wanted = [t.strip() for t in (request.headers.get('If-None-Match') or '').split(',')]
    if etag in wanted or '*' in wanted:
        return Response(status=304, headers=headers)
    return Response(data, status=200, mimetype='application/json', headers=headers)
