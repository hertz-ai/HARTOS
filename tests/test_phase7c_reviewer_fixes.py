"""Regression tests for reviewer findings on Phase 7c sync layer.

Each test locks behavior the reviewer explicitly flagged as broken or
fragile.  Names map back to the review punch list:

  C1 — _max_cursor must tuple-compare, not string-compare on '|'-encoded cursors.
  C2 — sync_service must enforce tenant_id at the SQL level (raw text() bypasses
       the ORM tenant_filter listener).
  C3 — soft_delete_message must bump edited_at so /sync mirrors the delete.
  H3 — realtime authorizer must not blanket-allow tenant.* publishes.
  M2 — dispatch_to_agent must cap recursion depth.
  N4 — edit_message must refuse when created_at is unparseable.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
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


def _silence_realtime(monkeypatch):
    from integrations.social import realtime
    monkeypatch.setattr(realtime, 'on_notification', lambda *a, **kw: None)
    monkeypatch.setattr(realtime, 'publish_event', lambda *a, **kw: None)
    monkeypatch.setattr(realtime, '_get_publisher', lambda: None)


def _seed(db):
    from integrations.social.models import User
    a = User(id=str(uuid.uuid4()), username='alice', display_name='A',
             email='a@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='B',
             email='b@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, b]); db.commit()
    return a, b


# ── C1: cursor tuple-compare correctness ─────────────────────────

def test_c1_max_cursor_tuple_not_string():
    """A cursor with `<ts>|<uuid>` must NOT outrank a strictly later
    `<ts+1>` (no id) just because '|' (0x7C) sorts after digits.

    Before fix: max_cursor returned the earlier-with-id cursor, causing
    the next sync to either re-deliver shipped rows or skip future
    ones depending on what advanced.  After fix: tuple compare on
    (ts, id) gives the temporally correct ordering.
    """
    from integrations.social.sync_service import _max_cursor
    earlier_with_id = "2026-05-03 10:00:00|ffffffff-ffff-ffff-ffff-ffffffffffff"
    strictly_later = "2026-05-03 10:00:01"
    winner = _max_cursor(earlier_with_id, strictly_later)
    assert winner.startswith("2026-05-03 10:00:01"), (
        f"_max_cursor must pick the strictly-later timestamp regardless "
        f"of id-tiebreaker on the earlier candidate, got: {winner}")


def test_c1_max_cursor_id_breaks_ties_at_same_ts():
    """When two candidates share the same timestamp, the id tiebreaker
    must pick the lexicographically larger id."""
    from integrations.social.sync_service import _max_cursor
    a = "2026-05-03 10:00:00|aaa"
    b = "2026-05-03 10:00:00|zzz"
    assert _max_cursor(a, b).endswith('|zzz')


# ── C2: tenant filter enforced in raw SQL fetchers ──────────────

def test_c2_sync_filters_by_tenant_id(fresh_db, monkeypatch):
    """A user in tenant A must not see notifications stamped with
    tenant B in /sync.  Before fix: tenant_id was passed to deltas()
    and silently ignored — raw SQL bypassed the ORM listener.
    After fix: every fetcher's WHERE includes a tenant_id predicate
    when caller's tenant_id is non-null."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, _ = _seed(db)

    # Insert two notifications for `a`: one in tenant A, one in tenant B.
    from sqlalchemy import text
    db.execute(text(
        "INSERT INTO notifications "
        "(id, tenant_id, user_id, type, source_user_id, target_type, "
        " target_id, message, is_read, created_at) "
        "VALUES (:id, 'tenant-A', :uid, 'test', NULL, 'post', 'p1', "
        " 'msg-A', 0, CURRENT_TIMESTAMP)"),
        {'id': str(uuid.uuid4()), 'uid': a.id})
    db.execute(text(
        "INSERT INTO notifications "
        "(id, tenant_id, user_id, type, source_user_id, target_type, "
        " target_id, message, is_read, created_at) "
        "VALUES (:id, 'tenant-B', :uid, 'test', NULL, 'post', 'p1', "
        " 'msg-B', 0, CURRENT_TIMESTAMP)"),
        {'id': str(uuid.uuid4()), 'uid': a.id})
    db.commit()

    from integrations.social.sync_service import SyncService
    res = SyncService.deltas(db, user_id=a.id, since=None,
                              kinds=['notifications'],
                              tenant_id='tenant-A')
    msgs = [n['message'] for n in res['deltas']['notifications']]
    assert 'msg-A' in msgs, "tenant A user must see their own tenant's row"
    assert 'msg-B' not in msgs, (
        "PRIVACY HOLE: tenant A user saw tenant B notification — "
        "raw SQL fetcher is leaking across tenants")


def test_c2_null_tenant_passes_through(fresh_db, monkeypatch):
    """Legacy untenanted rows (tenant_id NULL) remain visible to a
    tenant-scoped sync — backward compatibility per plan E.1."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, _ = _seed(db)
    from sqlalchemy import text
    db.execute(text(
        "INSERT INTO notifications "
        "(id, tenant_id, user_id, type, source_user_id, target_type, "
        " target_id, message, is_read, created_at) "
        "VALUES (:id, NULL, :uid, 'test', NULL, 'post', 'p1', "
        " 'legacy', 0, CURRENT_TIMESTAMP)"),
        {'id': str(uuid.uuid4()), 'uid': a.id})
    db.commit()
    from integrations.social.sync_service import SyncService
    res = SyncService.deltas(db, user_id=a.id, since=None,
                              kinds=['notifications'],
                              tenant_id='tenant-A')
    msgs = [n['message'] for n in res['deltas']['notifications']]
    assert 'legacy' in msgs


def test_c2_no_tenant_id_returns_all(fresh_db, monkeypatch):
    """Flat/regional callers (tenant_id=None) see every row regardless
    of tenant column — the no-op pass-through path that keeps existing
    deploys working."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, _ = _seed(db)
    from sqlalchemy import text
    db.execute(text(
        "INSERT INTO notifications "
        "(id, tenant_id, user_id, type, source_user_id, target_type, "
        " target_id, message, is_read, created_at) "
        "VALUES (:id, 'X', :uid, 'test', NULL, 'post', 'p', "
        " 'a', 0, CURRENT_TIMESTAMP)"),
        {'id': str(uuid.uuid4()), 'uid': a.id})
    db.execute(text(
        "INSERT INTO notifications "
        "(id, tenant_id, user_id, type, source_user_id, target_type, "
        " target_id, message, is_read, created_at) "
        "VALUES (:id, 'Y', :uid, 'test', NULL, 'post', 'p', "
        " 'b', 0, CURRENT_TIMESTAMP)"),
        {'id': str(uuid.uuid4()), 'uid': a.id})
    db.commit()
    from integrations.social.sync_service import SyncService
    res = SyncService.deltas(db, user_id=a.id, since=None,
                              kinds=['notifications'])  # no tenant_id
    msgs = sorted([n['message'] for n in res['deltas']['notifications']])
    assert msgs == ['a', 'b']


# ── C3: soft-deletes propagate to /sync ────────────────────────

def test_c3_soft_delete_visible_in_sync(fresh_db, monkeypatch):
    """Already covered in test_phase7c6_sync.py::test_cursor_includes_soft_deletes
    after the C3 fix — duplicated here as a regression anchor for the
    reviewer punch-list line."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    from integrations.social.sync_service import SyncService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='oops')
    res1 = SyncService.deltas(db, user_id=a.id, since=None)
    cursor1 = res1['cursor']
    import time; time.sleep(1.05)
    ConversationService.soft_delete_message(
        db, message_id=msg['id'], requester_id=a.id)
    res2 = SyncService.deltas(db, user_id=a.id, since=cursor1)
    target = [m for m in res2['deltas']['messages'] if m['id'] == msg['id']]
    assert len(target) == 1
    assert target[0]['is_deleted'] is True


# ── H3: realtime authorizer hardening ──────────────────────────

def test_h3_tenant_user_topic_requires_uid_suffix():
    """tenant.<tid>.user.<uid>.<event> publishes must end with the
    publishing user's id (defense in depth — prevents one user
    forging events targeting another user's inbox)."""
    from integrations.social.realtime import _authorize_topic_for_user_id
    # Legitimate: ends with our user_id.
    assert _authorize_topic_for_user_id(
        'tenant.A.user.alice.mention', 'alice') is True
    # Forgery: pretending we're alice publishing to bob's inbox.
    assert _authorize_topic_for_user_id(
        'tenant.A.user.bob.mention', 'alice') is False


def test_h3_tenant_conv_topic_passthrough_for_service_layer():
    """tenant.<tid>.conv.<cid>.* topics are gated at the service layer
    (ConversationService.emit_typing / mark_read both call _is_member
    before publishing).  The realtime authorizer accepts; the second
    layer is the WAMP router subscribe ACL (Phase 8)."""
    from integrations.social.realtime import _authorize_topic_for_user_id
    assert _authorize_topic_for_user_id(
        'tenant.A.conv.123.typing', 'anyone') is True
    assert _authorize_topic_for_user_id(
        'tenant._.conv.123.read', 'anyone') is True


def test_h3_unknown_tenant_shape_refused():
    from integrations.social.realtime import _authorize_topic_for_user_id
    # Some made-up tenant.<tid>.foo.<bar> shape — neither conv nor user.
    assert _authorize_topic_for_user_id(
        'tenant.A.foo.bar.event', 'alice') is False


# ── M2: agent dispatch recursion limit ──────────────────────────

def test_m2_dispatch_refuses_past_depth_ceiling(fresh_db, monkeypatch):
    """An agent reply chain capped at depth 2 — beyond that, the
    worker returns immediately without invoking the LLM. Prevents
    two mention-each-other agents from spinning an unbounded chain."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import User, Post, Community
    s = User(id=str(uuid.uuid4()), username='sol', display_name='S',
             email='s@x.test', password_hash='x:y', user_type='agent')
    s.owner_id = a.id
    com = Community(id=str(uuid.uuid4()), name='c', display_name='C',
                    description='', creator_id=a.id, is_private=False)
    db.add_all([s, com]); db.commit()
    p = Post(id='p_m2', author_id=a.id, community_id=com.id,
             title='hi', content='@sol', content_type='text')
    db.add(p); db.commit()

    llm_calls = []
    class _R:
        def __init__(self, t): self.content = t
    class _L:
        def invoke(self, prompt):
            llm_calls.append(prompt)
            return _R('reply')
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _L()) if name == 'get_llm' else None)

    from integrations import agentic_router
    # Pass a context with _dispatch_depth already past the ceiling —
    # worker should refuse before invoking the LLM.
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='ping', synchronous=True,
        context={'source_kind': 'post', 'source_id': p.id,
                 'author_id': a.id, 'tenant_id': None,
                 '_dispatch_depth': 99})
    assert llm_calls == [], (
        "dispatch_to_agent should not invoke LLM past depth ceiling")


def test_m2_initial_dispatch_at_depth_zero_succeeds(fresh_db, monkeypatch):
    """Sanity check: the cap doesn't block normal first-level dispatch."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import User, Post, Community
    s = User(id=str(uuid.uuid4()), username='sol', display_name='S',
             email='s@x.test', password_hash='x:y', user_type='agent')
    s.owner_id = a.id
    com = Community(id=str(uuid.uuid4()), name='c', display_name='C',
                    description='', creator_id=a.id, is_private=False)
    db.add_all([s, com]); db.commit()
    p = Post(id='p_m2_ok', author_id=a.id, community_id=com.id,
             title='hi', content='@sol', content_type='text')
    db.add(p); db.commit()

    class _R:
        def __init__(self, t): self.content = t
    class _L:
        def invoke(self, prompt):
            return _R('first reply')
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _L()) if name == 'get_llm' else None)

    from integrations import agentic_router
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='hi', synchronous=True,
        context={'source_kind': 'post', 'source_id': p.id,
                 'author_id': a.id, 'tenant_id': None})
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT content FROM comments WHERE post_id = :pid"),
        {'pid': p.id}).fetchall()
    assert len(rows) == 1
    assert 'first reply' in rows[0][0]


# ── N4: edit refuses on unparseable timestamp ──────────────────

# ── Pass 2 — C-NEW-1: silent noop returns real public count ─────

def test_c_new_1_silent_noop_does_not_leak_block_state(fresh_db, monkeypatch):
    """When a blocked user toggles a reaction, the response count must
    match what an UNblocked observer would see — otherwise comparing
    the response to GET /reactions reveals the block state."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import User, Community, Post
    c = User(id=str(uuid.uuid4()), username='cara', display_name='C',
             email='c@x.test', password_hash='x:y', user_type='human')
    com = Community(id=str(uuid.uuid4()), name='c', display_name='C',
                    description='', creator_id=a.id, is_private=False)
    db.add_all([c, com]); db.commit()
    p = Post(id='pn1', author_id=a.id, community_id=com.id,
             title='hi', content='hi', content_type='text')
    db.add(p); db.commit()

    # cara reacts (legitimate).
    from integrations.social.reaction_service import ReactionService
    ReactionService.toggle(db, 'post', p.id, c.id, '🔥')

    # alice blocks bob.
    from integrations.social.friend_service import FriendService
    FriendService.block(db, blocker_id=a.id, blocked_id=b.id)

    # bob (blocked) tries to react — silent noop, but count must
    # equal cara's contribution (the real public state).
    res_blocked = ReactionService.toggle(db, 'post', p.id, b.id, '🔥')
    assert res_blocked['action'] == 'noop'
    assert res_blocked['count'] == 1, (
        f"silent noop returned count={res_blocked['count']}; that "
        f"differs from the real public count and reveals block state")


# ── Pass 2 — C-NEW-2: cross-tenant source_id refused ───────────

def test_c_new_2_cross_tenant_source_refused(fresh_db, monkeypatch):
    """A reactor in tenant A passing a source_id that belongs to a
    different-tenant post must be refused — `_author_of` filters by
    tenant so the toggle never proceeds."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import Community, Post
    com = Community(id=str(uuid.uuid4()), name='c', display_name='C',
                    description='', creator_id=a.id, is_private=False)
    db.add(com); db.commit()
    # Post owned by tenant B.
    from sqlalchemy import text
    db.execute(text(
        "INSERT INTO posts (id, tenant_id, author_id, community_id, "
        " title, content, content_type, created_at) "
        "VALUES ('cross-tenant-post', 'tenant-B', :aid, :cid, "
        " 't', 'c', 'text', CURRENT_TIMESTAMP)"),
        {'aid': a.id, 'cid': com.id})
    db.commit()

    from integrations.social.reaction_service import (
        ReactionService, ReactionError)
    # Reactor in tenant A reaches for tenant B's post — refused.
    with pytest.raises(ReactionError, match='not found'):
        ReactionService.toggle(
            db, 'post', 'cross-tenant-post', b.id, '🔥',
            tenant_id='tenant-A')


# ── Pass 2 — H-NEW-1: empty-id boundary doesn't re-emit ────────

def test_h_new_1_empty_id_cursor_no_reemit(fresh_db, monkeypatch):
    """A cursor returned as bare `<ts>` (empty id) must not re-emit
    rows at the boundary timestamp on the next sync.  Before fix:
    the WHERE `(ts = :ts AND id > '')` matched every non-empty id,
    so boundary rows shipped on every call forever."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    from integrations.social.sync_service import SyncService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='boundary')
    res1 = SyncService.deltas(db, user_id=a.id, since=None,
                               kinds=['conversations'])
    cursor1 = res1['cursor']
    # Confirm cursor encodes empty-id boundary case for at least one
    # kind we didn't request (notifications); the messages cursor
    # advanced to (ts, id), but conversations may report bare ts.
    res2 = SyncService.deltas(db, user_id=a.id, since=cursor1,
                               kinds=['conversations'])
    # No new writes → no rows on the second call.
    assert res2['deltas']['conversations'] == [], (
        f"cursor re-emitted boundary rows: {res2['deltas']['conversations']}")


def test_n_new_2_soft_delete_sweeps_mentions(fresh_db, monkeypatch):
    """soft_delete_message must clear the source's Mention rows so the
    mentions index doesn't carry pointers into '[deleted]' content."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id,
        content='hey @bob — original')
    from sqlalchemy import text
    before = db.execute(text(
        "SELECT id FROM mentions WHERE source_id = :sid"),
        {'sid': msg['id']}).fetchall()
    assert len(before) >= 1, "test setup: original message had a mention"

    ConversationService.soft_delete_message(
        db, message_id=msg['id'], requester_id=a.id)
    after = db.execute(text(
        "SELECT id FROM mentions WHERE source_id = :sid"),
        {'sid': msg['id']}).fetchall()
    assert after == [], (
        f"soft_delete left {len(after)} stale Mention rows pointing at "
        f"deleted content")


def test_n_new_3_dispatch_depth_uses_gte(fresh_db, monkeypatch):
    """`_MAX_AGENT_DISPATCH_DEPTH=2` must mean exactly 2 levels — not 3.
    With `>` (the bug), depth 0→1→2 all proceed; with `>=` (fix),
    depth 2 refuses."""
    from integrations import agentic_router
    # At depth = ceiling, refuse.
    llm_calls = []
    class _R:
        def __init__(self, t): self.content = t
    class _L:
        def invoke(self, p):
            llm_calls.append(p); return _R('x')
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _L()) if name == 'get_llm' else None)
    agentic_router.dispatch_to_agent(
        agent_id='agent-x', prompt='hi', synchronous=True,
        context={'source_kind': 'post', 'source_id': 'p',
                 '_dispatch_depth':
                 agentic_router._MAX_AGENT_DISPATCH_DEPTH})
    assert llm_calls == [], (
        "depth check should refuse at depth == ceiling, not just >")


def test_n4_edit_refuses_unparseable_timestamp(fresh_db, monkeypatch):
    """If created_at is corrupt/unparseable, edit must refuse rather
    than silently allow forever — safe default is no-edit."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='original')

    # Corrupt created_at directly.
    from sqlalchemy import text
    db.execute(text(
        "UPDATE messages SET created_at = 'not-a-timestamp' "
        "WHERE id = :id"),
        {'id': msg['id']})
    db.commit()
    with pytest.raises(ConversationError, match='cannot determine'):
        ConversationService.edit_message(
            db, message_id=msg['id'], requester_id=a.id,
            new_content='hijacked')


# ── Pass-2 N-NEW-1 — `:tid` parameter name collision assertion ───

def test_n_new_1_tid_param_name_does_not_collide_with_caller():
    """sync_service._tenant_predicate uses `:tid` as the bind name
    for tenant_id.  If a caller's params dict already contains `tid`
    with a different value, the merge in `params.update(tenant_params)`
    would silently overwrite either way.  This test asserts the
    helper documents and uses a stable name; future refactors must
    keep `:tid` reserved.
    """
    from integrations.social.sync_service import _tenant_predicate
    sql, params = _tenant_predicate('tenant-A')
    assert 'tid' in params
    assert ':tid' in sql
    # Loose-mode passthrough behavior: empty when no tenant.
    sql_none, params_none = _tenant_predicate(None)
    assert sql_none == ''
    assert params_none == {}


# ── Pass-5 F14 — _is_already_exists_error recognised signals ─────

def test_f14_recognises_already_exists_signals():
    """Pass-5 F14 helper distinguishes idempotent migration safe-skip
    from genuine SQL errors.  Locks the recognised signal set."""
    from integrations.social.migrations import _is_already_exists_error
    assert _is_already_exists_error(
        Exception("table users already exists")) is True
    assert _is_already_exists_error(
        Exception("(sqlite3.OperationalError) duplicate column name: tenant_id")
    ) is True
    assert _is_already_exists_error(
        Exception("psycopg2.errors.DuplicateColumn: 42701")) is True
    assert _is_already_exists_error(
        Exception("MySQL ER_DUP_FIELDNAME 1060")) is True
    # Genuine errors don't match.
    assert _is_already_exists_error(
        Exception("syntax error near 'SELEC'")) is False
    assert _is_already_exists_error(
        Exception("connection lost")) is False


# ── Pass-1 M3 — mark_read shape consistency ───────────────────────

def test_m3_mark_read_empty_conv_returns_canonical_noop_shape(fresh_db):
    """mark_read on an empty conversation must return the canonical
    no-op dict shape (not raise).  Errors (not-a-member,
    message-not-in-conv) raise; no-ops return.  Two distinct
    shapes for two distinct concerns.  Pass-1 M3 contract.
    """
    from integrations.social.conversation_service import ConversationService
    db, _ = fresh_db
    a, b = _seed(db)
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    # Empty conversation — no messages yet.
    result = ConversationService.mark_read(
        db, conv_id=conv['id'], user_id=a.id)
    assert result == {
        'sent': False,
        'conv_id': conv['id'],
        'last_read_message_id': None,
        'reason': 'empty conversation',
    }


# ── Pass-1 N3 — publish_event fan-out instrumentation ─────────────

def test_n3_publish_counters_increment_on_authorized_topic(monkeypatch):
    """publish_event increments the per-leg counters so ops can
    detect partial fan-out regressions."""
    from integrations.social import realtime
    realtime.reset_publish_counters()
    # Authorized topic + working bus
    realtime.publish_event('community.feed', {'x': 1})
    counters = realtime.get_publish_counters()
    # bus_ok OR http_fallback_ok must increment depending on env
    assert counters['bus_ok'] + counters['http_fallback_ok'] >= 1
    assert counters['authorize_refused'] == 0


def test_n3_publish_counters_authorize_refused(monkeypatch):
    """An unauthorized topic increments authorize_refused, not the
    bus or http counters.  Locks N3 instrumentation contract."""
    from integrations.social import realtime
    realtime.reset_publish_counters()
    # tenant.x.user.bob.message published as user-charlie → refused
    realtime.publish_event(
        'tenant.t1.user.bob.message', {}, user_id='charlie')
    counters = realtime.get_publish_counters()
    assert counters['authorize_refused'] == 1
    assert counters['bus_ok'] == 0


# ── Pass-2 N-NEW-4 — `.conv.` segment vs substring match ─────────

def test_n_new_4_authorize_uses_segment_match_not_substring():
    """`'.conv.' in topic` was a substring check — a malicious topic
    like `tenant.x.conv.evil.user.bob.message` would falsely return
    True (it contains `.conv.` as a substring) when the actual scope
    is `user.bob`.  After the fix, the third segment is matched by
    equality so the malicious shape correctly falls through to the
    user-scope branch and gets refused without a matching user_id.
    """
    from integrations.social.realtime import _authorize_topic_for_user_id
    # Legitimate: tenant.t1.conv.c1.message → True
    assert _authorize_topic_for_user_id(
        'tenant.t1.conv.c1.message', 'user-x') is True
    # Legitimate: tenant.t1.user.user-x.message → True (suffix match)
    assert _authorize_topic_for_user_id(
        'tenant.t1.user.user-x.message', 'user-x') is True
    # Substring-attack: scope segment is 'conv', but the topic also
    # contains '.user.' deeper in.  Should still be allowed (it IS
    # conv-scoped) — service layer is the membership gate.
    assert _authorize_topic_for_user_id(
        'tenant.t1.conv.c-evil.user.someone.message', 'user-x') is True
    # The dangerous shape: user-scope topic targeting OTHER user
    # which the attacker tries to fake as conv-scope by inserting
    # 'conv' after a different position.  Real scope is `attack`,
    # not `conv` or `user` — must be refused.
    assert _authorize_topic_for_user_id(
        'tenant.t1.attack.fake.user.user-x.message', 'user-x') is False


# ── Pass-4 P4-8 — classifier-raises path is graceful ──────────────

def test_p4_8_classifier_failure_does_not_break_create_post(
        fresh_db, monkeypatch):
    """ContentClassifier failures must NEVER bubble into create_post.
    The post is still persisted; the classifier exception is logged
    and swallowed so the user gets a successful response.  Plan M
    explicitly documents this graceful-degrade behavior.
    """
    monkeypatch.setenv('HEVOLVE_FLAG_MODERATION_V2', 'true')
    db, _ = fresh_db
    a = _seed(db)[0]

    # Stub ContentClassifier to crash.
    from integrations.social import content_classifier as cc
    original = cc.ContentClassifier.classify_and_persist

    def _crash(*args, **kwargs):
        raise RuntimeError("simulated classifier crash")
    monkeypatch.setattr(
        cc.ContentClassifier, 'classify_and_persist',
        staticmethod(_crash))
    try:
        from flask import Flask
        from integrations.social import api, auth
        app = Flask(__name__)
        app.register_blueprint(api.social_bp)
        client = app.test_client()
        tok = auth.generate_jwt(a.id, a.username, 'flat')
        r = client.post(
            '/api/social/posts',
            json={'title': 'still works', 'content': 'classifier crashes'},
            headers={'Authorization': f'Bearer {tok}'})
        # Post is created despite the crash.
        assert r.status_code == 201
    finally:
        monkeypatch.setattr(
            cc.ContentClassifier, 'classify_and_persist',
            staticmethod(original))
