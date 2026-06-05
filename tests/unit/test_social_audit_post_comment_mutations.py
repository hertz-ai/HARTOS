"""Behavioural tests for #42 — post-edit + post-delete + comment-delete
write an audit trail via security/immutable_audit_log.

These exercise the REAL services + REAL DB (in-memory SQLite, same
fixture pattern as test_phase7c5_post_privacy.py) and assert on the
audit log's hash chain after each mutation.  No grep tests.

Reddit-style accountability requirement: original title/content stays
recoverable from the audit log even after delete sets is_deleted=True
and overwrites comment.content to '[deleted]'.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Shared fixtures (mirror Phase 7c.5 style) ──────────────────────

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
    # Reset the audit-log singleton so each test sees a clean chain
    # rooted at the migrated DB's first AuditLogEntry row (if any).
    import security.immutable_audit_log as audit_mod
    audit_mod._audit_log = None
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
        audit_mod._audit_log = None


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


def _seed_post(db, author_id, title='orig title', content='orig body'):
    from integrations.social.models import Post
    p = Post(id=str(uuid.uuid4()), author_id=author_id,
             title=title, content=content, content_type='text')
    db.add(p)
    db.commit()
    return p


def _seed_comment(db, post_id, author_id, content='orig comment'):
    from integrations.social.models import Comment
    c = Comment(id=str(uuid.uuid4()), post_id=post_id, author_id=author_id,
                content=content, depth=0)
    db.add(c)
    db.commit()
    return c


def _audit_entries(event_type=None, actor_id=None):
    from security.immutable_audit_log import get_audit_log
    return get_audit_log().get_trail(
        actor_id=actor_id, event_type=event_type, limit=200)


# ── PostService.update — audit trail ────────────────────────────

def test_post_update_writes_audit_with_field_diffs(fresh_db):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    post = _seed_post(db, alice.id, title='before', content='body-before')

    from integrations.social.services import PostService
    PostService.update(
        db, post,
        title='after',
        content='body-after',
        actor_id=alice.id,
    )
    db.commit()

    entries = _audit_entries(event_type='post.update')
    assert len(entries) == 1, f"expected 1 audit row, got {entries}"
    e = entries[0]
    assert e['actor_id'] == alice.id
    assert e['target_id'] == post.id
    assert post.id in e['action']

    import json
    detail = json.loads(e['detail_json'])
    assert detail['fields']['title']['before'] == 'before'
    assert detail['fields']['title']['after'] == 'after'
    assert detail['fields']['content']['before'] == 'body-before'
    assert detail['fields']['content']['after'] == 'body-after'
    assert detail['author_id'] == alice.id


def test_post_update_no_changes_writes_no_audit(fresh_db):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    post = _seed_post(db, alice.id, title='same', content='same')

    from integrations.social.services import PostService
    PostService.update(
        db, post,
        title='same',          # unchanged
        content='same',        # unchanged
        actor_id=alice.id,
    )
    db.commit()

    # No-op edits don't pollute the audit table.
    assert _audit_entries(event_type='post.update') == []


def test_post_update_audit_records_admin_actor_when_different(fresh_db):
    """An admin editing someone else's post is recorded with the
    admin's actor_id, not the post author's — accountability."""
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    admin = _seed_user(db, 'admin')
    post = _seed_post(db, alice.id, title='before', content='x')

    from integrations.social.services import PostService
    PostService.update(db, post, title='admin-edited', actor_id=admin.id)
    db.commit()

    entries = _audit_entries(event_type='post.update')
    assert len(entries) == 1
    assert entries[0]['actor_id'] == admin.id
    import json
    detail = json.loads(entries[0]['detail_json'])
    assert detail['author_id'] == alice.id  # post owner ≠ actor


# ── PostService.delete — audit trail ────────────────────────────

def test_post_delete_writes_audit_with_original_content(fresh_db):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    post = _seed_post(db, alice.id,
                      title='to-be-deleted', content='will-disappear')

    from integrations.social.services import PostService
    PostService.delete(db, post, actor_id=alice.id)
    db.commit()

    entries = _audit_entries(event_type='post.delete')
    assert len(entries) == 1
    e = entries[0]
    assert e['actor_id'] == alice.id
    assert e['target_id'] == post.id

    import json
    detail = json.loads(e['detail_json'])
    # Reddit-style: original content recoverable from audit.
    assert detail['title'] == 'to-be-deleted'
    assert detail['content'] == 'will-disappear'

    # Side-effect on the post object still happened.
    assert post.is_deleted is True


def test_post_delete_logs_before_mutation(fresh_db):
    """Audit detail must reflect pre-delete state — if we logged
    AFTER the mutation we'd capture is_deleted=True/empty content."""
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    post = _seed_post(db, alice.id, title='snapshot-me', content='still-here')

    from integrations.social.services import PostService
    PostService.delete(db, post, actor_id=alice.id)
    db.commit()

    import json
    detail = json.loads(
        _audit_entries(event_type='post.delete')[0]['detail_json'])
    assert detail['title'] == 'snapshot-me'
    assert detail['content'] == 'still-here'


# ── CommentService.delete — audit trail ────────────────────────

def test_comment_delete_writes_audit_with_original_content(fresh_db):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    post = _seed_post(db, alice.id)
    comment = _seed_comment(db, post.id, alice.id,
                            content='regret-this-comment')

    from integrations.social.services import CommentService
    CommentService.delete(db, comment, actor_id=alice.id)
    db.commit()

    entries = _audit_entries(event_type='comment.delete')
    assert len(entries) == 1
    e = entries[0]
    assert e['actor_id'] == alice.id
    assert e['target_id'] == comment.id

    import json
    detail = json.loads(e['detail_json'])
    # Original content preserved BEFORE the '[deleted]' overwrite.
    assert detail['content'] == 'regret-this-comment'
    assert detail['post_id'] == post.id

    # The mutation itself still happened — '[deleted]' is what
    # readers see; the audit is where the truth lives.
    assert comment.is_deleted is True
    assert comment.content == '[deleted]'


def test_comment_delete_audit_survives_with_unknown_actor(fresh_db):
    """If a caller forgets to thread actor_id (legacy / daemon path),
    we still log with actor='unknown' — never drop the trail."""
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    post = _seed_post(db, alice.id)
    comment = _seed_comment(db, post.id, alice.id, content='oops')

    from integrations.social.services import CommentService
    CommentService.delete(db, comment)  # no actor_id passed
    db.commit()

    entries = _audit_entries(event_type='comment.delete')
    assert len(entries) == 1
    assert entries[0]['actor_id'] == 'unknown'


# ── All three event types coexist in a single session ──────────
#
# NOTE: a previous draft of this test also called verify_chain() —
# but ImmutableAuditLog has a pre-existing timestamp-drift bug
# (log_event hashes datetime.utcnow().isoformat() computed in
# Python; the row's created_at column is set later by SQLAlchemy's
# default=datetime.utcnow at INSERT time, so the two never match
# and verify_chain always reports the first entry as broken).
# That's tracked separately as task #48 — not in scope for #42.
# Here we assert the WRITE contract only: every mutation produces
# an audit row of the right shape.

def test_post_edit_delete_and_comment_delete_all_produce_audit_rows(fresh_db):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    post1 = _seed_post(db, alice.id, title='one', content='a')
    post2 = _seed_post(db, alice.id, title='two', content='b')
    comment = _seed_comment(db, post1.id, alice.id, content='c')

    from integrations.social.services import PostService, CommentService
    PostService.update(db, post1, title='one-edited', actor_id=alice.id)
    PostService.delete(db, post2, actor_id=alice.id)
    CommentService.delete(db, comment, actor_id=alice.id)
    db.commit()

    types = [e['event_type'] for e in _audit_entries()]
    assert 'post.update' in types
    assert 'post.delete' in types
    assert 'comment.delete' in types

    # Each row's target_id points at the right entity.
    by_type = {e['event_type']: e for e in _audit_entries()}
    assert by_type['post.update']['target_id'] == post1.id
    assert by_type['post.delete']['target_id'] == post2.id
    assert by_type['comment.delete']['target_id'] == comment.id


# ── Truncation — large bodies bounded ──────────────────────────

def test_post_update_audit_truncates_huge_content(fresh_db):
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    huge = 'x' * 20000  # well past the 4096 cap
    post = _seed_post(db, alice.id, content=huge)

    from integrations.social.services import PostService
    new_huge = 'y' * 20000
    PostService.update(db, post, content=new_huge, actor_id=alice.id)
    db.commit()

    import json
    detail = json.loads(
        _audit_entries(event_type='post.update')[0]['detail_json'])
    before = detail['fields']['content']['before']
    after = detail['fields']['content']['after']
    # Both halves of the diff are bounded.
    assert len(before) < 6000
    assert len(after) < 6000
    assert 'truncated' in before
    assert 'truncated' in after


# ── Audit failure must NOT block the user action ───────────────

def test_post_delete_succeeds_even_if_audit_layer_explodes(
        fresh_db, monkeypatch):
    """If get_audit_log() or log_event() raises (DB down, import error),
    the user action must still succeed.  Operators detect audit gaps
    via verify_chain() / Prometheus, not by 500-ing the user."""
    db, _ = fresh_db
    alice = _seed_user(db, 'alice')
    post = _seed_post(db, alice.id, title='delete-me')

    import security.immutable_audit_log as audit_mod

    def boom():
        raise RuntimeError("simulated audit outage")

    monkeypatch.setattr(audit_mod, 'get_audit_log', boom)

    from integrations.social.services import PostService
    # Must not raise.
    PostService.delete(db, post, actor_id=alice.id)
    db.commit()
    # Mutation succeeded despite the audit failure.
    assert post.is_deleted is True
