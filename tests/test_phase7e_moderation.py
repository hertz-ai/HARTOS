"""Phase 7e — AI moderation classifier (post-DLP soft signal).

Plan reference: sunny-gliding-eich.md, Part E.11 + Part M.

Coverage:
  - Migration v50 creates content_moderation_decisions + posts.is_quarantined.
  - Classifier verdicts: allow / quarantine / block on the keyword
    rules (deterministic, no LLM dep in tests).
  - classify_and_persist writes the audit row + flips visibility
    (is_hidden for block, is_quarantined for quarantine).
  - moderator queue endpoint shape (list_quarantine_queue).
  - human_overrule appends + un-flips the post visibility correctly.
  - Flag-gated: create_post with moderation_v2 OFF → no rows written,
    no flips.  Existing pre-flag posts unchanged.
  - DLPEngine is unchanged: this is a SECOND layer below it (the
    existing PII tests still pass — verified by running the regression
    suite, locked here at the contract level by exercising the
    endpoint with non-PII content that the classifier handles).
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


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv('HEVOLVE_FLAG_MODERATION_V2', 'true')
    from flask import Flask
    from integrations.social import api
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    yield app.test_client(), fresh_db[0]


def _seed_user(db):
    from integrations.social.models import User
    u = User(id=str(uuid.uuid4()), username=f'u_{uuid.uuid4().hex[:6]}',
             display_name='U', email=f'u_{uuid.uuid4().hex[:6]}@x.test',
             password_hash='x:y', user_type='human')
    db.add(u)
    db.commit()
    return u


def _make_post(db, author_id, content='hello world'):
    from integrations.social.models import Post
    p = Post(id=str(uuid.uuid4()), author_id=author_id,
             title='t', content=content, content_type='text')
    db.add(p)
    db.commit()
    return p


# ── Migration ───────────────────────────────────────────────────────

def test_v50_creates_decisions_table_and_quarantine_column(fresh_db):
    from sqlalchemy import text
    db, _ = fresh_db
    rows = db.execute(text(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name = 'content_moderation_decisions'"
    )).fetchall()
    assert rows
    cols = db.execute(text(
        "PRAGMA table_info(posts)")).fetchall()
    col_names = {c[1] for c in cols}
    assert 'is_quarantined' in col_names


# ── Classifier verdicts (pure compute) ─────────────────────────────

def test_clean_content_classified_as_allow():
    from integrations.social.content_classifier import ContentClassifier
    _scores, decision, _conf = ContentClassifier.classify(
        'I love photographing Saturn at night.', prefer_llm=False)
    assert decision == 'allow'


def test_self_harm_quarantines():
    from integrations.social.content_classifier import ContentClassifier
    _scores, decision, conf = ContentClassifier.classify(
        'I keep cutting myself when I am alone.', prefer_llm=False)
    # self_harm weight 0.85 → above QUARANTINE_THRESHOLD 0.55, below
    # BLOCK_THRESHOLD 0.85 (boundary) — falls into quarantine OR block.
    # The keyword rule weight is exactly 0.85 = BLOCK_THRESHOLD, so
    # 'block' is the expected category since `>=` is the gate.
    assert decision in ('quarantine', 'block')
    assert conf >= 0.55


def test_csam_keyword_blocks_at_high_confidence():
    """sexual_minors at weight 0.95 is above the 0.85 BLOCK_THRESHOLD."""
    from integrations.social.content_classifier import ContentClassifier
    _scores, decision, conf = ContentClassifier.classify(
        'this is csam material', prefer_llm=False)
    assert decision == 'block'
    assert conf >= 0.85


def test_borderline_quarantines():
    """'porn' (weight 0.7) → above QUARANTINE_THRESHOLD 0.55 but
    in QUARANTINE_GREY only (not BLOCK_PROTECTED), so quarantine."""
    from integrations.social.content_classifier import ContentClassifier
    _scores, decision, _conf = ContentClassifier.classify(
        'check out this porn site', prefer_llm=False)
    assert decision == 'quarantine'


# ── classify_and_persist side effects ───────────────────────────────

def test_persist_writes_decision_row_for_allow(fresh_db):
    from sqlalchemy import text
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db
    u = _seed_user(db)
    p = _make_post(db, u.id, content='clean post')
    ContentClassifier.classify_and_persist(
        db, source_kind='post', source_id=p.id, content='clean post',
        prefer_llm=False)
    rows = db.execute(text(
        "SELECT decision, confidence FROM content_moderation_decisions "
        "WHERE source_id = :id"),
        {'id': p.id}).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'allow'


def test_persist_block_flips_is_hidden(fresh_db):
    from sqlalchemy import text
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db
    u = _seed_user(db)
    p = _make_post(db, u.id, content='csam content')
    ContentClassifier.classify_and_persist(
        db, source_kind='post', source_id=p.id, content='csam content',
        prefer_llm=False)
    row = db.execute(text(
        "SELECT is_hidden, is_quarantined FROM posts WHERE id = :id"),
        {'id': p.id}).fetchone()
    assert row[0] == 1   # is_hidden flipped
    assert row[1] == 0   # is_quarantined NOT flipped


def test_persist_quarantine_flips_is_quarantined_not_hidden(fresh_db):
    from sqlalchemy import text
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db
    u = _seed_user(db)
    p = _make_post(db, u.id, content='violent murder threat')
    ContentClassifier.classify_and_persist(
        db, source_kind='post', source_id=p.id,
        content='violent murder threat', prefer_llm=False)
    row = db.execute(text(
        "SELECT is_hidden, is_quarantined FROM posts WHERE id = :id"),
        {'id': p.id}).fetchone()
    assert row[0] == 0   # is_hidden unchanged (visible to mods at least)
    assert row[1] == 1   # is_quarantined flipped


def test_persist_allow_no_flips(fresh_db):
    from sqlalchemy import text
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db
    u = _seed_user(db)
    p = _make_post(db, u.id, content='happy birthday')
    ContentClassifier.classify_and_persist(
        db, source_kind='post', source_id=p.id, content='happy birthday',
        prefer_llm=False)
    row = db.execute(text(
        "SELECT is_hidden, is_quarantined FROM posts WHERE id = :id"),
        {'id': p.id}).fetchone()
    assert row[0] == 0
    assert row[1] == 0


# ── Moderator queue ─────────────────────────────────────────────────

def test_quarantine_queue_lists_pending(fresh_db):
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db
    u = _seed_user(db)
    # One quarantine, one allow
    p1 = _make_post(db, u.id, content='violent threat')
    p2 = _make_post(db, u.id, content='hello world')
    ContentClassifier.classify_and_persist(
        db, 'post', p1.id, 'violent threat', prefer_llm=False)
    ContentClassifier.classify_and_persist(
        db, 'post', p2.id, 'hello world', prefer_llm=False)
    queue = ContentClassifier.list_quarantine_queue(db)
    ids = {q['source_id'] for q in queue}
    assert p1.id in ids
    assert p2.id not in ids


def test_human_overrule_clears_quarantine_on_allow(fresh_db):
    from sqlalchemy import text
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db
    mod = _seed_user(db)
    author = _seed_user(db)
    p = _make_post(db, author.id, content='violent threat')
    decision = ContentClassifier.classify_and_persist(
        db, 'post', p.id, 'violent threat', prefer_llm=False)
    # Mod overrules → human_decision='allow' → is_quarantined cleared
    ContentClassifier.human_overrule(
        db, decision['decision_id'], mod.id, 'allow')
    row = db.execute(text(
        "SELECT is_hidden, is_quarantined FROM posts WHERE id = :id"),
        {'id': p.id}).fetchone()
    assert row[0] == 0
    assert row[1] == 0


def test_human_overrule_unknown_decision_raises():
    """Defensive: a typo in human_decision must not silently corrupt
    the audit log."""
    from integrations.social.content_classifier import ContentClassifier
    with pytest.raises(ValueError):
        ContentClassifier.human_overrule(
            db=None, decision_id='x', reviewer_id='y',
            human_decision='god_mode')


# ── Flag-off bit-for-bit identical ─────────────────────────────────

def test_create_post_no_decision_row_when_flag_off(fresh_db, monkeypatch):
    """moderation_v2 OFF → create_post writes the post but NO
    content_moderation_decisions row.  Pre-7e behavior preserved."""
    monkeypatch.delenv('HEVOLVE_FLAG_MODERATION_V2', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    from sqlalchemy import text
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    client = app.test_client()
    db = fresh_db[0]
    u = _seed_user(db)
    tok = auth.generate_jwt(u.id, u.username, 'flat')
    r = client.post('/api/social/posts',
                    json={'title': 'test', 'content': 'violent threat'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    # No decision row should exist
    rows = db.execute(text(
        "SELECT 1 FROM content_moderation_decisions")).fetchall()
    assert rows == []


def test_create_post_writes_decision_when_flag_on(app_client):
    client, db = app_client
    u = _seed_user(db)
    from sqlalchemy import text
    from integrations.social import auth
    tok = auth.generate_jwt(u.id, u.username, 'flat')
    r = client.post('/api/social/posts',
                    json={'title': 'test', 'content': 'happy clean post'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    rows = db.execute(text(
        "SELECT decision FROM content_moderation_decisions")).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'allow'


# ── Pass-4 P4-10: ordering — post + decision land atomically ────────

def test_create_post_block_decision_flips_is_hidden_atomically(app_client):
    """Pass-4 P4-10: classifier runs with commit=False so the post
    row + decision row + is_hidden flip all land in one transaction.
    A reader querying the post immediately after the response can
    never see is_hidden=False."""
    from sqlalchemy import text
    client, db = app_client
    u = _seed_user(db)
    from integrations.social import auth
    tok = auth.generate_jwt(u.id, u.username, 'flat')
    # Use a CSAM keyword that triggers BLOCK (block_protected, weight 0.95)
    r = client.post('/api/social/posts',
                    json={'title': 'bad', 'content': 'this is csam'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    pid = r.get_json()['data']['id']
    row = db.execute(text(
        "SELECT is_hidden FROM posts WHERE id = :id"),
        {'id': pid}).fetchone()
    assert row[0] == 1, (
        "Pass-4 P4-10: classifier-flipped is_hidden must be visible "
        "to the very next read in the same DB session — proves the "
        "decision row + flip + post insert all share one transaction.")


def test_create_comment_writes_decision_when_flag_on(app_client):
    """Pass-4: classifier wired into create_comment too."""
    from sqlalchemy import text
    client, db = app_client
    u = _seed_user(db)
    p = _make_post(db, u.id, content='clean post')
    from integrations.social import auth
    tok = auth.generate_jwt(u.id, u.username, 'flat')
    r = client.post(f'/api/social/posts/{p.id}/comments',
                    json={'content': 'clean reply'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    rows = db.execute(text(
        "SELECT decision, source_kind FROM content_moderation_decisions "
        "WHERE source_kind = 'comment'")).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'allow'


# ── Pass-4 P4-9: prompt-injection boundary marker ──────────────────

def test_llm_prompt_uses_random_boundary_marker(monkeypatch):
    """Pass-4 P4-9: each _classify_via_llm call wraps user content
    in a per-request random marker the user can't predict, so a
    prerecorded prompt-injection payload can't survive across runs.
    Verify the boundary marker appears twice (open + close) and is
    different on each call.
    """
    from integrations.social import content_classifier as cc
    captured = []

    # Stub the gateway to capture the prompt instead of calling LLM.
    fake_gateway_complete = lambda prompt, **kw: (
        captured.append(prompt) or
        '{"hate":0.0,"harassment":0.0,"sexual":0.0,"sexual_minors":0.0,'
        '"violence":0.0,"self_harm":0.0}')
    fake_module = type('M', (), {'gateway_complete': fake_gateway_complete})
    monkeypatch.setitem(__import__('sys').modules,
                        'integrations.providers.gateway', fake_module)

    cc._classify_via_llm('first call content')
    cc._classify_via_llm('second call content')

    assert len(captured) == 2
    # Each prompt mentions the boundary marker three times: once in
    # the instructions naming the markers, then once as opening
    # delimiter, then once as closing.  All three are the same per
    # call; markers differ between calls (per-request randomness).
    import re as _re
    m1 = _re.findall(r'<<<([0-9a-f]{16})>>>', captured[0])
    m2 = _re.findall(r'<<<([0-9a-f]{16})>>>', captured[1])
    assert len(m1) >= 2 and len(set(m1)) == 1
    assert len(m2) >= 2 and len(set(m2)) == 1
    assert m1[0] != m2[0], (
        "boundary markers must be per-request random — same marker "
        "across calls defeats the injection defense")


def test_llm_classifier_falls_back_on_malformed_response(monkeypatch):
    """Pass-4 P4-12: malformed LLM JSON → return None → caller falls
    back to keyword classifier."""
    from integrations.social import content_classifier as cc
    fake_module = type('M', (), {
        'gateway_complete': lambda prompt, **kw: 'not JSON at all'})
    monkeypatch.setitem(__import__('sys').modules,
                        'integrations.providers.gateway', fake_module)
    result = cc._classify_via_llm('hello')
    assert result is None


def test_llm_classifier_clamps_scores_outside_0_1(monkeypatch):
    """Defense: an LLM that returns 1.5 or -0.3 must not poison the
    decision threshold.  _classify_via_llm clamps to [0, 1]."""
    from integrations.social import content_classifier as cc
    fake_module = type('M', (), {
        'gateway_complete': lambda prompt, **kw: (
            '{"hate": 1.5, "harassment": -0.3, "sexual": 0.5, '
            '"sexual_minors": 0.0, "violence": 0.0, "self_harm": 0.0}')})
    monkeypatch.setitem(__import__('sys').modules,
                        'integrations.providers.gateway', fake_module)
    result = cc._classify_via_llm('hello')
    assert result is not None
    assert 0.0 <= result['hate'] <= 1.0
    assert 0.0 <= result['harassment'] <= 1.0
    assert result['sexual'] == 0.5


# ── Pass-4 P4-7: keyword fallback no longer claims hate coverage ───

def test_keyword_fallback_does_not_match_hate_via_placeholder():
    """Pass-4 P4-7: dropped placeholder slur1/slur2 + 'hate' literal
    rule.  Verify the word 'hate' (e.g. 'I hate Mondays') no longer
    triggers a false positive on the hate category."""
    from integrations.social.content_classifier import ContentClassifier
    scores, decision, _ = ContentClassifier.classify(
        'I hate Mondays', prefer_llm=False)
    assert scores['hate'] == 0.0
    assert decision == 'allow'


# ── Pass-4 P4-14: per-tenant rules registry ──────────────────────

def test_p4_14_register_tenant_rules_overrides_default():
    """A tenant can register custom keyword rules that take priority
    over the module-level defaults."""
    import re as _re
    from integrations.social.content_classifier import (
        ContentClassifier, register_tenant_rules)

    # Default rules don't match 'banana' as anything
    scores, _, _ = ContentClassifier.classify(
        'banana republic', prefer_llm=False)
    assert scores['violence'] == 0.0

    # Register a tenant-specific rule
    custom_rules = (
        ('violence', _re.compile(r'\bbanana\b', _re.I), 0.7),
    )
    try:
        register_tenant_rules('tenant-fruity', custom_rules)
        scores, decision, _ = ContentClassifier.classify(
            'banana republic', prefer_llm=False, tenant_id='tenant-fruity')
        assert scores['violence'] == 0.7
        assert decision == 'quarantine'

        # Different tenant still sees default behavior
        scores_default, _, _ = ContentClassifier.classify(
            'banana republic', prefer_llm=False, tenant_id='tenant-other')
        assert scores_default['violence'] == 0.0
    finally:
        # Clean up — clear override
        register_tenant_rules('tenant-fruity', ())


def test_p4_14_register_tenant_rules_clears_with_empty_tuple():
    """Passing rules=() reverts the tenant to default behavior."""
    import re as _re
    from integrations.social.content_classifier import (
        ContentClassifier, register_tenant_rules)
    custom = (('violence', _re.compile(r'\bzebra\b', _re.I), 0.7),)
    register_tenant_rules('tenant-zoo', custom)
    scores, _, _ = ContentClassifier.classify(
        'zebra crossing', prefer_llm=False, tenant_id='tenant-zoo')
    assert scores['violence'] == 0.7
    # Clear
    register_tenant_rules('tenant-zoo', ())
    scores2, _, _ = ContentClassifier.classify(
        'zebra crossing', prefer_llm=False, tenant_id='tenant-zoo')
    assert scores2['violence'] == 0.0


def test_p4_14_register_tenant_rules_requires_tenant_id():
    from integrations.social.content_classifier import register_tenant_rules
    with pytest.raises(ValueError):
        register_tenant_rules('', ())
