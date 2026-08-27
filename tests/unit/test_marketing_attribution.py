"""Tests for the /api/social/marketing/track endpoint (task #178).

Pins behavior of the anonymous channel-code attribution writer +
aggregating reader. The endpoint accepts {code, event, platform, ...},
appends a JSONL row, and the /stats reader groups by code+event.

Flywheel context: every Twitter/LinkedIn/Reddit/WhatsApp drop carries
a ?ref=<channel_code> in its download URL. This endpoint is what the
landing page calls to bump the counter, so we can measure which channel
converts.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import pytest


def _make_client(tmp_data_dir):
    """Build a minimal Flask test client wired to a fresh data dir."""
    from flask import Flask
    from integrations.social.api import social_bp

    app = Flask(__name__)
    app.register_blueprint(social_bp)

    # Re-route get_data_dir to the tmp dir so the JSONL writes land there.
    patcher = patch('core.platform_paths.get_data_dir',
                    return_value=tmp_data_dir)
    patcher.start()
    return app.test_client(), patcher


# ─── /marketing/track — valid input ───

def test_track_writes_jsonl_row(tmp_path):
    client, patcher = _make_client(str(tmp_path))
    try:
        resp = client.post(
            '/api/social/marketing/track',
            json={'code': 'li_a', 'event': 'click',
                  'platform': 'linkedin'},
            headers={'User-Agent': 'pytest/1.0'},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert body['data']['tracked'] is True
        assert body['data']['code'] == 'li_a'
        assert body['data']['event'] == 'click'

        path = os.path.join(str(tmp_path), 'marketing_clicks.jsonl')
        assert os.path.exists(path)
        with open(path, 'r', encoding='utf-8') as f:
            row = json.loads(f.readline())
        assert row['code'] == 'li_a'
        assert row['event'] == 'click'
        assert row['platform'] == 'linkedin'
        assert 'ts' in row
        assert len(row['ip_hash']) == 16
        assert len(row['ua_hash']) == 16
    finally:
        patcher.stop()


# ─── /marketing/track — invalid code shape ───

def test_track_rejects_uppercase_code(tmp_path):
    client, patcher = _make_client(str(tmp_path))
    try:
        resp = client.post(
            '/api/social/marketing/track',
            json={'code': 'LI_A', 'event': 'click'},
        )
        assert resp.status_code == 400
        assert 'invalid code' in resp.get_json()['error']
    finally:
        patcher.stop()


def test_track_rejects_empty_code(tmp_path):
    client, patcher = _make_client(str(tmp_path))
    try:
        resp = client.post('/api/social/marketing/track',
                           json={'code': '', 'event': 'click'})
        assert resp.status_code == 400
    finally:
        patcher.stop()


def test_track_normalizes_hyphenated_channel_code(tmp_path):
    """A launch channel written with a hyphen ('product-hunt') must be captured,
    normalized to its underscore bucket, not 400ed and silently lost
    (PENDING.md #10 — launch channels are exactly where hyphens get used)."""
    client, patcher = _make_client(str(tmp_path))
    try:
        resp = client.post(
            '/api/social/marketing/track',
            json={'code': 'product-hunt', 'event': 'click'},
        )
        assert resp.status_code == 200
        assert resp.get_json()['data']['code'] == 'product_hunt'
        path = os.path.join(str(tmp_path), 'marketing_clicks.jsonl')
        with open(path, 'r', encoding='utf-8') as f:
            row = json.loads(f.readline())
        assert row['code'] == 'product_hunt'
    finally:
        patcher.stop()


# ─── /marketing/track — invalid event ───

def test_track_rejects_unknown_event(tmp_path):
    client, patcher = _make_client(str(tmp_path))
    try:
        resp = client.post(
            '/api/social/marketing/track',
            json={'code': 'li_a', 'event': 'purchase'},
        )
        assert resp.status_code == 400
        assert 'invalid event' in resp.get_json()['error']
    finally:
        patcher.stop()


# ─── /marketing/stats — aggregation ───

def test_stats_groups_by_code_and_event(tmp_path):
    client, patcher = _make_client(str(tmp_path))
    try:
        # Three clicks on li_a, one download on li_a, one click on tw1
        for _ in range(3):
            client.post('/api/social/marketing/track',
                        json={'code': 'li_a', 'event': 'click'})
        client.post('/api/social/marketing/track',
                    json={'code': 'li_a', 'event': 'download'})
        client.post('/api/social/marketing/track',
                    json={'code': 'tw1', 'event': 'click'})

        resp = client.get('/api/social/marketing/stats')
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert data['total'] == 5
        assert data['by_code']['li_a']['click'] == 3
        assert data['by_code']['li_a']['download'] == 1
        assert data['by_code']['li_a']['install'] == 0
        assert data['by_code']['tw1']['click'] == 1
    finally:
        patcher.stop()


def test_stats_filters_by_code(tmp_path):
    client, patcher = _make_client(str(tmp_path))
    try:
        client.post('/api/social/marketing/track',
                    json={'code': 'li_a', 'event': 'click'})
        client.post('/api/social/marketing/track',
                    json={'code': 'tw1', 'event': 'click'})

        resp = client.get('/api/social/marketing/stats?code=li_a')
        data = resp.get_json()['data']
        assert 'li_a' in data['by_code']
        assert 'tw1' not in data['by_code']
        assert data['total'] == 1
    finally:
        patcher.stop()


def test_stats_empty_when_no_clicks(tmp_path):
    client, patcher = _make_client(str(tmp_path))
    try:
        resp = client.get('/api/social/marketing/stats')
        data = resp.get_json()['data']
        assert data['total'] == 0
        assert data['by_code'] == {}
    finally:
        patcher.stop()


# ─── Idempotency-ish: two writes from same code accumulate ───

def test_repeat_clicks_accumulate(tmp_path):
    client, patcher = _make_client(str(tmp_path))
    try:
        for _ in range(10):
            client.post('/api/social/marketing/track',
                        json={'code': 'wa_broadcast', 'event': 'click'})
        resp = client.get('/api/social/marketing/stats?code=wa_broadcast')
        data = resp.get_json()['data']
        assert data['by_code']['wa_broadcast']['click'] == 10
    finally:
        patcher.stop()


# ─── _record_marketing_event — the single-source writer used by BOTH the
#     /marketing/track route AND signup (register), so a marketing-driven
#     signup is counted with the same writer the clicks/downloads use. ───

def test_record_event_writes_signup_row_directly(tmp_path):
    with patch('core.platform_paths.get_data_dir', return_value=str(tmp_path)):
        from integrations.social.api import _record_marketing_event
        row = _record_marketing_event('hn_show', 'signup', 'web', b'9.9.9.9', 'UA')
        assert row is not None and row['code'] == 'hn_show' and row['event'] == 'signup'
        path = os.path.join(str(tmp_path), 'marketing_clicks.jsonl')
        last = json.loads(open(path, encoding='utf-8').read().strip().splitlines()[-1])
        assert last['event'] == 'signup' and last['code'] == 'hn_show'
        assert '9.9.9.9' not in open(path, encoding='utf-8').read()  # ip hashed


def test_record_event_skips_user_referral_code(tmp_path):
    """register() funnels ALL referral_codes through this writer; a mixed-case
    user invite code (handled by the Referral table) must NOT create a channel
    row, or it would pollute channel attribution."""
    with patch('core.platform_paths.get_data_dir', return_value=str(tmp_path)):
        from integrations.social.api import _record_marketing_event
        assert _record_marketing_event('ABC123XYZ', 'signup', 'web') is None
        assert not os.path.exists(os.path.join(str(tmp_path), 'marketing_clicks.jsonl'))


# ─── /marketing/growth — progress-toward-10K dashboard ───

def test_growth_dashboard_combines_funnel_and_target(tmp_path):
    """The "make sure we have 10K" view: user-count-vs-target + the per-channel
    funnel with conversion, sorted so the best-converting channel surfaces."""
    client, patcher = _make_client(str(tmp_path))
    try:
        for _ in range(5):
            client.post('/api/social/marketing/track', json={'code': 'li_a', 'event': 'click'})
        for _ in range(2):
            client.post('/api/social/marketing/track', json={'code': 'li_a', 'event': 'signup'})
        for _ in range(3):
            client.post('/api/social/marketing/track', json={'code': 'tw1', 'event': 'click'})

        resp = client.get('/api/social/marketing/growth')
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert data['target'] == 10000
        # user count + trajectory fields present (count may be 0 with no DB)
        for k in ('total_users', 'pct_to_target', 'remaining', 'new_users_7d'):
            assert k in data
        assert isinstance(data['total_users'], int) and data['total_users'] >= 0
        assert data['funnel_events_total'] == 10  # 5 + 2 + 3
        # channels sorted by signups desc → li_a (2 signups) leads tw1 (0)
        assert data['top_channel'] == 'li_a'
        assert data['channels'][0]['code'] == 'li_a'
        assert data['channels'][0]['signup'] == 2
        assert data['channels'][0]['signup_rate'] == round(2 / 5, 4)
    finally:
        patcher.stop()
