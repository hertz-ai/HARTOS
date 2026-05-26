"""Public Flask endpoints for the Hive Contest.

Surface:
  GET  /api/hive/contest/info              — rules, tracks, onramp
  GET  /api/hive/contest/leaderboard       — ranked entries
  GET  /api/hive/contest/claude-code.mcp   — paste-ready MCP snippet
  POST /api/hive/contest/join              — idempotent registration

All endpoints are public except POST /join which needs an authenticated
user (the @require_auth decorator matches the pattern used by
api_audit.py — no new auth mechanism, no parallel path)."""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request, g

from integrations.agent_engine.hive_contest import (
    ContestTrack,
    claude_code_mcp_snippet,
    get_contest_info,
    get_leaderboard,
    list_ideas,
    register_participant,
    submit_idea,
)
from integrations.social.models import get_db
from integrations.social.auth import require_auth

logger = logging.getLogger(__name__)

hive_contest_bp = Blueprint(
    'hive_contest', __name__, url_prefix='/api/hive/contest'
)


def _parse_track(raw) -> ContestTrack | None:
    if not raw:
        return None
    try:
        return ContestTrack(raw.lower())
    except ValueError:
        return None


@hive_contest_bp.route('/info', methods=['GET'])
def contest_info():
    """Public: rules, tracks, dates, how-to-join, Claude Code snippet."""
    return jsonify({'data': get_contest_info()})


@hive_contest_bp.route('/leaderboard', methods=['GET'])
def contest_leaderboard():
    """Public: ranked entries.  Query param:
        ?track=digital|embodied|human_wellness   (default: overall)
        ?limit=50                                (max 200)
    """
    track = _parse_track(request.args.get('track'))
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
    except (TypeError, ValueError):
        limit = 50

    db = get_db()
    try:
        rows = get_leaderboard(db, track=track, limit=limit)
        return jsonify({
            'data': rows,
            'meta': {
                'track': track.value if track else 'overall',
                'count': len(rows),
            },
        })
    finally:
        db.close()


@hive_contest_bp.route('/claude-code.mcp', methods=['GET'])
def contest_mcp_snippet():
    """Public paste-ready snippet for Claude Code -> HARTOS MCP.
    Served as text/plain so the user can pipe it straight into
    their settings file:

        curl -s $HOST/api/hive/contest/claude-code.mcp > ~/.config/claude-code/settings.json
    """
    from flask import Response
    return Response(claude_code_mcp_snippet(), mimetype='text/plain')


@hive_contest_bp.route('/ideas', methods=['GET'])
def contest_ideas_list():
    """Public: list submitted contest ideas.

    Query: ?track=digital|embodied|human_wellness (optional)
           &limit=50                              (max 200)
           &since=<iso>                           (incremental)

    Consumed by:
      - hevolve.ai's floating-ideas page (initial fill)
      - HARTOS local /hive-contest page (grid render)
      - Nunba contest curator (shows 'similar ideas' while a user drafts)
    """
    track = _parse_track(request.args.get('track'))
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
    except (TypeError, ValueError):
        limit = 50
    since = (request.args.get('since') or '').strip() or None

    db = get_db()
    try:
        rows = list_ideas(db, track=track, limit=limit, since_iso=since)
        return jsonify({
            'data': rows,
            'meta': {
                'track': track.value if track else 'all',
                'count': len(rows),
            },
        })
    finally:
        db.close()


@hive_contest_bp.route('/ideas', methods=['POST'])
@require_auth
def contest_ideas_submit():
    """Idempotent-ish: submit a contest idea.

    Body:
      { "title": "...",
        "description": "...",
        "track": "digital" | "embodied" | "human_wellness",
        "source": "ui" | "nunba_agent" | "mcp_agent" }

    The Nunba contest curator POSTs here on behalf of the user after
    the conversational capture completes.  Clicking "Submit" on the
    local /hive-contest page POSTs with source='ui'.  A Claude Code
    plugin can POST via MCP with source='mcp_agent'.
    """
    body = request.get_json(silent=True) or {}
    title = (body.get('title') or '').strip()
    description = (body.get('description') or '').strip()
    track = _parse_track(body.get('track')) or ContestTrack.DIGITAL
    source = (body.get('source') or 'ui').strip().lower()
    if source not in ('ui', 'nunba_agent', 'mcp_agent'):
        source = 'ui'

    user_id = getattr(g.user, 'id', None)
    if not user_id:
        return jsonify({'error': 'auth required'}), 401
    if not title or not description:
        return jsonify({'error': 'title and description required'}), 400

    db = get_db()
    try:
        result = submit_idea(
            db, user_id=user_id, title=title,
            description=description, track=track, source=source,
        )
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.debug(f'commit failed: {exc}')
            return jsonify({'error': 'db commit failed'}), 500
        if not result.get('ok'):
            return jsonify({'error': result.get('reason', 'unknown')}), 400
        return jsonify({'data': result}), 201
    finally:
        db.close()


@hive_contest_bp.route('/ideas/stream', methods=['GET'])
def contest_ideas_stream():
    """Server-Sent Events feed — the Hevolve floating UI keeps this
    connection open and animates a new card every time an idea lands.

    Event names:
      contest.idea_submitted   — payload = {post_id, track, title,
                                  preview, user_id, source, spark_awarded}
      ping                     — keepalive every 15s

    Why SSE (not WebSocket): the feed is one-way server→client.  SSE
    works across all browsers, reconnects automatically, and piggybacks
    on the existing Flask/waitress server.  No extra infrastructure.
    """
    from flask import Response, stream_with_context
    import queue as _queue
    import time as _time

    q: _queue.Queue = _queue.Queue(maxsize=100)

    # Subscribe to the EventBus topic via the platform bus's on()/off()
    # API (same pattern FederatedAggregator + ThemeService use) — no
    # parallel pub/sub stack.  Obtain the bus from the platform
    # ServiceRegistry; absent registry (import-only tests) → bus=None
    # and the stream just emits keepalives.
    try:
        from core.platform.registry import get_registry
        _reg = get_registry()
        bus = _reg.get('events') if _reg.has('events') else None
    except Exception:
        bus = None

    # EventBus callbacks are invoked as (topic, data) — capture data only.
    def _on_event(_topic, payload):
        try:
            q.put_nowait(payload or {})
        except _queue.Full:
            pass

    subscribed = False
    if bus is not None:
        try:
            bus.on('contest.idea_submitted', _on_event)
            subscribed = True
        except Exception as exc:
            logger.debug(f'idea stream subscribe failed: {exc}')

    def _generate():
        import json as _json
        # Initial keepalive so the client connects cleanly
        yield 'event: ping\ndata: {}\n\n'
        last_ping = _time.time()
        try:
            while True:
                try:
                    payload = q.get(timeout=5.0)
                    yield (
                        f'event: contest.idea_submitted\n'
                        f'data: {_json.dumps(payload)}\n\n'
                    )
                except _queue.Empty:
                    pass
                # Keepalive every 15s so intermediate proxies don't
                # close the connection.
                if _time.time() - last_ping > 15:
                    yield 'event: ping\ndata: {}\n\n'
                    last_ping = _time.time()
        finally:
            if subscribed and bus is not None:
                try:
                    bus.off('contest.idea_submitted', _on_event)
                except Exception:
                    pass

    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    }
    return Response(stream_with_context(_generate()), headers=headers)


@hive_contest_bp.route('/join', methods=['POST'])
@require_auth
def contest_join():
    """Idempotent: register the authenticated user for the contest.

    Body:
      { "track": "digital" | "embodied" | "human_wellness",
        "github": "optional-handle",
        "email": "optional" }
    """
    body = request.get_json(silent=True) or {}
    track = _parse_track(body.get('track')) or ContestTrack.DIGITAL
    github = (body.get('github') or '').strip() or None
    email = (body.get('email') or '').strip() or None

    user_id = getattr(g.user, 'id', None)
    if not user_id:
        return jsonify({'error': 'auth required'}), 401

    db = get_db()
    try:
        result = register_participant(
            db, user_id=user_id, track=track,
            github_handle=github, email=email,
        )
        try:
            db.commit()
        except Exception as exc:  # pragma: no cover
            db.rollback()
            logger.debug(f'commit failed: {exc}')
        return jsonify({'data': result})
    finally:
        db.close()
