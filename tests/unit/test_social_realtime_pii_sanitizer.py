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
    on_comment_delete,
    on_new_comment,
    on_new_post,
    on_notification,
    on_post_delete,
    on_post_update,
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
    # And the caller's dict didn't acquire the wire-only fields.
    assert 'event' not in post
    assert 'msg_id' not in post


def test_on_new_post_stamps_event_and_msg_id():
    # Subscribers discriminate lifecycle by `event` field; `msg_id`
    # gives them an idempotency key for SSE+WAMP race dedup.
    post = {'id': 'p-1', 'author': _full_author()}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_new_post(post)
    _, data = calls[0]
    assert data['event'] == 'post.new'
    assert 'msg_id' in data
    # ULID-like 16 hex chars (chat_messages._generate_msg_id format).
    assert len(data['msg_id']) >= 16


def test_on_post_update_event_discriminator():
    post = {'id': 'p-1', 'title': 'edited', 'author': _full_author()}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_post_update(post, community_name='general')
    # community.feed + community.message both fire (same routing as
    # post.new — the canonical _publish_post_event handles both).
    events = [d['event'] for _, d in calls]
    assert events == ['post.update', 'post.update']
    for _, data in calls:
        assert 'msg_id' in data
        for field in _AUTHOR_PUBLIC_BLOCKLIST:
            assert field not in data['author']


def test_on_post_delete_event_discriminator():
    snapshot = {'id': 'p-1', 'author_id': 'u-42', 'is_deleted': True}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_post_delete(snapshot, community_name='general')
    events = [d['event'] for _, d in calls]
    assert events == ['post.delete', 'post.delete']
    # is_deleted=True propagates so subscribers know to drop the entry.
    assert all(d['is_deleted'] is True for _, d in calls)


def test_on_comment_delete_event_discriminator():
    snapshot = {'id': 'c-1', 'post_id': 'p-1', 'author_id': 'u-42',
                'is_deleted': True}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_comment_delete(snapshot, community_name='general')
    assert len(calls) == 1
    topic, data = calls[0]
    assert topic == 'community.message'
    assert data['event'] == 'comment.delete'
    assert data['is_deleted'] is True
    assert 'msg_id' in data


def test_on_notification_stamps_msg_id_consistently_across_transports():
    # WAMP + SSE both carry the SAME msg_id so a multidevice race that
    # delivers both transports doesn't double-render the notification.
    wamp_calls = []
    sse_calls = []
    from core.platform import events as platform_events
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: wamp_calls.append(data)):
        with patch.object(platform_events, 'broadcast_sse_safe',
                          side_effect=lambda evt, data, **kw: sse_calls.append(data)):
            on_notification('u-42', {'id': 'notif-1', 'message': 'hi'})
    assert len(wamp_calls) == 1 and len(sse_calls) == 1
    assert wamp_calls[0]['msg_id'] == sse_calls[0]['msg_id']
    assert len(wamp_calls[0]['msg_id']) >= 16


def test_on_notification_respects_caller_supplied_msg_id():
    # If a caller already has a msg_id (e.g. NotificationService
    # generating one at create-time), the helper must NOT clobber it.
    wamp_calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: wamp_calls.append(data)):
        from core.platform import events as platform_events
        with patch.object(platform_events, 'broadcast_sse_safe'):
            on_notification('u-42', {
                'id': 'notif-1', 'msg_id': 'caller-supplied-key',
                'message': 'hi'})
    assert wamp_calls[0]['msg_id'] == 'caller-supplied-key'


# ─── 4. on_new_comment — same guarantee ─────────────────────────

def test_on_new_comment_sanitizes_payload_when_routed_to_community():
    # New canonical routing: comment.new now flows via
    # community.message.{community_name} — the topic web/Android/iOS
    # already subscribe to.  Old social.post.{id}.new_comment had no
    # subscribers (was dead code).
    comment = {'id': 'c-1', 'post_id': 'p-1', 'content': 'reply',
               'author': _full_author()}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_new_comment(comment, community_name='general')
    assert len(calls) == 1
    topic, data = calls[0]
    assert topic == 'community.message'
    assert data['community_id'] == 'general'
    assert data['event'] == 'comment.new'
    assert 'msg_id' in data and len(data['msg_id']) >= 16
    for field in _AUTHOR_PUBLIC_BLOCKLIST:
        assert field not in data['author']


def test_on_new_comment_without_community_publishes_nothing():
    # Comments on community-less posts have no canonical fan-out
    # topic.  The per-user NotificationService path (NotificationService.
    # create → on_notification) is the only live channel for these.
    comment = {'id': 'c-1', 'post_id': 'p-1', 'content': 'reply',
               'author': _full_author()}
    calls = []
    with patch.object(realtime, 'publish_event',
                      side_effect=lambda topic, data, **kw: calls.append((topic, data))):
        on_new_comment(comment)
    assert calls == []


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
