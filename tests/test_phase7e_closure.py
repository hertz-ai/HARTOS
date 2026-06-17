"""Phase 7e — closure test suite (Ship #220).

Plan reference: sunny-gliding-eich.md, Part E.11 + Part M.

Purpose
-------
Phase 7e closure auditor — verifies that the documented contracts of the
content-moderation classifier are wired the way the plan promises, and
that DLP semantics are preserved exactly (the classifier is a SECOND
soft-signal layer below the FIRST binary DLP block/allow layer).

Scout findings drive the test shape:
  * `integrations/social/content_classifier.py` exists (432 LOC).
  * `ContentClassifier.classify_async` does NOT exist — only the
    synchronous `classify()` + `classify_and_persist()` methods are
    implemented.  The docstring at line 30 references `classify_async`
    as the flag-gate target, but no async wrapper was ever shipped.
  * `ContentModerationDecision` is created via raw SQL in migration
    v50 (NOT v30 as the original plan said) — there is no SQLAlchemy
    ORM model class for it in `_models_local.py`.
  * `posts.is_quarantined` column exists (added in v50 raw SQL) — but
    is NOT mapped on the ORM `Post` class either; the classifier
    writes/reads it via `text()` UPDATEs.  This is intentional but
    asymmetric with `Post.is_hidden` which IS mapped on the ORM.
  * `comments.is_quarantined` does NOT exist — classifier code
    explicitly notes that comments + messages aren't flagged today.
  * `Report.ai_*` columns (ai_classification, ai_confidence, auto_action,
    decision_id FK) do NOT exist.  Plan E.11 promised them; ship reality
    folded the same audit into `content_moderation_decisions` directly.
  * Existing coverage is consolidated in `test_phase7e_moderation.py`
    (no separate `test_classifier.py` or `test_dlp_unchanged.py` files).

This file exercises the four closure-critical contracts:

1. **classify_async** — xfail-marked test documents the gap.  Once
   somebody actually ships the async wrapper, this xfail will start
   xpassing and force someone to delete the xfail.

2. **DLPEngine.check_outbound unchanged** — the classifier ship plan
   promised the FIRST layer's PII-block contract is preserved.  Re-runs
   the existing DLPEngine contract (email + phone + ssn + credit card
   still block at 1.0 confidence, no false-positive on safe IPs).

3. **Classifier quarantine path** — when classifier verdict is
   quarantine on a 'hate' score above QUARANTINE_THRESHOLD, the
   `posts.is_quarantined` column flips to 1.  Note: hate has no
   keyword fallback (Pass-4 P4-7), so this test uses the LLM gateway
   mock to inject hate=0.86 (> BLOCK_THRESHOLD 0.85 keeps it in block
   territory if it's in BLOCK_PROTECTED — hate IS in BLOCK_PROTECTED).
   To exercise the quarantine path on hate exclusively, we need a
   hate score in [0.55, 0.85).  Below 0.85 means it falls through
   the BLOCK_PROTECTED gate but matches QUARANTINE_GREY → quarantine.

4. **ContentModerationDecision row inserted by classify_and_persist** —
   verifies the audit append-only row is written via mocked gateway
   (so no real 15-LLM call), with the expected schema columns.

The `classify_async` test is xfail because the method genuinely
doesn't exist.  All other tests pass.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest


# ── Path bootstrap (same pattern as tests/test_phase7e_moderation.py) ──
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Fixtures (mirror test_phase7e_moderation.py to stay drop-in compat) ──

@pytest.fixture
def fresh_db(monkeypatch):
    """Fresh in-memory sqlite + migrations applied.  Mirrors the fixture
    in test_phase7e_moderation.py so this closure file can be run
    independently or beside the main moderation suite without state
    bleed.  Resets the module-level engine + session factory so each
    test gets a clean schema.
    """
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


def _seed_user(db):
    """Minimal user — JWT not required for direct classify_and_persist
    tests; we only need FK satisfaction for the posts.author_id link."""
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


def _post_column_exists(db, column_name: str) -> bool:
    """SQLite-portable check: scan PRAGMA table_info to confirm a
    column was added by migration.  Used to skip the quarantine flip
    test if the column was never created (would happen if migration v50
    were rolled back or the closure auditor runs on an older schema)."""
    from sqlalchemy import text
    rows = db.execute(text("PRAGMA table_info(posts)")).fetchall()
    return any(r[1] == column_name for r in rows)


# ── 1. classify_async closure gap ──────────────────────────────────────

@pytest.mark.xfail(
    reason=(
        "Scout gap: ContentClassifier.classify_async does not exist.  "
        "Only sync classify() + classify_and_persist() are shipped at "
        "integrations/social/content_classifier.py.  The module "
        "docstring (line 30) references `classify_async` as the "
        "flag-gate no-op target, but no async wrapper was ever added.  "
        "This xfail documents the closure gap — once someone ships the "
        "async wrapper accepting (source_kind, source_id, content), "
        "this test will xpass and the xfail marker must be deleted."
    ),
    strict=True,
)
def test_classify_async_accepts_source_kind_source_id_content():
    """Phase 7e closure gap: the async wrapper promised in the module
    docstring (line 30 — `classify_async is a no-op` when flag off)
    does not exist.  When implemented, it should accept the same
    arguments as classify_and_persist + return an awaitable that
    writes a ContentModerationDecision row.
    """
    from integrations.social.content_classifier import ContentClassifier
    # The docstring promises this exists; assertion below makes the
    # gap discoverable by anyone running the closure suite.
    assert hasattr(ContentClassifier, 'classify_async'), (
        "ContentClassifier.classify_async missing — see scout report"
    )
    # If it ever lands, it must accept the documented arg shape.
    import inspect
    sig = inspect.signature(ContentClassifier.classify_async)
    params = set(sig.parameters.keys())
    expected = {'source_kind', 'source_id', 'content'}
    assert expected.issubset(params), (
        f"classify_async signature missing args: "
        f"expected {expected}, got {params}"
    )


# ── 2. DLP unchanged contract (Plan A.3 promise) ──────────────────────

class TestDLPUnchanged:
    """Phase 7e closure promise: the classifier is a SECOND layer below
    the FIRST DLPEngine PII-block layer.  DLP semantics are UNCHANGED.

    Re-asserts the pre-7e DLPEngine contract here so a future
    classifier refactor that accidentally touches dlp_engine.py
    (e.g., shared regex consolidation) trips this regression bar.
    """

    def test_email_pii_still_blocks(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        allowed, reason = dlp.check_outbound(
            'reach me at john.doe@example.com tomorrow')
        assert allowed is False, "email PII must block outbound"
        assert 'email' in reason
        # Confidence is asserted via the count present in the reason —
        # DLPEngine reports findings count, not a probability.  '1 items'
        # signals exactly one PII match at full confidence (binary gate).
        assert '1 items' in reason

    def test_phone_pii_still_blocks(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        allowed, reason = dlp.check_outbound('call me at 555-123-4567')
        assert allowed is False
        assert 'phone' in reason

    def test_ssn_pii_still_blocks(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        allowed, reason = dlp.check_outbound('SSN is 123-45-6789')
        assert allowed is False
        assert 'ssn' in reason

    def test_credit_card_still_blocks(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        allowed, reason = dlp.check_outbound(
            'card 4111 1111 1111 1111 expires 12/26')
        assert allowed is False
        assert 'credit_card' in reason

    def test_clean_text_passes(self):
        """No PII → allowed=True, empty reason.  The classifier soft
        signal must NEVER cause clean text to fail DLP — proves the
        layers are independent."""
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        allowed, reason = dlp.check_outbound(
            'I love photographing Saturn at night.')
        assert allowed is True
        assert reason == ''

    def test_safe_ip_addresses_not_flagged(self):
        """127.0.0.1 + 192.168.0.1 are in _SAFE_PATTERNS — must not
        trip the DLP.  Preserves pre-7e behavior for dev hostnames
        + LAN-flat deploys."""
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        allowed, _ = dlp.check_outbound('server at 127.0.0.1 is up')
        assert allowed is True

    def test_redact_preserves_non_pii_text(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        redacted = dlp.redact('Email john@example.com about Saturn.')
        assert '[EMAIL_REDACTED]' in redacted
        assert 'Saturn' in redacted
        assert 'john@example.com' not in redacted

    def test_scan_returns_typed_findings(self):
        """Pre-7e contract: scan() returns List[Tuple[str, str]] of
        (pii_type, matched_text).  A change in this shape would break
        every caller including the MCP sandbox."""
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        findings = dlp.scan('x@y.com and 555-123-4567')
        assert isinstance(findings, list)
        assert len(findings) >= 2
        for item in findings:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)  # pii_type
            assert isinstance(item[1], str)  # matched_text
        types = {t for t, _ in findings}
        assert 'email' in types
        assert 'phone' in types


# ── 3. ContentModerationDecision row written by classify_and_persist ──

def test_classify_and_persist_writes_decision_row_with_mock_gateway(
        fresh_db, monkeypatch):
    """Phase 7e closure promise: classify_and_persist accepts the
    (source_kind, source_id, content) tuple and inserts an audit row
    into content_moderation_decisions.  Mocked gateway so this test
    NEVER hits the real 15-LLM ensemble.

    Closes the scout gap that asked for classify_async; since async
    wasn't shipped, we lock the sync `classify_and_persist` contract
    that was actually shipped + actually wired into api.create_post.
    """
    from sqlalchemy import text
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db
    u = _seed_user(db)
    p = _make_post(db, u.id, content='clean photography post')

    # Mock the gateway so we never touch a real LLM.  Returns the
    # all-zero score JSON the classifier expects — this exercises the
    # full LLM path including the JSON parser + score-clamping.
    fake_gateway = type('M', (), {
        'gateway_complete': lambda prompt, **kw: (
            '{"hate":0.0,"harassment":0.0,"sexual":0.0,'
            '"sexual_minors":0.0,"violence":0.0,"self_harm":0.0}'
        )
    })
    monkeypatch.setitem(sys.modules,
                        'integrations.providers.gateway', fake_gateway)

    result = ContentClassifier.classify_and_persist(
        db, source_kind='post', source_id=p.id,
        content='clean photography post', prefer_llm=True)

    assert 'decision_id' in result
    assert result['source_kind'] == 'post'
    assert result['source_id'] == p.id
    assert result['decision'] == 'allow'

    rows = db.execute(text(
        "SELECT id, source_kind, source_id, decision, confidence, "
        "       classifications, classifier_model "
        "FROM content_moderation_decisions WHERE source_id = :id"),
        {'id': p.id}).fetchall()
    assert len(rows) == 1, "exactly one audit row per classify call"
    row = rows[0]
    assert row[1] == 'post'
    assert row[2] == p.id
    assert row[3] == 'allow'
    # All-zero score JSON → confidence is the max, which is 0.0
    assert row[4] == 0.0
    # classifier_model is 'llm_or_keyword' when prefer_llm=True even if
    # the LLM mock returned zeros (the mock IS the LLM as far as the
    # classifier is concerned).
    assert row[6] == 'llm_or_keyword'


def test_classify_and_persist_accepts_documented_signature():
    """Phase 7e closure: lock the public signature.  classify_and_persist
    MUST accept positional (db, source_kind, source_id, content) +
    optional (tenant_id, prefer_llm, commit) kwargs.  A signature
    change would silently break api.create_post + api.create_comment
    + api.reply_to_comment + the reclassify background job."""
    import inspect
    from integrations.social.content_classifier import ContentClassifier
    sig = inspect.signature(ContentClassifier.classify_and_persist)
    params = sig.parameters
    assert 'db' in params
    assert 'source_kind' in params
    assert 'source_id' in params
    assert 'content' in params
    # Optional kwargs that callers depend on.
    assert 'tenant_id' in params
    assert 'prefer_llm' in params
    assert 'commit' in params
    # commit defaults to True per the docstring (Pass-4 P4-10).
    assert params['commit'].default is True


# ── 4. Quarantine path: hate score in [0.55, 0.85) flips is_quarantined ──

def test_quarantine_path_high_hate_flips_is_quarantined(
        fresh_db, monkeypatch):
    """Phase 7e closure promise: when classifier verdict is `quarantine`
    on a hate score above QUARANTINE_THRESHOLD (0.55) but BELOW
    BLOCK_THRESHOLD (0.85), the posts.is_quarantined column flips to 1.

    Note: `hate` is BOTH in BLOCK_PROTECTED and QUARANTINE_GREY.  At
    hate=0.86 the verdict is `block`; at hate in [0.55, 0.85) it's
    `quarantine`.  The task prompt says "hate > 0.85" but at exactly
    0.85+ we hit BLOCK_PROTECTED gate → block (which flips is_hidden,
    not is_quarantined).  So we use 0.80 to land cleanly in quarantine
    territory and exercise the is_quarantined flip the prompt asked for.

    Skips gracefully if posts.is_quarantined column wasn't migrated
    (e.g., running on a pre-v50 schema).
    """
    from sqlalchemy import text
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db

    if not _post_column_exists(db, 'is_quarantined'):
        pytest.skip(
            "posts.is_quarantined column missing — migration v50 "
            "(or its raw-SQL ALTER TABLE) did not run.  Closure test "
            "cannot exercise the flip contract on this schema.")

    u = _seed_user(db)
    p = _make_post(db, u.id, content='trigger hate quarantine')

    # Inject hate=0.80 via LLM mock.  hate has NO keyword fallback
    # (Pass-4 P4-7 dropped placeholder slurs), so the only way to
    # exercise the hate-quarantine path is via the LLM gateway.
    fake_gateway = type('M', (), {
        'gateway_complete': lambda prompt, **kw: (
            '{"hate":0.80,"harassment":0.0,"sexual":0.0,'
            '"sexual_minors":0.0,"violence":0.0,"self_harm":0.0}'
        )
    })
    monkeypatch.setitem(sys.modules,
                        'integrations.providers.gateway', fake_gateway)

    result = ContentClassifier.classify_and_persist(
        db, source_kind='post', source_id=p.id,
        content='trigger hate quarantine', prefer_llm=True)

    # 0.80 hate is above QUARANTINE_THRESHOLD 0.55 + below
    # BLOCK_THRESHOLD 0.85 → quarantine.
    assert result['decision'] == 'quarantine'
    assert result['confidence'] >= 0.55
    assert result['confidence'] < 0.85

    row = db.execute(text(
        "SELECT is_hidden, is_quarantined FROM posts WHERE id = :id"),
        {'id': p.id}).fetchone()
    assert row[0] == 0, "is_hidden must NOT flip on quarantine verdict"
    assert row[1] == 1, "is_quarantined MUST flip on quarantine verdict"


def test_block_path_hate_above_0_85_flips_is_hidden(fresh_db, monkeypatch):
    """Phase 7e closure addendum: the prompt asked specifically about
    hate > 0.85 → is_quarantined.  In the shipped code, hate > 0.85
    actually trips BLOCK (because hate is in BLOCK_PROTECTED), which
    flips is_hidden NOT is_quarantined.  This test documents that
    branch explicitly so the contract is unambiguous and so a future
    refactor that swaps the gates is caught.
    """
    from sqlalchemy import text
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db
    u = _seed_user(db)
    p = _make_post(db, u.id, content='extreme hate content')

    # hate=0.90 — above BLOCK_THRESHOLD 0.85.  hate IS in
    # BLOCK_PROTECTED, so the verdict is `block` (not `quarantine`).
    fake_gateway = type('M', (), {
        'gateway_complete': lambda prompt, **kw: (
            '{"hate":0.90,"harassment":0.0,"sexual":0.0,'
            '"sexual_minors":0.0,"violence":0.0,"self_harm":0.0}'
        )
    })
    monkeypatch.setitem(sys.modules,
                        'integrations.providers.gateway', fake_gateway)

    result = ContentClassifier.classify_and_persist(
        db, source_kind='post', source_id=p.id,
        content='extreme hate content', prefer_llm=True)

    assert result['decision'] == 'block', (
        "hate >= 0.85 must trip BLOCK_PROTECTED gate (not quarantine).  "
        "If this fails, somebody changed the gate semantics — verify "
        "the docstring change is intentional + acceptance tests updated."
    )

    row = db.execute(text(
        "SELECT is_hidden, is_quarantined FROM posts WHERE id = :id"),
        {'id': p.id}).fetchone()
    assert row[0] == 1, "is_hidden MUST flip on block verdict"
    assert row[1] == 0, "is_quarantined must NOT flip on block verdict"
