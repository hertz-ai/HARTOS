"""Behavioural tests for #49/#51/#52/#54 — every Post/Comment lifecycle
mutation in services.py fires the canonical realtime helper exactly
once, with the right event discriminator + community routing.

These exercise the REAL services on in-memory SQLite and assert on the
realtime helper call args (not the wire — that's covered by
test_social_realtime_pii_sanitizer.py).  The two layers compose: this
suite proves the SERVICE WIRING; the realtime suite proves the wire
shape.  Together they cover the full publisher chain end-to-end.
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


# ── Shared fixtures (mirror audit-trail test) ─────────────────

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


def _seed_user(db, username='alice'):
    from integrations.social.models import User
    u = User(id=str(uuid.uuid4()),
             username=f'{username}_{uuid.uuid4().hex[:6]}',
             display_name=username.title(),
             email=f'{username}_{uuid.uuid4().hex[:6]}@x.test',
             password_hash='x:y',
             user_type='human')
    db.add(u)
    db.commit()
    return u


def _seed_community(db, owner_id, name='general'):
    from sqlalchemy import text
    from integrations.social.models import Community
    cid = str(uuid.uuid4())
    cname = f'{name}_{uuid.uuid4().hex[:6]}'
    com = Community(id=cid, name=cname, display_name=cname.title(),
                    description='', creator_id=owner_id, is_private=False)
    db.add(com)
    db.commit()
    db.execute(text(
        "INSERT INTO memberships "
        "(id, parent_kind, parent_id, member_id, agent_kind, role) "
        "VALUES (:id, 'community', :pid, :mid, 'human', 'admin')"),
        {'id': str(uuid.uuid4()), 'pid': cid, 'mid': owner_id})
    db.commit()
    return com


# ── Helper to capture realtime calls ──────────────────────────

class _RTCapture:
    """Captures every realtime.* call so tests can assert wiring
    without touching the WAMP wire."""
    def __init__(self):
        self.calls = []  # list of (fn_name, args, kwargs)

    def __call__(self, fn_name):
        def shim(*args, **kwargs):
            self.calls.append((fn_name, args, kwargs))
        return shim

    def of(self, fn_name):
        return [(args, kwargs) for n, args, kwargs in self.calls if n == fn_name]


@pytest.fixture
def rt():
    """Patches realtime.on_* helpers so service-layer wiring can be
    inspected.  Use rt.of('on_new_post') to list calls of one helper."""
    cap = _RTCapture()
    from integrations.social import realtime
    targets = ['on_new_post', 'on_post_update', 'on_post_delete',
               'on_new_comment', 'on_comment_delete']
    with patch.multiple(
        realtime,
        on_new_post=cap('on_new_post'),
        on_post_update=cap('on_post_update'),
        on_post_delete=cap('on_post_delete'),
        on_new_comment=cap('on_new_comment'),
        on_comment_delete=cap('on_comment_delete'),
    ):
        yield cap


# ── PostService.create — fan-out present ──────────────────────

def test_post_create_fires_on_new_post_with_community_name(fresh_db, rt):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    com = _seed_community(db, alice.id)

    from integrations.social.services import PostService
    post = PostService.create(
        db, alice, 'hello', content='body', community_name=com.name)
    db.commit()

    calls = rt.of('on_new_post')
    assert len(calls) == 1, "PostService.create must fan out exactly once"
    args, kwargs = calls[0]
    post_dict = args[0]
    assert post_dict['id'] == post.id
    assert post_dict['title'] == 'hello'
    assert kwargs.get('community_name') == com.name


def test_post_create_without_community_still_fans_out(fresh_db, rt):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')

    from integrations.social.services import PostService
    PostService.create(db, alice, 'standalone', content='x')
    db.commit()

    calls = rt.of('on_new_post')
    assert len(calls) == 1
    assert calls[0][1].get('community_name') is None


# ── PostService.update — fan-out only on real change ──────────

def test_post_update_fires_on_post_update_when_changed(fresh_db, rt):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    com = _seed_community(db, alice.id)

    from integrations.social.services import PostService
    post = PostService.create(db, alice, 'orig', content='a',
                              community_name=com.name)
    db.commit()
    rt.calls.clear()  # drop the create event

    PostService.update(db, post, title='edited', actor_id=alice.id)
    db.commit()

    calls = rt.of('on_post_update')
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0]['title'] == 'edited'
    assert kwargs.get('community_name') == com.name


def test_post_update_no_op_does_not_fan_out(fresh_db, rt):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')

    from integrations.social.services import PostService
    post = PostService.create(db, alice, 'orig', content='a')
    db.commit()
    rt.calls.clear()

    PostService.update(db, post, title='orig', content='a', actor_id=alice.id)
    db.commit()

    # No real changes → no fan-out.  Audit table also stays empty
    # (already covered by the audit test suite).
    assert rt.of('on_post_update') == []


# ── PostService.delete — pre-mutation snapshot ────────────────

def test_post_delete_fires_with_pre_delete_snapshot(fresh_db, rt):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    com = _seed_community(db, alice.id)

    from integrations.social.services import PostService
    post = PostService.create(db, alice, 'doomed', content='x',
                              community_name=com.name)
    db.commit()
    rt.calls.clear()
    pid = post.id

    PostService.delete(db, post, actor_id=alice.id)
    db.commit()

    calls = rt.of('on_post_delete')
    assert len(calls) == 1
    args, kwargs = calls[0]
    payload = args[0]
    assert payload['id'] == pid
    assert payload['author_id'] == alice.id
    assert payload['is_deleted'] is True
    # Community name still threaded so cached clients in the room
    # remove the entry too, not just the global feed.
    assert kwargs.get('community_name') == com.name


# ── CommentService.create — #49 fix ───────────────────────────

def test_comment_create_fires_on_new_comment(fresh_db, rt):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    bob = _seed_user(db, 'bob')
    com = _seed_community(db, alice.id)

    from integrations.social.services import PostService, CommentService
    post = PostService.create(db, alice, 'p1', content='x',
                              community_name=com.name)
    db.commit()
    rt.calls.clear()

    comment = CommentService.create(db, post, bob, 'first comment')
    db.commit()

    # Pre-fix: this was 0 — the fan-out only happened from
    # external_bot_bridge, human API comments were dark.  Post-fix:
    # service-level wiring fires exactly once for every caller.
    calls = rt.of('on_new_comment')
    assert len(calls) == 1, (
        "CommentService.create must fan out on_new_comment exactly "
        "once — pre-#49 it didn't fire at all from this path"
    )
    args, kwargs = calls[0]
    assert args[0]['id'] == comment.id
    assert args[0]['content'] == 'first comment'
    # Community routing threaded so comment lands on community.message
    # subscribers, not the dead per-post topic.
    assert kwargs.get('community_name') == com.name


def test_comment_create_on_community_less_post_still_fans_out(fresh_db, rt):
    # Post without community → community_name=None → realtime helper
    # will skip the community.message publish but still gets called
    # so future routing (e.g. DM-style direct fan-out) can be added
    # in one place.
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    bob = _seed_user(db, 'bob')

    from integrations.social.services import PostService, CommentService
    post = PostService.create(db, alice, 'standalone', content='x')
    db.commit()
    rt.calls.clear()

    CommentService.create(db, post, bob, 'orphan comment')
    db.commit()

    calls = rt.of('on_new_comment')
    assert len(calls) == 1
    assert calls[0][1].get('community_name') is None


# ── CommentService.delete — snapshot before flip ──────────────

def test_comment_delete_fires_with_pre_delete_snapshot(fresh_db, rt):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    com = _seed_community(db, alice.id)

    from integrations.social.services import PostService, CommentService
    post = PostService.create(db, alice, 'p', content='x',
                              community_name=com.name)
    comment = CommentService.create(db, post, alice, 'will-be-deleted')
    db.commit()
    rt.calls.clear()
    cid = comment.id

    CommentService.delete(db, comment, actor_id=alice.id)
    db.commit()

    calls = rt.of('on_comment_delete')
    assert len(calls) == 1
    args, kwargs = calls[0]
    payload = args[0]
    assert payload['id'] == cid
    assert payload['post_id'] == post.id
    assert payload['is_deleted'] is True
    assert kwargs.get('community_name') == com.name


# ── Fan-out failure must NOT block the user action ────────────

def test_fan_out_failure_does_not_block_create(fresh_db, monkeypatch):
    """If realtime helpers raise (WAMP down, transport error), the
    DB mutation must still succeed.  Operators monitor fan-out
    health via the publish counters / dashboards, not by 500-ing
    user-facing endpoints."""
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')

    from integrations.social import realtime as realtime_mod

    def boom(*a, **kw):
        raise RuntimeError("simulated WAMP outage")

    monkeypatch.setattr(realtime_mod, 'on_new_post', boom)

    from integrations.social.services import PostService
    # Must not raise.
    post = PostService.create(db, alice, 'survives', content='x')
    db.commit()
    # Mutation actually persisted.
    assert post.id is not None
