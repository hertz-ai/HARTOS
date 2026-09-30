"""A private share link must not hand out the resource before consent.

THE DEFECT (found auditing the share-link "consent store", 2026-09-21):
``resolve_share_token`` withheld only the OG preview card for a private link.
It still returned ``redirect_url``, ``resource_type`` and ``resource_id`` — the
actual locator — and that route is ``@optional_auth`` while
``/share/<token>/consent`` is ``@require_auth``.  So an anonymous caller could
read the target straight out of the resolve response and open the resource
without ever consenting: the gate hid the picture and published the address.

Same shape as the trust gate in hive_expert_discovery: a check that looks like it
protects something and does not.

Second finding, same read: ``requires_consent`` was ``link.is_private`` flat, so
a viewer who had already accepted was asked again on every single visit and
banked a duplicate ShareEvent each time.  The "has this viewer consented"
predicate existed in ``/check-consent`` and was simply not consulted here — the
query is now one helper both paths share.

NOT folded onto ``UserConsent``, deliberately: that table is "I permit software
to do X to me" (camera, screen, copilot, device), keyed
UNIQUE(user_id, agent_id, consent_type, scope) with a revoke + privacy-page
lifecycle.  This is a VIEWER acknowledging a notice before seeing someone else's
content — different subject, different object, per-link and unbounded, and it
doubles as the sharer's audit trail.  Folding it would put every view
acknowledgement on the owner's privacy page and drown the actual capability
grants.  The plan's own "do not fold" list already flagged this file as a
name collision.
"""
import contextlib
import json
import os

import pytest

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from flask import Flask, g                                      # noqa: E402

from integrations.social import api_sharing                     # noqa: E402
from integrations.social.models import (                        # noqa: E402
    Base, ShareEvent, ShareableLink, get_engine, get_db,
)

PRIVATE = 'tok-private'
PUBLIC = 'tok-public'
VIEWER = 'viewer-1'


@pytest.fixture
def ctx(monkeypatch):
    """A request context over a real DB holding one private and one public link."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    db = get_db()
    for row in db.query(ShareEvent).all():
        db.delete(row)
    for row in db.query(ShareableLink).all():
        db.delete(row)
    db.add(ShareableLink(
        id='link-priv', token=PRIVATE, resource_type='post',
        resource_id='post-42', is_private=True,
        metadata_json=json.dumps({'title': 'Secret', 'image': 'x.png'})))
    db.add(ShareableLink(
        id='link-pub', token=PUBLIC, resource_type='post',
        resource_id='post-7', is_private=False,
        metadata_json=json.dumps({'title': 'Open'})))
    db.commit()

    # The endpoints call get_db() and close it; hand back the same session so
    # the rows above stay visible and a close() does not end the test's session.
    monkeypatch.setattr(api_sharing, 'get_db', lambda: db)
    monkeypatch.setattr(db, 'close', lambda: None)

    # Go through the REAL @optional_auth, which sets g.user_id itself (to None
    # when no Authorization header — exactly the anonymous case under test, and
    # the reason injecting g.user_id directly would prove nothing).  Only the
    # token->user lookup is stubbed.
    import types

    from integrations.social import auth as social_auth
    monkeypatch.setattr(
        social_auth, '_get_user_from_token',
        lambda tok: (types.SimpleNamespace(id=tok[len('test-'):]), db)
        if tok.startswith('test-') else (None, db))

    app = Flask(__name__)

    class _Ctx:
        session = db

        @contextlib.contextmanager
        def request(self, viewer=None):
            headers = ({'Authorization': f'Bearer test-{viewer}'}
                       if viewer else {})
            with app.test_request_context(headers=headers):
                yield

        def resolve(self, token, viewer=None):
            with self.request(viewer):
                resp = api_sharing.resolve_share_token(token)
            body = resp[0] if isinstance(resp, tuple) else resp
            return json.loads(body.get_data(as_text=True))['data']

        def check(self, token, viewer=None):
            with self.request(viewer):
                resp = api_sharing.check_consent(token)
            body = resp[0] if isinstance(resp, tuple) else resp
            return json.loads(body.get_data(as_text=True))['data']

        def accept(self, viewer):
            db.add(ShareEvent(link_id='link-priv', event_type='consent',
                              viewer_id=viewer))
            db.commit()

    yield _Ctx()
    Base.metadata.drop_all(engine)


# ── the bypass ──────────────────────────────────────────────────────────

def test_anonymous_resolve_of_a_private_link_leaks_no_locator(ctx):
    """THE BUG. Anonymous, no consent — and the target used to come back."""
    data = ctx.resolve(PRIVATE)

    assert 'redirect_url' not in data, (
        'the resource URL was handed to an anonymous caller who has not '
        'consented — the consent step can simply be skipped')
    assert 'resource_id' not in data, (
        'the resource id was handed over before consent')
    assert data['requires_consent'] is True


def test_the_private_stub_keeps_what_the_consent_screen_needs(ctx):
    """Closing the leak must not blank the consent dialog.

    ShareConsentDialog renders resource_type ("wants to share a post with you"),
    so that stays — it is a category, not a locator — along with the stub og.
    """
    data = ctx.resolve(PRIVATE)

    assert data['resource_type'] == 'post'
    assert data['is_private'] is True
    assert data['og']['title'] == 'Private content shared with you'
    assert data['og']['image'] == ''
    assert 'Secret' not in json.dumps(data), (
        'the real OG metadata leaked through the stub')


def test_a_public_link_still_returns_its_locator(ctx):
    """No regression: the whole non-private path is untouched."""
    data = ctx.resolve(PUBLIC)

    assert data['redirect_url'] == '/social/post/post-7'
    assert data['resource_id'] == 'post-7'
    assert data['requires_consent'] is False
    assert data['og']['title'] == 'Open'


# ── the returning viewer ────────────────────────────────────────────────

def test_a_consented_viewer_gets_the_locator_and_is_not_asked_again(ctx):
    ctx.accept(VIEWER)

    data = ctx.resolve(PRIVATE, viewer=VIEWER)

    assert data['requires_consent'] is False, (
        'an already-consented viewer was asked again — that is how a duplicate '
        'ShareEvent was banked on every visit')
    assert data['already_consented'] is True
    assert data['redirect_url'] == '/social/post/post-42'
    assert data['og']['title'] == 'Secret'


def test_a_signed_in_viewer_who_has_not_consented_still_gets_nothing(ctx):
    """Being logged in is not consenting."""
    data = ctx.resolve(PRIVATE, viewer='someone-else')

    assert data['requires_consent'] is True
    assert 'redirect_url' not in data


def test_one_consent_of_one_viewer_does_not_admit_another(ctx):
    ctx.accept(VIEWER)

    other = ctx.resolve(PRIVATE, viewer='viewer-2')

    assert other['requires_consent'] is True
    assert 'redirect_url' not in other


# ── one predicate ───────────────────────────────────────────────────────

@pytest.mark.parametrize('viewer,accepted', [
    (None, False), (VIEWER, False), (VIEWER, True),
])
def test_resolve_and_check_consent_always_agree(ctx, viewer, accepted):
    """They answered the same question with the query written twice.

    Two copies of a permission check are two things to forget to update; this
    pins that they cannot disagree about whether a viewer has consented.
    """
    if accepted:
        ctx.accept(viewer)

    resolved = ctx.resolve(PRIVATE, viewer=viewer)
    checked = ctx.check(PRIVATE, viewer=viewer)

    assert resolved['requires_consent'] == checked['requires_consent']
    assert resolved.get('already_consented', False) is accepted


def test_the_check_is_not_written_twice():
    """Divergence guard: one predicate, both callers."""
    import inspect
    src = inspect.getsource(api_sharing)
    assert src.count("event_type='consent'") == 2, (
        "a third site builds the consent query by hand — it belongs in "
        '_viewer_has_consented (the other occurrence is the WRITE in '
        'grant_consent)')
    for fn in (api_sharing.resolve_share_token, api_sharing.check_consent):
        assert '_viewer_has_consented' in inspect.getsource(fn), (
            f'{fn.__name__} does not use the shared predicate')
