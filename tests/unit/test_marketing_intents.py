"""Tests for /api/social/marketing/intents (#181).

Pins the contract that:
  - Every platform has at least one canonical MarketingIntent.
  - Every intent's code matches the regex /marketing/track requires
    (so a click → /marketing/track increment is a closed loop).
  - Every landing_url carries ?ref=<code>.
  - The endpoint filters by platform query param + returns the full
    list when platform is absent.

This module replaces the one-off _drops_ready/intents.md doc.  Tests
catch silent drift if a future entry forgets the ref-tag or uses an
invalid code shape.
"""
from __future__ import annotations

import re

import pytest


# Mirror the regex in api.py:track_marketing_event
_CODE_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')


def _make_client():
    from flask import Flask
    from integrations.social.api import social_bp
    app = Flask(__name__)
    app.register_blueprint(social_bp)
    return app.test_client()


# ─── intents.py module-level invariants ────────────────────────────


def test_every_platform_has_an_intent():
    from integrations.marketing.intents import (
        INTENTS, list_platforms, INTENTS_BY_PLATFORM,
    )
    platforms = list_platforms()
    assert len(platforms) >= 5  # twitter, linkedin, reddit, hn, whatsapp
    for p in platforms:
        assert INTENTS_BY_PLATFORM[p], f'platform {p} has no intents'
    assert all(i.platform in platforms for i in INTENTS)


def test_every_code_matches_marketing_track_regex():
    """Every intent's code must validate via the /marketing/track
    regex, otherwise the loop click → counter increment silently
    breaks (track returns 400 on invalid codes).
    """
    from integrations.marketing.intents import INTENTS
    for intent in INTENTS:
        assert _CODE_RE.match(intent.code), (
            f'code {intent.code!r} for platform {intent.platform} '
            f'does not match /marketing/track validation regex')


def test_every_landing_url_carries_ref():
    from integrations.marketing.intents import INTENTS
    for intent in INTENTS:
        assert f'ref={intent.code}' in intent.landing_url, (
            f'landing_url for platform={intent.platform} '
            f'code={intent.code} missing ?ref={intent.code} '
            f'attribution tag — got {intent.landing_url}')


def test_codes_are_unique():
    from integrations.marketing.intents import INTENTS
    codes = [i.code for i in INTENTS]
    assert len(codes) == len(set(codes)), (
        f'duplicate codes found: {codes}')


# ─── /marketing/intents endpoint ───────────────────────────────────


def test_endpoint_returns_all_intents_without_filter():
    client = _make_client()
    resp = client.get('/api/social/marketing/intents')
    assert resp.status_code == 200
    data = resp.get_json()['data']
    assert 'intents' in data
    assert data['count'] == len(data['intents'])
    assert data['platform_filter'] is None
    assert isinstance(data['platforms'], list)
    assert 'twitter' in data['platforms']
    assert 'linkedin' in data['platforms']
    assert 'whatsapp' in data['platforms']


def test_endpoint_filters_by_platform():
    client = _make_client()
    resp = client.get('/api/social/marketing/intents?platform=twitter')
    data = resp.get_json()['data']
    assert data['platform_filter'] == 'twitter'
    assert all(i['platform'] == 'twitter' for i in data['intents'])
    assert data['count'] >= 1


def test_endpoint_handles_unknown_platform_gracefully():
    """Unknown platforms return empty intents (not a 4xx) — frontend
    can decide what to do with the empty list.
    """
    client = _make_client()
    resp = client.get('/api/social/marketing/intents?platform=myspace')
    assert resp.status_code == 200
    data = resp.get_json()['data']
    assert data['count'] == 0
    assert data['intents'] == []


def test_intent_shape_carries_all_required_fields():
    client = _make_client()
    resp = client.get('/api/social/marketing/intents?platform=linkedin')
    intent = resp.get_json()['data']['intents'][0]
    assert intent['platform'] == 'linkedin'
    assert intent['code'].startswith('li_')
    assert intent['landing_url']
    assert intent['body_text']  # LinkedIn needs paste-able body


def test_whatsapp_has_no_intent_url_but_has_body():
    """WhatsApp doesn't have a click-to-compose URL — it's driven by
    the whatsapp_adapter sending body_text directly.  The endpoint
    must return body_text even when intent_url is empty.
    """
    client = _make_client()
    resp = client.get('/api/social/marketing/intents?platform=whatsapp')
    intent = resp.get_json()['data']['intents'][0]
    assert intent['intent_url'] == ''
    assert 'hevolve.ai/download' in intent['landing_url']
    assert 'free tier' in intent['body_text'].lower()


def test_twitter_intent_url_carries_body_and_landing():
    """The Twitter intent URL must be fully self-contained — opening
    it pre-fills the composer with body + landing URL.
    """
    from urllib.parse import unquote
    client = _make_client()
    resp = client.get('/api/social/marketing/intents?platform=twitter')
    intent = resp.get_json()['data']['intents'][0]
    assert intent['intent_url'].startswith(
        'https://twitter.com/intent/tweet?text=')
    decoded = unquote(intent['intent_url'])
    assert 'ref=tw1' in decoded
    assert 'AI API' in decoded
