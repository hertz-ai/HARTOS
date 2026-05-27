"""Flywheel-bootstrap regression — benchmark proof publishing actually
lands a post + fires fan-out.

Previously _publish_results inlined `db.add(Post(author_id=
'nunba'))` — but posts.author_id is a non-null FK to
users.id, so the literal string failed the constraint silently inside
try/except.  Net effect: every benchmark run "published" but nothing
persisted, no fan-out fired, no users saw the proof — the
self-advertising leg of the flywheel was dead.

The fix routes through UserService.ensure_system_user +
PostService.create — the same canonical fan-out path human posts go
through, which fires on_new_post and reaches every WAMP/SSE subscriber
on web/desktop/Android/iOS.

These tests prove:
  - the system user gets bootstrapped on first publish
  - the social post actually persists (not silently swallowed)
  - on_new_post fan-out fires exactly once per benchmark result
  - re-running doesn't create duplicate system users (idempotent)
"""
from __future__ import annotations

import os
import sys
import uuid
from unittest.mock import patch

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def fresh_db(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db
    eng = get_engine()
    migrations.run_migrations()
    db = get_db()
    try:
        yield db, eng
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            eng.dispose()
        except Exception:
            pass
        models_mod._engine = None
        models_mod._SessionLocal = None


# ── ensure_system_user contract ────────────────────────────────

def test_ensure_system_user_creates_row_on_first_call(fresh_db):
    db, _ = fresh_db
    from integrations.social.services import UserService
    from integrations.social.models import User

    u = UserService.ensure_system_user(
        db, 'nunba',
        display_name='Nunba')
    db.commit()

    assert u.id is not None
    assert u.username == 'nunba'
    assert u.user_type == 'system'
    assert u.api_token  # generated, non-empty
    # And the row actually persisted.
    row = db.query(User).filter_by(username='nunba').first()
    assert row is not None
    assert row.id == u.id


def test_ensure_system_user_idempotent(fresh_db):
    db, _ = fresh_db
    from integrations.social.services import UserService

    u1 = UserService.ensure_system_user(db, 'nunba')
    db.commit()
    u2 = UserService.ensure_system_user(db, 'nunba')
    db.commit()

    # Same row, not a new one.  This matters because the publish
    # path calls ensure_system_user on every benchmark run — a
    # non-idempotent helper would either fail uniqueness or fork
    # the identity.
    assert u1.id == u2.id


def test_ensure_system_user_refuses_to_hijack_human_account(fresh_db):
    """If a human grabbed 'nunba' before bootstrap, ensure_system_user
    must NOT silently use their row to author system content.
    Otherwise benchmark proofs would post under a real user's
    identity — confusing and impersonating."""
    db, _ = fresh_db
    from integrations.social.models import User
    # Seed a human row claiming 'nunba'.
    human = User(
        id=str(uuid.uuid4()),
        username='nunba',
        display_name='Some Human Named Nunba',
        email='human@example.test',
        password_hash='x:y',
        user_type='human',
    )
    db.add(human)
    db.commit()

    from integrations.social.services import UserService
    with pytest.raises(ValueError, match='already taken by a non-system'):
        UserService.ensure_system_user(db, 'nunba')


# ── benchmark publish goes through PostService.create ──────────

def test_benchmark_publish_persists_post_with_real_user(fresh_db):
    """End-to-end: call _publish_results with a synthetic result dict
    and verify the post actually persisted AND that on_new_post fired."""
    db, _ = fresh_db

    from integrations.social import realtime as realtime_mod
    fan_out = []

    with patch.object(realtime_mod, 'on_new_post',
                      side_effect=lambda *a, **kw: fan_out.append((a, kw))):
        from integrations.agent_engine.hive_benchmark_prover import (
            HiveBenchmarkProver)
        prover = HiveBenchmarkProver()
        publish_info = prover._publish_results(
            run_id='run-abc-123',
            results={
                'benchmark': 'mmlu',
                'score': 0.872,
                'num_nodes': 4,
                'speedup': 3.1,
                'time_seconds': 27.4,
            })

    # Post id surfaced — pre-fix this was None because the FK failure
    # was caught + swallowed silently.
    assert publish_info.get('post_id') is not None, (
        "post_id must be populated — bare db.add() failed silently "
        "pre-fix; PostService.create must persist a real row")

    # Post actually in the DB with a real (FK-satisfying) author_id.
    from integrations.social.models import Post, User
    post = db.query(Post).filter_by(id=publish_info['post_id']).first()
    assert post is not None
    assert post.is_deleted is False
    assert 'mmlu' in post.title.lower()
    assert '87' in post.content  # 0.872 formats as 87.2%

    author = db.query(User).filter_by(id=post.author_id).first()
    assert author is not None
    assert author.username == 'nunba'
    assert author.user_type == 'system'

    # And on_new_post fired — which means web/desktop/Android/iOS
    # subscribers see the proof in their realtime feed.  This is
    # the self-advertising leg of the flywheel that was dead pre-fix.
    assert len(fan_out) == 1
    args, _kw = fan_out[0]
    payload = args[0]
    assert payload['id'] == post.id
    assert 'mmlu' in payload['title'].lower()


def test_two_benchmarks_share_one_prover_identity(fresh_db):
    """Running two benchmarks must NOT create two prover users.  The
    ensure_system_user idempotency keeps the leaderboard / audit
    trail attributable to one consistent identity."""
    db, _ = fresh_db
    from integrations.social import realtime as realtime_mod

    with patch.object(realtime_mod, 'on_new_post'):
        from integrations.agent_engine.hive_benchmark_prover import (
            HiveBenchmarkProver)
        prover = HiveBenchmarkProver()
        info1 = prover._publish_results('run-1', {
            'benchmark': 'mmlu', 'score': 0.8, 'num_nodes': 2,
            'speedup': 1.8, 'time_seconds': 10.0,
        })
        info2 = prover._publish_results('run-2', {
            'benchmark': 'humaneval', 'score': 0.7, 'num_nodes': 3,
            'speedup': 2.5, 'time_seconds': 8.0,
        })

    from integrations.social.models import User, Post
    provers = db.query(User).filter_by(
        username='nunba').all()
    assert len(provers) == 1, (
        f"expected ONE prover identity, got {len(provers)} — "
        f"ensure_system_user idempotency broken"
    )
    # Both posts share that one identity.
    post1 = db.query(Post).filter_by(id=info1['post_id']).first()
    post2 = db.query(Post).filter_by(id=info2['post_id']).first()
    assert post1.author_id == post2.author_id == provers[0].id


def test_publish_resilient_to_fan_out_failure(fresh_db, monkeypatch):
    """If realtime fan-out raises, the benchmark post must STILL
    persist — operators see the proof in the feed on next refresh
    even if WAMP is down.  Same resilience contract as #51."""
    db, _ = fresh_db

    from integrations.social import realtime as realtime_mod

    def boom(*a, **kw):
        raise RuntimeError("simulated WAMP outage")
    monkeypatch.setattr(realtime_mod, 'on_new_post', boom)

    from integrations.agent_engine.hive_benchmark_prover import (
        HiveBenchmarkProver)
    prover = HiveBenchmarkProver()
    publish_info = prover._publish_results('run-resilient', {
        'benchmark': 'gsm8k', 'score': 0.5, 'num_nodes': 1,
        'speedup': 1.0, 'time_seconds': 5.0,
    })

    # Even with fan-out broken, the post landed.
    assert publish_info.get('post_id') is not None
    from integrations.social.models import Post
    post = db.query(Post).filter_by(id=publish_info['post_id']).first()
    assert post is not None
