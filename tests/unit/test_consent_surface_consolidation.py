"""Cross-surface guard tests for the consent-surface consolidation
(orchestrator review acd11f55, 2026-04-25).

Two write paths used to exist for ``user_consents``:

  1. ``/api/consent/<user_id>/*`` — registered by
     ``register_consent_routes`` in ``consent_service.py``.  No JWT;
     accepted ``user_id`` from the URL path.  UPSERT semantics —
     re-grants rewrote ``granted_at``.
  2. ``/api/social/consent`` (consent_bp) in ``consent_api.py``.
     JWT-authed.  APPEND-ONLY — every grant is a NEW row.

This file pins the consolidation:

  a) ``register_consent_routes`` must NOT be importable from
     ``consent_service.py`` — the symbol is gone.
  b) The legacy ``/api/consent/<user_id>/*`` URL family is NOT
     registered on a fresh Flask app that mounts ``consent_bp``.
  c) ``ConsentService.grant_consent`` is APPEND-ONLY: a second grant
     for the same triple creates a SECOND row; ``granted_at`` is
     never rewritten.
  d) ``ConsentService.has_active_consent`` (alias ``check_consent``)
     still returns ``True`` after a re-grant — the read layer's
     semantic equivalence is preserved.
  e) The internal callers at ``revenue_tracker.py:178`` and
     ``ai_governance.py:684`` continue to import
     ``ConsentService.check_consent``.

The test file is deliberately import-only for (e): the goal is to
catch a regression where the symbol vanishes or the call site is
deleted, not to spin up the full revenue/governance stack.
"""
from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ══════════════════════════════════════════════════════════════════════
# (a) register_consent_routes is GONE from consent_service.py
# ══════════════════════════════════════════════════════════════════════

def test_register_consent_routes_symbol_removed():
    """The legacy route-registration helper must not exist."""
    consent_service = importlib.import_module(
        'integrations.social.consent_service',
    )
    assert not hasattr(consent_service, 'register_consent_routes'), (
        'register_consent_routes must be deleted as part of the '
        'consent-surface consolidation (orchestrator review acd11f55).'
    )


def test_register_consent_routes_not_importable():
    """Direct ``from ... import register_consent_routes`` must fail."""
    with pytest.raises(ImportError):
        from integrations.social.consent_service import (  # noqa: F401
            register_consent_routes,
        )


# ══════════════════════════════════════════════════════════════════════
# (b) /api/consent/<user_id>/* is NOT registered when consent_bp is
#     mounted on a fresh Flask app
# ══════════════════════════════════════════════════════════════════════

def test_legacy_consent_routes_not_registered():
    """A fresh Flask app + the new consent_bp must NOT serve any of
    the deprecated /api/consent/<user_id>/* paths.
    """
    from flask import Flask
    from integrations.social.consent_api import consent_bp

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(consent_bp)

    legacy_paths = [
        ('GET', '/api/consent/u1'),
        ('POST', '/api/consent/u1'),
        ('POST', '/api/consent/u1/revoke'),
        ('GET', '/api/consent/u1/check'),
    ]
    client = app.test_client()
    for method, path in legacy_paths:
        if method == 'GET':
            resp = client.get(path)
        else:
            resp = client.post(path, json={})
        assert resp.status_code == 404, (
            f'{method} {path} returned {resp.status_code}; '
            f'legacy /api/consent/* must be 404 after the '
            f'consolidation.'
        )


# ══════════════════════════════════════════════════════════════════════
# (c) ConsentService.grant_consent is APPEND-ONLY
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def fresh_db():
    """Fresh in-memory SQLite session for each test."""
    from integrations.social.models import (
        Base, get_engine, db_session,
    )
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield db_session
    Base.metadata.drop_all(engine)


def test_grant_consent_is_append_only(fresh_db):
    """Two grants for the same (user_id, consent_type, scope) tuple
    create TWO rows.  Neither row's granted_at is rewritten.
    """
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import UserConsent

    with fresh_db() as db:
        c1 = ConsentService.grant_consent(db, 'u1', 'data_access')
        c1_id = c1.id
        c1_granted_at = c1.granted_at

    with fresh_db() as db:
        c2 = ConsentService.grant_consent(db, 'u1', 'data_access')
        c2_id = c2.id

    assert c1_id != c2_id, 'second grant must be a NEW row'

    # Direct DB query: there are exactly TWO rows for this triple.
    with fresh_db() as db:
        rows = db.query(UserConsent).filter(
            UserConsent.user_id == 'u1',
            UserConsent.consent_type == 'data_access',
            UserConsent.scope == '*',
        ).order_by(UserConsent.granted_at.asc()).all()

    assert len(rows) == 2, (
        f'expected 2 rows after re-grant (append-only); got {len(rows)}'
    )
    assert rows[0].id == c1_id
    assert rows[1].id == c2_id
    assert rows[0].granted_at == c1_granted_at, (
        'first row\'s granted_at must NOT be rewritten by the second '
        'grant — audit immutability invariant.'
    )


def test_grant_consent_returns_new_row_each_time(fresh_db):
    """Each grant returns a fresh row — never the prior one."""
    from integrations.social.consent_service import ConsentService

    ids = set()
    for _ in range(3):
        with fresh_db() as db:
            c = ConsentService.grant_consent(db, 'u1', 'revenue_share')
            ids.add(c.id)

    assert len(ids) == 3, (
        f'three grants must produce three distinct ids; got {ids}'
    )


# ══════════════════════════════════════════════════════════════════════
# (d) check_consent / has_active_consent semantic equivalence preserved
# ══════════════════════════════════════════════════════════════════════

def test_check_consent_true_after_regrant(fresh_db):
    """check_consent must return True after a re-grant — the read
    layer is independent of how many rows back the audit trail.
    """
    from integrations.social.consent_service import ConsentService

    with fresh_db() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    with fresh_db() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')

    with fresh_db() as db:
        assert ConsentService.check_consent(
            db, 'u1', 'data_access',
        ) is True
        # has_consent is the documented public alias.
        assert ConsentService.has_consent(
            db, 'u1', 'data_access',
        ) is True


def test_check_consent_false_after_revoke_of_only_active_row(fresh_db):
    """If we grant once then revoke, check_consent returns False.
    (Sanity check — revoke targets the active row, append-only does
    not magically resurrect it.)
    """
    from integrations.social.consent_service import ConsentService

    with fresh_db() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    with fresh_db() as db:
        ConsentService.revoke_consent(db, 'u1', 'data_access')
    with fresh_db() as db:
        assert ConsentService.check_consent(
            db, 'u1', 'data_access',
        ) is False


# ══════════════════════════════════════════════════════════════════════
# (e) revenue_tracker + ai_governance call sites still resolve
# ══════════════════════════════════════════════════════════════════════

def test_revenue_tracker_imports_check_consent():
    """revenue_tracker.py:178 imports ConsentService.check_consent.

    Regression guard: if the symbol moves or vanishes, this test
    breaks — surfacing the cross-module dependency before runtime.
    """
    rt = importlib.import_module('integrations.providers.revenue_tracker')
    src = Path(rt.__file__).read_text(encoding='utf-8')
    assert 'ConsentService.check_consent' in src, (
        'revenue_tracker must keep its ConsentService.check_consent '
        'call site (currently revenue_tracker.py:178).'
    )


def test_ai_governance_imports_check_consent():
    """ai_governance.py:684 imports ConsentService.check_consent."""
    gov = importlib.import_module('security.ai_governance')
    src = Path(gov.__file__).read_text(encoding='utf-8')
    assert 'ConsentService.check_consent' in src, (
        'ai_governance must keep its ConsentService.check_consent '
        'call site (currently ai_governance.py:684).'
    )


def test_consent_service_static_methods_still_exposed():
    """The full read+write static-method API remains importable for
    internal callers (revenue_tracker, ai_governance,
    federated_aggregator, lifecycle_hooks)."""
    from integrations.social.consent_service import ConsentService

    expected = {
        'request_consent', 'grant_consent', 'revoke_consent',
        'check_consent', 'has_consent', 'list_consents',
        'set_payment_id', 'get_payment_id',
    }
    missing = {
        name for name in expected
        if not hasattr(ConsentService, name)
    }
    assert not missing, (
        f'ConsentService missing expected static methods: {missing}'
    )
