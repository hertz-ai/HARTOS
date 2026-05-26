"""Phase 7c — agent dispatch coverage gaps.

Plan reference: sunny-gliding-eich.md, Part W.1 (gaps #233 + #234).

Two paths in `agentic_router.dispatch_to_agent` had no e2e exerciser:

  - **#233** async daemon-thread mode (`synchronous=False`).  Existing
    test_phase7b_mentions covers `synchronous=True` only, because
    threading harness is flake-prone.  Here we use `threading.Event`
    coordination so the worker signals completion exactly once, and
    the test waits on the Event with a hard timeout.  No polling, no
    sleeps — the assertion fires the moment the worker commits.

  - **#234** `source_kind='comment'` nested reply path.  The branch
    exists in `_post_agent_reply` but had no exerciser.  Verifies the
    reply lands as a Comment authored by the agent with `parent_id`
    set to the source comment id (Reddit-style nested thread shape).

Both tests stub `core.safe_hartos_attr.safe_hartos_attr('get_llm')`
to return a fixed-text fake LLM, matching the existing pattern in
test_phase7b_mentions.test_dispatch_to_agent_posts_comment_when_llm_replies.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Fixtures (mirror test_phase7b_mentions for consistency) ────────

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
    """Block every outbound publish path so background workers don't
    hit real HTTP / WAMP — same pattern as test_phase7b_mentions."""
    from integrations.social import realtime
    monkeypatch.setattr(realtime, 'publish_event',
                        lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(realtime, 'on_notification',
                        lambda *a, **kw: None, raising=False)


def _seed(db):
    """Alice (human), solar-architect (agent owned by Alice), Bob, community."""
    from integrations.social.models import User, Community
    a = User(id=str(uuid.uuid4()), username='alice', display_name='Alice',
             email='alice@x.test', password_hash='x:y', user_type='human')
    s = User(id=str(uuid.uuid4()), username='solar-architect',
             display_name='Solar Architect', email='sa@x.test',
             password_hash='x:y', user_type='agent')
    s.owner_id = a.id
    b = User(id=str(uuid.uuid4()), username='bob_p', display_name='Bob',
             email='b@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, s, b])
    c = Community(id=str(uuid.uuid4()), name='test-com',
                  display_name='Test', description='desc',
                  creator_id=a.id, is_private=False)
    db.add(c)
    db.commit()
    return a, s, b, c


# ── #233 async daemon-thread mode ──────────────────────────────────

def test_dispatch_to_agent_async_completes_via_daemon_thread(
        fresh_db, monkeypatch):
    """Default mode (synchronous=False) spawns a daemon thread.

    Use a `threading.Event` set by the fake LLM's invoke() so we
    can wait on completion deterministically — no sleep, no polling.
    Hard timeout 5s — if the thread never fires the Event, the test
    fails fast rather than hanging CI.

    After the Event, we additionally wait for the worker thread to
    join (so the comment row is committed) before asserting on the DB.
    """
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, s, _b, c = _seed(db)

    from integrations.social.models import Post
    post = Post(id='p_async_1', author_id=a.id, community_id=c.id,
                title='ask', content='@solar-architect what is 2+2?',
                content_type='text')
    db.add(post)
    db.commit()

    invoke_started = threading.Event()
    invoke_finished = threading.Event()

    class _FakeReply:
        def __init__(self, text):
            self.content = text

    class _FakeLLM:
        def invoke(self, _prompt):
            invoke_started.set()
            try:
                return _FakeReply('Async answer: four.')
            finally:
                invoke_finished.set()

    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _FakeLLM())
        if name == 'get_llm' else None)

    from integrations import agentic_router

    # Capture spawned threads so we can join them at the end.
    spawned = []
    real_thread = threading.Thread

    def _capture(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        spawned.append(t)
        return t

    monkeypatch.setattr(threading, 'Thread', _capture)

    # Default mode is async — synchronous omitted on purpose.
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='what is 2+2?',
        context={'source_kind': 'post', 'source_id': post.id,
                 'author_id': a.id, 'tenant_id': None})

    # Wait for the worker to finish (it sets invoke_finished after
    # the LLM call completes; CommentService.create runs immediately
    # after).  The thread.join() that follows pins to the actual
    # commit so the DB read below is deterministic.
    assert invoke_finished.wait(timeout=5.0), (
        "Worker thread never reached the LLM invoke — async "
        "dispatch failed to spawn or fire.")

    # Join all spawned threads so the worker finishes its commit.
    for t in spawned:
        t.join(timeout=5.0)
        assert not t.is_alive(), (
            f"Worker thread {t.name} did not exit within 5s; "
            "may indicate a hang in the post-LLM CommentService "
            "path or commit failure.")

    # Re-read from a fresh session — the worker uses its own
    # db_session() so the commit must be visible cross-session.
    from integrations.social.models import get_db
    obs = get_db()
    try:
        from sqlalchemy import text
        rows = obs.execute(text(
            "SELECT author_id, content FROM comments "
            "WHERE post_id = :pid"),
            {'pid': post.id}).fetchall()
        assert len(rows) == 1, (
            f"expected exactly 1 comment from async worker, "
            f"got {len(rows)}")
        assert rows[0][0] == s.id  # authored by the agent
        assert 'four' in rows[0][1].lower()
    finally:
        obs.close()


def test_dispatch_to_agent_async_thread_is_daemon(monkeypatch):
    """Worker threads must be daemon=True so process shutdown isn't
    blocked by an in-flight LLM call.  If a non-daemon thread ever
    creeps in via refactor, this catches it."""
    captured = []
    real_thread = threading.Thread

    def _capture(*args, **kwargs):
        captured.append(kwargs)
        # Don't actually start — we only care about the spec.
        t = real_thread(*args, **kwargs)
        return t

    monkeypatch.setattr(threading, 'Thread', _capture)

    from integrations import agentic_router
    # Mock get_llm to None so the worker exits quickly even if it runs.
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(sha, 'safe_hartos_attr', lambda name: None)

    agentic_router.dispatch_to_agent(
        agent_id='aid', prompt='hi',
        context={'source_kind': 'post', 'source_id': 'pid'})

    assert len(captured) == 1
    assert captured[0].get('daemon') is True


def test_dispatch_to_agent_async_swallows_worker_exceptions(
        fresh_db, monkeypatch):
    """A worker that crashes (e.g. DB transient error) must not bubble
    into the calling thread.  The dispatch is fire-and-forget; the
    Notification + Mention rows persisted upstream survive.
    """
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, s, _b, c = _seed(db)

    from integrations.social.models import Post
    post = Post(id='p_async_2', author_id=a.id, community_id=c.id,
                title='ask', content='hi', content_type='text')
    db.add(post)
    db.commit()

    crashed = threading.Event()

    class _CrashLLM:
        def invoke(self, _prompt):
            crashed.set()
            raise RuntimeError("simulated transient LLM crash")

    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _CrashLLM())
        if name == 'get_llm' else None)

    spawned = []
    real_thread = threading.Thread
    monkeypatch.setattr(threading, 'Thread',
                        lambda *a, **kw: spawned.append(real_thread(*a, **kw))
                        or spawned[-1])

    from integrations import agentic_router
    # Calling thread must NOT raise.
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='hi',
        context={'source_kind': 'post', 'source_id': post.id,
                 'tenant_id': None})

    assert crashed.wait(timeout=5.0)
    for t in spawned:
        t.join(timeout=5.0)

    # No comment should have been posted (LLM crashed pre-post).
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT 1 FROM comments WHERE post_id = :pid"),
        {'pid': post.id}).fetchall()
    assert rows == []


# ── #234 source_kind='comment' nested reply path ──────────────────

def test_dispatch_to_agent_replies_under_parent_comment(
        fresh_db, monkeypatch):
    """`source_kind='comment'` makes the agent's reply a nested reply
    (parent_id set to the source comment id).  Plan E.5: agents reply
    via the same CommentService.create path a human gets — no
    privileged code path."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, s, b, c = _seed(db)

    # Seed: Bob writes a top-level comment that mentions the agent.
    from integrations.social.models import Post, Comment
    post = Post(id='p_nested_1', author_id=a.id, community_id=c.id,
                title='topic', content='discuss', content_type='text')
    db.add(post)
    db.commit()
    parent_comment = Comment(
        id='c_nested_1', post_id=post.id, author_id=b.id,
        content='@solar-architect can you weigh in?',
        depth=0)
    db.add(parent_comment)
    db.commit()

    class _FakeReply:
        def __init__(self, text):
            self.content = text

    class _FakeLLM:
        def invoke(self, _prompt):
            return _FakeReply('Nested answer.')

    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _FakeLLM())
        if name == 'get_llm' else None)

    from integrations import agentic_router
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='@solar-architect can you weigh in?',
        synchronous=True,
        context={'source_kind': 'comment', 'source_id': parent_comment.id,
                 'author_id': b.id, 'tenant_id': None})

    # The agent's reply must be a Comment under the same post,
    # authored by the agent, with parent_id set to the source comment.
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT id, author_id, parent_id, content "
        "FROM comments WHERE post_id = :pid AND author_id = :aid"),
        {'pid': post.id, 'aid': s.id}).fetchall()
    assert len(rows) == 1, (
        f"expected exactly 1 nested reply by the agent, got {len(rows)}")
    reply_id, author_id, parent_id, content = rows[0]
    assert reply_id != parent_comment.id
    assert author_id == s.id
    assert parent_id == parent_comment.id, (
        "Pass-3 check: parent_id must point at the source comment, "
        "not at the post root — otherwise the reply renders as a "
        "sibling, not a nested child.")
    assert 'nested answer' in content.lower()


def test_dispatch_to_agent_skips_when_parent_comment_missing(
        fresh_db, monkeypatch):
    """If the source comment id doesn't exist (deleted between
    dispatch and worker), the worker logs and exits — no exception,
    no orphan reply."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    _a, s, _b, _c = _seed(db)

    class _FakeLLM:
        def invoke(self, _prompt):
            class _R:
                content = 'reply'
            return _R()

    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _FakeLLM())
        if name == 'get_llm' else None)

    from integrations import agentic_router
    # Source comment id doesn't exist — worker must no-op.
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='hi', synchronous=True,
        context={'source_kind': 'comment', 'source_id': 'does-not-exist',
                 'author_id': 'whoever', 'tenant_id': None})

    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT 1 FROM comments WHERE author_id = :aid"),
        {'aid': s.id}).fetchall()
    assert rows == []
