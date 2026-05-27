"""Behavioural tests for realtime.py PII sanitizer (#41).

These call the actual sanitizer + on_new_post / on_new_comment with a
mocked publish_event, and assert on the dict that hits the WAMP wire —
NOT on file contents.  If a field is added to User.to_dict() that
should be private, this suite must be extended; otherwise a future
schema change silently re-opens the leak.
"""
from unittest.mock import patch
import pytest

from integrations.social import realtime
from integrations.social.realtime import (
    _AUTHOR_PUBLIC_BLOCKLIST,
    _sanitize_author_for_public,
    _sanitize_for_public_broadcast,
    on_new_comment,
    on_new_post,
)


# ─── helpers ─────────────────────────────────────────────────────

def _full_author():
    """An author dict containing every field that could leak."""
    return {
        'id': 'u-42',
        'username': 'alice',
        'display_name': 'Alice',
        'avatar_url': 'https://cdn/alice.png',
        'karma_score': 12,
        'post_count': 3,
        # blocklist starts here ↓
        'email': 'alice@example.com',
        'phone': '+15551234567',
        'phone_number': '+15551234567',
        'password_hash': 'pbkdf2:sha256:1$...$....',
        'api_token': 'tok_secret',
        'voice_profile': 'voiceprint-blob',
        'idle_compute_opt_in': True,
        'location_sharing_enabled': True,
        'referral_code': 'REF-ALICE',
        'last_active_at': '2026-05-27T12:34:56Z',
    }


# ─── 1. _sanitize_author_for_public ──────────────────────────────

def test_sanitize_author_drops_every_blocklist_field():
    out = _sanitize_author_for_public(_full_author())
    for field in _AUTHOR_PUBLIC_BLOCKLIST:
        assert field not in out, (
            f"blocklisted field {field!r} survived sanitization → "
            f"public-feed leak"
        )


def test_sanitize_author_preserves_public_fields():
    out = _sanitize_author_for_public(_full_author())
    # These ARE intended to be public profile data:
    assert out['id'] == 'u-42'
    assert out['username'] == 'alice'
    assert out['display_name'] == 'Alice'
    assert out['avatar_url'] == 'https://cdn/alice.png'
    assert out['karma_score'] == 12
    assert out['post_count'] == 3


def test_sanitize_author_idempotent():
    once = _sanitize_author_for_public(_full_author())
    twice = _sanitize_author_for_public(once)
    assert once == twice


def test_sanitize_author_non_dict_passes_through():
    # Defensive — never raise on malformed input.
    assert _sanitize_author_for_public(None) is None
    assert _sanitize_author_for_public("nope") == "nope"
    assert _sanitize_author_for_public(42) == 42


# ─── 2. _sanitize_for_public_broadcast (post-level) ──────────────

def test_sanitize_post_strips_author_pii_but_keeps_content():
    post = {
        'id': 'p-1',
        'title': 'hello',
        'content': 'world',
        'community_id': 'c-7',
        'author': _full_author(),
    }
    safe = _sanitize_for_public_broadcast(post)
    # Post-level fields preserved.
    assert safe['id'] == 'p-1'
    assert safe['title'] == 'hello'
    assert safe['content'] == 'world'
    assert safe['community_id'] == 'c-7'
    # Author sanitized.
    for field in _AUTHOR_PUBLIC_BLOCKLIST:
        assert field not in safe['author']
    # Author kept its safe fields.
    assert safe['author']['username'] == 'alice'


def test_sanitize_does_not_mutate_input():
    post = {
        'id': 'p-1',
        'author': _full_author(),
    }
    safe = _sanitize_for_public_broadcast(post)
    # Original untouched — caller's in-memory dict still has PII for
    # whatever non-public path they're using it for (HTTP response,
    # logs, etc.).
    assert post['author']['email'] == 'alice@example.com'
    assert safe is not post
    assert safe['author'] is not post['author']


def test_sanitize_post_without_author_passes_through():
    post = {'id': 'p-1', 'title': 'x'}
    safe = _sanitize_for_public_broadcast(post)
    assert safe == post
    assert safe is not post  # shallow copy still returned


def test_sanitize_post_with_non_dict_author_passes_through():
    post = {'id': 'p-1', 'author': None}
    assert _sanitize_for_public_broadcast(post) == post


# ─── 3. on_new_post — publish_event sees sanitized payload ──────

def test_on_new_post_sanitizes_community_feed_payload():
    post = {'id': 'p-1', 'title': 'hi', 'author': _full_author()}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_new_post(post)
    assert len(calls) == 1
    topic, data = calls[0]
    assert topic == 'community.feed'
    for field in _AUTHOR_PUBLIC_BLOCKLIST:
        assert field not in data['author'], (
            f"{field!r} reached WAMP topic {topic!r}"
        )


def test_on_new_post_sanitizes_community_message_payload():
    post = {'id': 'p-1', 'title': 'hi', 'author': _full_author()}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_new_post(post, community_name='general')
    # Two calls: community.feed + community.message.
    topics = [c[0] for c in calls]
    assert 'community.feed' in topics
    assert 'community.message' in topics
    for _topic, data in calls:
        for field in _AUTHOR_PUBLIC_BLOCKLIST:
            assert field not in data['author']
    # community.message also carries community_id (sanitizer doesn't
    # strip that — it's the routing key).
    msg = next(d for t, d in calls if t == 'community.message')
    assert msg['community_id'] == 'general'


def test_on_new_post_does_not_mutate_caller_dict():
    post = {'id': 'p-1', 'author': _full_author()}
    with patch.object(realtime, 'publish_event'):
        on_new_post(post, community_name='general')
    # Caller's post dict is unchanged.
    assert post['author']['email'] == 'alice@example.com'
    assert post['author']['voice_profile'] == 'voiceprint-blob'


# ─── 4. on_new_comment — same guarantee ─────────────────────────

def test_on_new_comment_sanitizes_payload():
    comment = {'id': 'c-1', 'content': 'reply', 'author': _full_author()}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_new_comment(comment, post_id='p-1')
    assert len(calls) == 1
    topic, data = calls[0]
    assert topic == 'social.post.p-1.new_comment'
    for field in _AUTHOR_PUBLIC_BLOCKLIST:
        assert field not in data['author']


# ─── 5. Sanity: blocklist covers the User.to_dict surface ──────

def test_blocklist_contains_known_sensitive_fields():
    # If User.to_dict ever gains email/phone/etc again (it doesn't
    # today), they MUST already be in the blocklist.  This is a
    # forward-defense assertion, not a current-leak assertion.
    must_be_blocked = {
        'email', 'phone', 'phone_number', 'password_hash', 'api_token',
        'voice_profile', 'idle_compute_opt_in',
        'location_sharing_enabled', 'referral_code', 'last_active_at',
    }
    missing = must_be_blocked - set(_AUTHOR_PUBLIC_BLOCKLIST)
    assert not missing, (
        f"sensitive fields not in blocklist: {missing} — extending the "
        f"User schema with one of these would silently leak via "
        f"community.feed; add to _AUTHOR_PUBLIC_BLOCKLIST first."
    )
