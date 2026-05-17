"""Unit tests for the hive-contest idea submission pipeline.

Covers:
  - submit_idea() creates a SocialPost (content_type='contest_idea')
  - submit_idea() awards contest Spark via score_event('idea_submitted')
  - submit_idea() emits 'contest.idea_submitted' on the EventBus
  - list_ideas() returns posts filtered by track
  - idea_submitted is a valid EVENT_TYPE with per-track weight
  - Constitutional filter blocks violating content
  - UI page route returns 200 HTML
  - UI page references all three tracks + ideas wall + submit form
  - Panel registered in PANEL_MANIFEST
  - Contest Curator seed goal present
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine import hive_contest as hc


def test_idea_submitted_event_type_registered():
    assert 'idea_submitted' in hc.EVENT_TYPES


# ── Public canonical URL ───────────────────────────────────────────

def test_contest_public_url_default(monkeypatch):
    """Without env override, URL defaults to hevolve.ai/hive_contest."""
    monkeypatch.delenv('HEVOLVE_CONTEST_PUBLIC_URL', raising=False)
    assert hc.get_contest_public_url() == 'https://hevolve.ai/hive_contest'


def test_contest_public_url_env_override(monkeypatch):
    """A staging deploy overrides via HEVOLVE_CONTEST_PUBLIC_URL."""
    monkeypatch.setenv(
        'HEVOLVE_CONTEST_PUBLIC_URL',
        'https://staging.hevolve.ai/hive_contest',
    )
    assert hc.get_contest_public_url() == (
        'https://staging.hevolve.ai/hive_contest'
    )


def test_contest_public_url_falls_back_on_empty(monkeypatch):
    """Empty/whitespace env falls through to the default — not empty."""
    monkeypatch.setenv('HEVOLVE_CONTEST_PUBLIC_URL', '   ')
    assert hc.get_contest_public_url() == 'https://hevolve.ai/hive_contest'


def test_contest_info_exposes_public_url(monkeypatch):
    """get_contest_info() carries the public_url so every consumer
    (local UI footer, hevolve.ai React, API callers) gets the same
    canonical destination."""
    monkeypatch.delenv('HEVOLVE_CONTEST_PUBLIC_URL', raising=False)
    info = hc.get_contest_info()
    assert info['public_url'] == 'https://hevolve.ai/hive_contest'
    # how_to_join[0] must lead with the canonical page — workflow
    # surfaces read this as the CTA.
    assert any(
        'hevolve.ai/hive_contest' in line for line in info['how_to_join']
    )


def test_quest_prompt_uses_canonical_url_not_docs():
    """Regression: Quest's description should NOT hardcode
    docs.hevolve.ai/hive-contest as the CTA — that page redirects.
    Instead the prompt instructs Quest to read from
    get_contest_public_url()."""
    from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
    quest = next(g for g in SEED_BOOTSTRAP_GOALS
                 if g['slug'] == 'bootstrap_quest_contest_host')
    desc = quest['description']
    assert 'get_contest_public_url' in desc
    assert 'hevolve.ai/hive_contest' in desc


def test_docs_page_has_redirect_to_app_page():
    """docs.hevolve.ai/hive-contest/ must bounce to the app page so
    old links keep landing users somewhere live."""
    import os as _os
    path = _os.path.join(_ROOT, 'docs', 'hive-contest.md')
    with open(path, 'r', encoding='utf-8') as fh:
        body = fh.read()
    assert 'hevolve.ai/hive_contest' in body
    # Three converging redirect channels for robustness
    assert 'http-equiv="refresh"' in body
    assert '<link rel="canonical"' in body
    assert 'window.location.replace' in body


def test_ideas_submitted_weight_per_track():
    for track in (hc.ContestTrack.DIGITAL,
                  hc.ContestTrack.EMBODIED,
                  hc.ContestTrack.HUMAN_WELLNESS):
        assert 'ideas_submitted' in hc.SCORE_WEIGHTS[track], (
            f'{track} missing ideas_submitted weight'
        )
        assert hc.SCORE_WEIGHTS[track]['ideas_submitted'] > 0


def test_event_weight_mapping_for_idea_submitted():
    # score_event uses key_map; idea_submitted → ideas_submitted
    w = hc._event_weight('idea_submitted', hc.ContestTrack.DIGITAL)
    assert w == 10.0


def test_track_event_source_types_includes_idea_submitted():
    for track in hc.ContestTrack:
        src_types = hc._track_event_source_types(track)
        assert 'contest:idea_submitted' in src_types


# ── submit_idea pipeline ────────────────────────────────────────────

class _StubPost:
    def __init__(self, **kw):
        self.id = kw.get('id') or 'post-id-1'
        self.author_id = kw['author_id']
        self.title = kw['title']
        self.content = kw['content']
        self.content_type = kw['content_type']
        self.source_channel = kw['source_channel']
        self.score = 0
        self.is_hidden = False
        from datetime import datetime
        self.created_at = datetime.utcnow()
    def to_dict(self):
        return {
            'id': self.id, 'title': self.title, 'content': self.content,
            'content_type': self.content_type,
            'source_channel': self.source_channel,
            'score': self.score,
        }


class _StubDB:
    def __init__(self):
        self.added = []
        self.flushed = False
    def add(self, obj):
        self.added.append(obj)
    def flush(self):
        self.flushed = True
    def commit(self):
        pass
    def rollback(self):
        pass
    def close(self):
        pass


def test_submit_idea_rejects_missing_fields():
    db = _StubDB()
    out = hc.submit_idea(db, user_id='u1', title='', description='x')
    assert out == {'ok': False, 'reason': 'title+description required'}


def test_submit_idea_truncates_overlong_fields(monkeypatch):
    db = _StubDB()
    # Stub SocialPost + ResonanceService imports
    monkeypatch.setattr(
        'integrations.social.models.SocialPost', _StubPost, raising=False,
    )
    monkeypatch.setattr(
        hc, 'score_event',
        lambda db, user_id, event_type, track, source_id=None, description='': 10,
    )
    out = hc.submit_idea(
        db, user_id='u1',
        title='X' * 500,
        description='Y' * 10000,
    )
    assert out['ok'] is True
    post = db.added[0]
    assert len(post.title) <= 200
    assert len(post.content) <= 4000


def test_submit_idea_happy_path_creates_post_and_awards_spark(monkeypatch):
    db = _StubDB()
    monkeypatch.setattr(
        'integrations.social.models.SocialPost', _StubPost, raising=False,
    )
    awarded = {'amount': 0, 'event_type': None, 'track': None}
    def _fake_score_event(db_, user_id, event_type, track,
                          source_id=None, description=''):
        awarded['amount'] = 10
        awarded['event_type'] = event_type
        awarded['track'] = track
        return 10
    monkeypatch.setattr(hc, 'score_event', _fake_score_event)

    emitted = []
    monkeypatch.setattr(
        'core.platform.events.emit_event',
        lambda topic, data: emitted.append((topic, data)),
    )

    out = hc.submit_idea(
        db, user_id='u42',
        title='Focus-helper companion',
        description='A daily checkin that notices when my focus drifts.',
        track=hc.ContestTrack.HUMAN_WELLNESS,
        source='nunba_agent',
    )
    assert out['ok'] is True
    assert out['track'] == 'human_wellness'
    assert out['spark_awarded'] == 10

    # Post row shape
    assert len(db.added) == 1
    post = db.added[0]
    assert post.content_type == 'contest_idea'
    assert post.source_channel == 'contest:human_wellness'
    assert post.title == 'Focus-helper companion'

    # Canonical event path used — no shadow ledger
    assert awarded['event_type'] == 'idea_submitted'
    assert awarded['track'] == hc.ContestTrack.HUMAN_WELLNESS

    # EventBus fan-out for Hevolve floating UI
    assert any(t == 'contest.idea_submitted' for t, _ in emitted)
    payload = next(d for t, d in emitted if t == 'contest.idea_submitted')
    assert payload['track'] == 'human_wellness'
    assert payload['source'] == 'nunba_agent'
    assert payload['spark_awarded'] == 10


def test_submit_idea_constitutional_gate_blocks(monkeypatch):
    db = _StubDB()
    with mock.patch(
        'security.hive_guardrails.ConstitutionalFilter.check_prompt',
        return_value=(False, 'blocked: violation_pattern'),
    ):
        out = hc.submit_idea(
            db, user_id='u1',
            title='x', description='y',
        )
    assert out['ok'] is False
    assert 'blocked' in out['reason']
    assert db.added == []   # no post row when blocked


# ── list_ideas ──────────────────────────────────────────────────────

def test_list_ideas_filters_by_track(monkeypatch):
    """Return rows via a chained query mock; verify the function maps
    source_channel → track and returns the expected shape."""
    class _StubQuery:
        def __init__(self, rows):
            self._rows = rows
        def filter(self, *a, **kw):
            return self
        def order_by(self, *a):
            return self
        def limit(self, n):
            self._rows = self._rows[:n]
            return self
        def all(self):
            return self._rows

    rows = [
        _StubPost(author_id='u1', title='A', content='aaa',
                  content_type='contest_idea',
                  source_channel='contest:digital'),
        _StubPost(author_id='u2', title='B', content='bbb',
                  content_type='contest_idea',
                  source_channel='contest:embodied'),
    ]
    db = mock.MagicMock()
    db.query = mock.MagicMock(return_value=_StubQuery(rows))

    # Fake SocialPost class with class-level attrs so SQLA-style
    # `SocialPost.content_type == x` expressions can be constructed.
    class _FakeCol:
        def __eq__(self, other): return True
        def desc(self): return self
        def is_(self, _other): return True
    class _FakeSocialPost:
        content_type = _FakeCol()
        source_channel = _FakeCol()
        is_hidden = _FakeCol()
        score = _FakeCol()
        created_at = _FakeCol()

    import sys as _sys
    fake_models = type(_sys)('fake_models')
    fake_models.SocialPost = _FakeSocialPost
    monkeypatch.setitem(
        _sys.modules, 'integrations.social.models', fake_models,
    )
    out = hc.list_ideas(db, track=hc.ContestTrack.DIGITAL, limit=10)
    assert len(out) == 2
    # track field extracted from source_channel
    assert {r['track'] for r in out} == {'digital', 'embodied'}
    # preview populated from content
    assert all('preview' in r for r in out)


# ── Manifests / seeds ───────────────────────────────────────────────

def test_panel_registered_in_manifest():
    from integrations.agent_engine.shell_manifest import PANEL_MANIFEST
    assert 'hive_contest' in PANEL_MANIFEST
    panel = PANEL_MANIFEST['hive_contest']
    assert panel['route'] == '/hive-contest'
    assert panel['group'] == 'Explore'


def test_contest_curator_seeded():
    from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
    slugs = [g.get('slug') for g in SEED_BOOTSTRAP_GOALS]
    assert 'bootstrap_curator_idea_capture' in slugs
    curator = next(g for g in SEED_BOOTSTRAP_GOALS
                   if g['slug'] == 'bootstrap_curator_idea_capture')
    assert curator['title'] == 'Contest Curator'
    cfg = curator['config']
    assert cfg['persona_kind'] == 'contest-curator'
    assert cfg['submit_endpoint'] == '/api/hive/contest/ideas'
    assert cfg['source_marker'] == 'nunba_agent'
    # Trigger phrases Nunba's router will match against
    assert any('contest idea' in t for t in cfg['entry_triggers'])


# ── UI route ────────────────────────────────────────────────────────

def test_ui_route_returns_html(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    from integrations.social.ui_hive_contest import hive_contest_ui_bp
    app.register_blueprint(hive_contest_ui_bp)
    client = app.test_client()
    r = client.get('/hive-contest')
    assert r.status_code == 200
    assert r.headers['Content-Type'].startswith('text/html')
    body = r.get_data(as_text=True)
    # Key landmarks
    assert 'Hive Contest' in body
    assert 'Three tracks' in body
    assert 'Leaderboard' in body
    assert 'Ideas wall' in body
    assert 'Claude Code' in body
    assert '/api/hive/contest/info' in body
    assert '/api/hive/contest/ideas' in body
    assert '/api/hive/contest/ideas/stream' in body
    # Three track values present in the submit form
    for t in ('digital', 'embodied', 'human_wellness'):
        assert t in body
